/**
 * « Sous le capot » — tiroir d'écoute du fil : tant qu'il est ouvert, il s'abonne au sujet
 * WebSocket `trace` et liste en direct les messages échangés (sens, protocole, étape,
 * procédure, taille, temps). Un clic sur une ligne révèle la charge utile : texte si elle
 * est lisible, sinon vue hexadécimale annotée (`HexView`, segments de la trace compris).
 */

import { h, clear } from '../core/dom.js';
import { ws } from '../core/api.js';
import { fmtBytes, fmtMs, fmtNumber, fmtTime, fmtUs } from '../core/format.js';
import { icon } from '../core/icons.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../core/protocols.js';
import { navigate } from '../core/router.js';
import { appStore } from '../core/store.js';
import { HexView, toBytes } from './hexview.js';
import { openDrawer } from './modal.js';
import { Badge, Button, EmptyState, IconButton, KeyValue, ProtocolChip, Segmented, StatusDot, Toggle } from './ui.js';

const MAX_EVENTS = 600;
const MAX_TEXT_CHARS = 6000;

/** @type {Array<Object>} Tampon circulaire des évènements normalisés. */
const events = [];
const callStarts = new Map();
let localSeq = 0;

let drawer = null;
let view = null;
let stops = [];
let paused = false;
let filter = 'all';
let showInternal = false;
let pending = [];
let flushFrame = 0;

/* --- Modèle ---------------------------------------------------------------- */

function normalize(raw) {
  localSeq += 1;
  const ts = Number(raw.ts) || Date.now() / 1000;
  const clock = raw.t_ns !== undefined && raw.t_ns !== null ? Number(raw.t_ns) / 1e6 : ts * 1000;
  const callId = String(raw.call_id ?? '');
  const isFirst = callId !== '' && !callStarts.has(callId);
  if (isFirst) callStarts.set(callId, clock);
  return {
    id: localSeq,
    ts,
    callId,
    protocol: raw.protocol ?? '',
    side: raw.side ?? '',
    stage: raw.stage ?? '',
    method: raw.method ?? '',
    durationUs: raw.duration_us ?? null,
    size: raw.size ?? null,
    detail: raw.detail ?? {},
    hex: raw.payload_hex ?? '',
    isFirst,
    offsetMs: callId ? clock - callStarts.get(callId) : null,
  };
}

function isWireEvent(event) {
  return /\.(send|receive|stream_item)$/.test(event.stage) || event.stage === 'client.error' || event.side === 'network' || event.side === 'resilience';
}

function visible(event) {
  if (filter !== 'all' && event.protocol !== filter) return false;
  return showInternal || isWireEvent(event);
}

function directionOf(event) {
  if (event.stage === 'client.error' || event.stage === 'server.error') return { icon: 'x', tone: 'danger', title: 'Erreur' };
  if (event.side === 'network') return { icon: 'zap', tone: 'warning', title: 'Évènement réseau injecté' };
  if (event.side === 'resilience') return { icon: 'shield', tone: 'info', title: 'Résilience du client' };
  if (event.stage.endsWith('.send')) return { icon: 'arrow-up-right', tone: 'out', title: 'Octets émis' };
  if (event.stage.endsWith('.receive') || event.stage.endsWith('.stream_item')) return { icon: 'arrow-down-left', tone: 'in', title: 'Octets reçus' };
  return { icon: 'circle-dot', tone: 'step', title: 'Étape interne' };
}

/* --- Charge utile ------------------------------------------------------------ */

/** Texte UTF-8 lisible, ou `null` si la charge contient du binaire. */
function decodeText(bytes) {
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
  return /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/.test(text) ? null : text;
}

function prettify(text) {
  const trimmed = text.trim();
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return text;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return text;
  }
}

/** Blocs décrivant la charge utile ; un bloc qui porte `destroy()` est libéré quand la ligne se replie. */
function payloadBlock(event) {
  if (event.hex) {
    const bytes = toBytes(event.hex);
    const text = decodeText(bytes);
    const truncated = event.detail?.payload_truncated ? ' · tronquée' : '';
    if (text !== null) {
      const pretty = prettify(text);
      return [
        h('div.wire__block-title.t-label', `Charge utile · texte${truncated}`),
        h('pre.wire__text.mono.scroll-y', pretty.length > MAX_TEXT_CHARS ? `${pretty.slice(0, MAX_TEXT_CHARS)}\n…` : pretty),
      ];
    }
    return [
      HexView({
        bytes,
        segments: Array.isArray(event.detail?.segments) ? event.detail.segments : [],
        title: `Octets sur le fil${truncated}`,
        bytesPerRow: 8,
        maxBytes: 128,
        maxHeight: 248,
      }),
    ];
  }
  const detail = Object.fromEntries(Object.entries(event.detail ?? {}).filter(([key]) => key !== 'segments'));
  if (!Object.keys(detail).length) return [h('p.wire__none', 'Cette étape ne transporte aucun octet.')];
  return [h('div.wire__block-title.t-label', 'Détail de l’étape'), h('pre.wire__text.mono.scroll-y', JSON.stringify(detail, null, 2))];
}

function details(event) {
  const info = appStore.get().catalog?.stages?.[event.stage];
  const payload = payloadBlock(event);
  const panel = h(
    'div.wire__details',
    info?.text ? h('p.wire__explain', info.text) : null,
    KeyValue({
      dense: true,
      items: [
        { label: 'Appel', value: event.callId, mono: true, copy: Boolean(event.callId) },
        { label: 'Étape', value: info?.label ? `${info.label} — ${info.role ?? event.side}` : event.stage },
        { label: 'Horodatage', value: fmtTime(event.ts, { ms: true }), mono: true },
        event.durationUs !== null ? { label: 'Durée', value: fmtUs(event.durationUs), mono: true } : null,
        event.size !== null ? { label: 'Taille', value: fmtBytes(event.size, { exact: true }), mono: true } : null,
      ].filter(Boolean),
    }),
    payload,
  );
  panel.dispose = () => payload.forEach((block) => block.destroy?.());
  return panel;
}

/* --- Rendu ------------------------------------------------------------------- */

function timeLabel(event) {
  if (event.isFirst || event.offsetMs === null) return fmtTime(event.ts);
  return `+${fmtMs(Math.max(0, event.offsetMs))}`;
}

function row(event) {
  const direction = directionOf(event);
  let panel = null;
  const button = h(
    'button.wire__row',
    {
      type: 'button',
      'aria-expanded': 'false',
      title: direction.title,
      onClick: () => {
        const open = button.getAttribute('aria-expanded') !== 'true';
        button.setAttribute('aria-expanded', String(open));
        if (open) {
          panel = details(event);
          item.appendChild(panel);
        } else {
          panel?.dispose();
          panel?.remove();
        }
      },
    },
    h('span.wire__dir', { dataset: { tone: direction.tone } }, icon(direction.icon, { size: 14, stroke: 2 })),
    ProtocolChip(event.protocol, { short: true, size: 'sm' }),
    h('span.wire__what', h('span.wire__stage.mono', event.stage), h('span.wire__method.truncate', event.method)),
    h('span.wire__size.num', event.size !== null ? fmtBytes(event.size) : ''),
    h('span.wire__time.num', { class: { 'wire__time--start': event.isFirst } }, timeLabel(event)),
  );
  const item = h('div.wire__item', { dataset: { first: String(event.isFirst) } }, button);
  return item;
}

function emptyState() {
  const { ws: socketState } = appStore.get();
  if (events.some(visible)) return null;
  if (events.length) {
    return EmptyState({ icon: 'filter', title: 'Aucun message pour ce filtre', text: 'Élargissez le filtre de protocole ou affichez les étapes internes.' });
  }
  if (socketState !== 'live') {
    return EmptyState({
      icon: 'unplug',
      title: 'Laboratoire hors ligne',
      text: 'Le flux des messages apparaîtra ici dès que le serveur du laboratoire sera joignable. La reconnexion est automatique.',
    });
  }
  return EmptyState({
    icon: 'radio',
    tone: 'accent',
    title: 'En écoute',
    text: 'Lancez un appel : chaque message échangé entre le stub et le squelette s’affichera ici, octet par octet.',
    action: Button({ label: 'Ouvrir la console RPC', icon: 'square-terminal', size: 'sm', onClick: () => navigate('console') }),
  });
}

function renderStatus() {
  if (!view) return;
  const { ws: socketState } = appStore.get();
  const shown = events.filter(visible).length;
  if (paused) view.status.set({ tone: 'warning', pulse: false, label: 'En pause' });
  else if (socketState === 'live') view.status.set({ tone: 'success', pulse: true, label: 'En direct' });
  else view.status.set({ tone: 'muted', pulse: false, label: 'Hors ligne' });
  view.count.textContent = `${fmtNumber(shown)} message${shown > 1 ? 's' : ''}`;
  view.pendingBadge.hidden = !(paused && pending.length);
  view.pendingBadge.setLabel(`${fmtNumber(pending.length)} en attente`);
}

function renderAll() {
  if (!view) return;
  pending = [];
  const empty = emptyState();
  clear(view.list, empty ?? events.filter(visible).map(row));
  view.list.dataset.empty = String(Boolean(empty));
  if (!empty) view.list.scrollTop = view.list.scrollHeight;
  renderStatus();
}

function flush() {
  flushFrame = 0;
  if (!view || paused || !pending.length) {
    renderStatus();
    return;
  }
  const list = view.list;
  if (list.dataset.empty === 'true') {
    renderAll();
    return;
  }
  const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
  const fragment = document.createDocumentFragment();
  for (const event of pending) fragment.appendChild(row(event));
  pending = [];
  list.appendChild(fragment);
  while (list.children.length > MAX_EVENTS) list.firstElementChild.remove();
  if (stick) list.scrollTop = list.scrollHeight;
  renderStatus();
}

function schedule() {
  if (!flushFrame) flushFrame = requestAnimationFrame(flush);
}

/* --- Cycle de vie -------------------------------------------------------------- */

function build() {
  const pause = IconButton({
    icon: 'pause',
    label: 'Mettre en pause',
    size: 'sm',
    onClick: () => {
      paused = !paused;
      pause.setIcon(paused ? 'play' : 'pause');
      pause.setLabel(paused ? 'Reprendre' : 'Mettre en pause');
      pause.setActive(paused);
      if (paused) renderStatus();
      else flush();
    },
  });
  pause.setActive(paused);
  if (paused) pause.setIcon('play');

  const list = h('div.wire__list.scroll-y', { role: 'log', 'aria-label': 'Messages sur le fil', 'aria-live': 'off' });
  const status = StatusDot({ size: 'sm' });
  const count = h('span.wire__count');
  const pendingBadge = Badge({ tone: 'warning', size: 'sm', label: '' });

  const toolbar = h(
    'div.wire__toolbar',
    Segmented({
      size: 'sm',
      value: filter,
      ariaLabel: 'Filtrer par protocole',
      options: [{ value: 'all', label: 'Tous' }, ...REMOTE_PROTOCOL_IDS.map((id) => ({ value: id, label: protocol(id).short, color: protocol(id).color }))],
      onChange: (value) => {
        filter = value;
        renderAll();
      },
    }),
    Toggle({
      size: 'sm',
      label: 'Étapes internes',
      checked: showInternal,
      onChange: (on) => {
        showInternal = on;
        renderAll();
      },
    }),
  );

  drawer = openDrawer({
    side: 'right',
    width: 440,
    modal: false,
    class: 'wiretap',
    icon: 'radio',
    title: 'Sous le capot',
    subtitle: 'Les messages sur le fil, en direct',
    actions: [pause, IconButton({ icon: 'trash-2', label: 'Effacer la liste', size: 'sm', onClick: () => wiretap.clear() })],
    body: [toolbar, list],
    footer: [
      h('div.wire__footer-left', status, count, pendingBadge),
      h('a.wire__link', { href: '#/xray' }, 'Analyse détaillée', icon('arrow-right', { size: 13 })),
    ],
    onClose: teardown,
  });
  view = { list, status, count, pendingBadge };
}

function teardown() {
  stops.forEach((stop) => stop());
  stops = [];
  cancelAnimationFrame(flushFrame);
  flushFrame = 0;
  drawer = null;
  view = null;
  pending = [];
  appStore.set({ wiretap: false });
}

/**
 * Tiroir « Sous le capot ». L'état d'ouverture est aussi dans `appStore` (`wiretap`).
 */
export const wiretap = Object.freeze({
  /** Ouvre le tiroir et commence l'écoute du sujet `trace`. */
  open() {
    if (drawer) return;
    build();
    stops = [
      ws.subscribe(['trace']),
      ws.on('trace', (message) => wiretap.push(message.event ?? message)),
      appStore.subscribe('ws', () => {
        if (view?.list.dataset.empty === 'true') renderAll();
        else renderStatus();
      }),
    ];
    renderAll();
    appStore.set({ wiretap: true });
  },

  /** Ferme le tiroir et arrête l'écoute. */
  close() {
    drawer?.close();
  },

  /** Ouvre ou ferme le tiroir. */
  toggle() {
    if (drawer) drawer.close();
    else wiretap.open();
  },

  /** @returns {boolean} Le tiroir est-il ouvert ? */
  isOpen() {
    return drawer !== null;
  },

  /**
   * Ajoute un évènement de trace à la liste (forme de `TraceEvent.to_dict()` : `call_id`, `protocol`,
   * `side`, `stage`, `method`, `size`, `duration_us`, `ts`, `t_ns`, `detail`, `payload_hex`).
   * Utilisé par le flux WebSocket ; une page peut aussi y verser les évènements d'un appel inspecté.
   * @param {Object} raw
   * @returns {void}
   */
  push(raw) {
    if (!raw || typeof raw !== 'object') return;
    const event = normalize(raw);
    events.push(event);
    if (events.length > MAX_EVENTS) {
      const dropped = events.splice(0, events.length - MAX_EVENTS);
      for (const old of dropped) if (old.isFirst) callStarts.delete(old.callId);
    }
    if (!view || !visible(event)) return;
    pending.push(event);
    schedule();
  },

  /** Vide la liste. */
  clear() {
    events.length = 0;
    callStarts.clear();
    renderAll();
  },
});
