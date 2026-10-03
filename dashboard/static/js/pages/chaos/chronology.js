/**
 * Chaos réseau — « Chronologie en direct » : un couloir par protocole ; chaque tentative y
 * tombe sous forme de plage colorée par son issue, les attentes sous forme de trait fin et
 * les pannes du proxy sous forme de losanges. Alimentée par le WebSocket (`call`, `network`,
 * `resilience`) ; sans WebSocket, reconstituée à partir des réponses de `POST /api/call`.
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtMs } from '../../core/format.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { Timeline } from '../../components/charts.js';
import { Card, EmptyState, IconButton, Segmented } from '../../components/ui.js';
import { BREAKER_STATES, CHAOS_KINDS, STAGE_INFO, attemptKind } from './model.js';

const MAX_ITEMS = 500;
const HISTORY_MS = 120000;
const ACTIVE_GRACE_MS = 1200;
const BLACKHOLE_GAP_MS = 400;
const WINDOWS = [
  { value: 10000, label: '10\u202Fs' },
  { value: 30000, label: '30\u202Fs' },
  { value: 60000, label: '1\u202Fmin' },
];

/**
 * Carte « Chronologie en direct ».
 * @param {{store: Object, ws: Object}} ctx Contexte de la page.
 * @param {{onClear: () => void, footer?: HTMLElement}} hooks `footer` : pied de carte (télécommande du client).
 * @returns {{el: HTMLElement, setProtocol: (id: string) => void, setActivity: (active: boolean) => void, ingestResponse: (response: Object, sentAt: number) => void, clear: () => void, destroy: () => void}}
 */
export function Chronology(ctx, hooks) {
  const { store, ws } = ctx;
  const items = new Map();
  const attempts = new Map();
  const seen = new Set();
  const lastBlackhole = new Map();
  let selected = 'custom';
  let origin = null;
  let clockOffset = 0;
  let hasOffset = false;
  let windowMs = WINDOWS[0].value;
  let active = false;
  let lastEventAt = 0;
  let counter = 0;

  const timeline = Timeline({
    ariaLabel: 'Chronologie des tentatives d’appel, par protocole',
    kinds: CHAOS_KINDS,
    lanes: [],
    items: [],
    window: windowMs,
    minSpan: 2000,
    laneHeight: 44,
    format: (value) => fmtMs(value, { decimals: Math.abs(value) >= 1000 ? 1 : 0 }),
  });
  timeline.hidden = true;

  const empty = EmptyState({
    icon: 'activity',
    size: 'sm',
    title: 'Rien à l’horizon',
    text: 'Chaque tentative d’appel tombera ici, colorée par son issue : lancez un appel, une rafale ou un scénario guidé (Ctrl + Entrée pour un appel).',
  });

  const el = Card(
    {
      title: 'Chronologie en direct',
      subtitle: 'Plages : tentatives et attentes · losanges : pannes injectées et décisions du client',
      icon: 'activity',
      class: 'chaos-card chaos-card--timeline',
      footer: hooks.footer,
      actions: [
        Segmented({ size: 'sm', value: windowMs, ariaLabel: 'Fenêtre visible', options: WINDOWS, onChange: (value) => { windowMs = value; render(); } }),
        IconButton({ icon: 'trash-2', label: 'Effacer la chronologie et les jauges', size: 'sm', onClick: () => hooks.onClear?.() }),
      ],
    },
    h('div.chaos-timeline', { 'aria-live': 'off' }, empty, timeline),
  );

  /* --- Temps ------------------------------------------------------------------- */

  const serverNow = () => Date.now() + clockOffset;

  /** Position (ms) d'un horodatage serveur (secondes) sur l'axe de la chronologie. */
  function at(tsSeconds) {
    const epochMs = tsSeconds * 1000;
    const offset = epochMs - Date.now();
    if (!hasOffset || offset > clockOffset) {
      clockOffset = offset;
      hasOffset = true;
    }
    if (origin === null) origin = epochMs;
    return epochMs - origin;
  }

  /* --- Marques ------------------------------------------------------------------ */

  function lanes() {
    const servers = store.get().status?.servers ?? [];
    return REMOTE_PROTOCOL_IDS.filter((id) => id === selected || seen.has(id)).map((id) => {
      const port = servers.find((server) => server.id === id)?.proxy_port;
      return { id, label: protocol(id).short, sublabel: port ? `proxy :${port}` : undefined, protocol: id };
    });
  }

  function render() {
    const list = Array.from(items.values());
    const showing = list.length > 0;
    empty.hidden = showing;
    timeline.hidden = !showing;
    timeline.update({ lanes: lanes(), items: list, window: windowMs });
  }

  function prune() {
    if (items.size <= MAX_ITEMS / 2) return;
    let latest = 0;
    for (const item of items.values()) latest = Math.max(latest, item.end ?? item.start);
    for (const [id, item] of items) {
      if ((item.end ?? item.start) < latest - HISTORY_MS || items.size > MAX_ITEMS) items.delete(id);
    }
  }

  function put(item) {
    if (!REMOTE_PROTOCOL_IDS.includes(item.lane)) return;
    seen.add(item.lane);
    items.set(item.id, item);
    lastEventAt = Date.now();
    prune();
    render();
  }

  function attemptItem({ id, lane, start, end, ok, code, method, message, bytes }) {
    const info = attempts.get(id);
    const n = info?.n ?? 1;
    const facts = [code];
    if (!ok && message) facts.push(message);
    if (ok && bytes) facts.push(bytes);
    return {
      id,
      lane,
      start,
      end,
      kind: attemptKind({ ok, code, durationMs: end - start, attempt: n }),
      label: n > 1 ? String(n) : undefined,
      title: info ? `${method} · tentative n° ${n}${info.max ? `/${info.max}` : ''}` : method,
      detail: facts.join(' — '),
      meta: { ok, code, method, message, bytes },
    };
  }

  function marker(lane, tsSeconds, kind, title, detail) {
    counter += 1;
    put({ id: `m${counter}`, lane, start: at(tsSeconds), kind, title, detail });
  }

  /* --- Flux temps réel ----------------------------------------------------------- */

  function onCall(message) {
    if (!REMOTE_PROTOCOL_IDS.includes(message.protocol) || message.status === 'pending' || message.started_at == null) return;
    const start = at(message.started_at);
    const sizes = message.request_bytes != null && message.response_bytes != null ? `${fmtBytes(message.request_bytes)} → ${fmtBytes(message.response_bytes)}` : '';
    put(
      attemptItem({
        id: message.call_id,
        lane: message.protocol,
        start,
        end: start + (message.duration_us ?? 0) / 1000,
        ok: message.status === 'ok',
        code: message.error?.code ?? 'OK',
        method: message.method,
        message: message.error?.message,
        bytes: sizes,
      }),
    );
  }

  function onResilience(message) {
    const event = message.event;
    if (!event) return;
    const detail = event.detail ?? {};
    const lane = event.protocol;
    if (event.stage === 'resilience.attempt') {
      const id = event.call_id || `a${(counter += 1)}`;
      attempts.set(id, { n: detail.n, max: detail.max_attempts });
      const known = items.get(id);
      const end = known?.end ?? at(event.ts);
      const start = known?.start ?? end - Number(detail.duration_ms ?? 0);
      put(attemptItem({ id, lane, start, end, ok: detail.outcome === 'ok', code: detail.code, method: event.method, message: detail.message, bytes: known?.meta?.bytes }));
    } else if (event.stage === 'resilience.backoff') {
      const start = at(event.ts);
      counter += 1;
      put({ id: `b${counter}`, lane, start, end: start + Number(detail.backoff_ms ?? 0), kind: 'backoff', title: `Attente avant la tentative n° ${detail.next_attempt}`, detail: `Après ${detail.code} : le client patiente avant de réessayer.` });
    } else if (event.stage === 'resilience.breaker') {
      const kind = detail.to === 'open' ? 'breaker_open' : detail.to === 'half_open' ? 'breaker_half' : 'breaker_closed';
      marker(lane, event.ts, kind, `Disjoncteur : ${BREAKER_STATES[detail.to]?.label.toLowerCase() ?? detail.to}`, detail.text);
    } else if (event.stage === 'resilience.dedup') {
      marker(lane, event.ts, 'dedup', 'Rejeu reconnu par le serveur', detail.text);
    } else if (event.stage === 'resilience.give_up') {
      marker(lane, event.ts, detail.reason === 'circuit_open' ? 'refused' : 'give_up', detail.reason === 'circuit_open' ? 'Appel refusé sans toucher au réseau' : 'Le client renonce', detail.text);
    }
  }

  function onNetwork(message) {
    const event = message.event;
    const info = STAGE_INFO[event?.stage];
    if (!event || !info) return;
    const detail = event.detail ?? {};
    if (event.stage === 'network.delay') {
      if (detail.spike) marker(event.protocol, event.ts, 'net_spike', 'Pic de latence', `La requête est retenue ${fmtMs(detail.delay_ms)} par le réseau.`);
      return;
    }
    if (event.stage === 'network.blackhole') {
      const previous = lastBlackhole.get(event.protocol) ?? 0;
      if (Date.now() - previous < BLACKHOLE_GAP_MS) return;
      lastBlackhole.set(event.protocol, Date.now());
    }
    marker(event.protocol, event.ts, info.kind, info.label, detail.text);
  }

  /* --- Repli sans WebSocket -------------------------------------------------------- */

  function ingestResponse(response, sentAt) {
    if (ws.state === 'live' || !REMOTE_PROTOCOL_IDS.includes(response?.protocol)) return;
    if (origin === null) origin = sentAt + clockOffset;
    let cursor = sentAt + clockOffset - origin;
    const list = response.mode === 'async' ? (response.calls ?? []).map((call, index) => ({ n: 1, call_id: call.call_id || `r${sentAt}-${index}`, outcome: call.ok ? 'ok' : 'error', code: call.error?.code ?? 'OK', duration_ms: call.duration_ms, backoff_ms: 0, parallel: true })) : (response.attempts ?? []);
    for (const attempt of list) {
      const id = attempt.call_id || `r${sentAt}-${attempt.n}`;
      const start = attempt.parallel ? sentAt + clockOffset - origin : cursor;
      const end = start + Number(attempt.duration_ms ?? 0);
      if (response.mode !== 'async' && list.length > 1) attempts.set(id, { n: attempt.n, max: null });
      put(attemptItem({ id, lane: response.protocol, start, end, ok: attempt.outcome === 'ok', code: attempt.code, method: response.method, message: response.error?.message }));
      cursor = end;
      if (attempt.backoff_ms > 0) {
        put({ id: `${id}-wait`, lane: response.protocol, start: cursor, end: cursor + attempt.backoff_ms, kind: 'backoff', title: `Attente avant la tentative n° ${attempt.n + 1}` });
        cursor += attempt.backoff_ms;
      }
    }
    if (response.mode !== 'async' && !list.length && response.error) {
      counter += 1;
      put({ id: `m${counter}`, lane: response.protocol, start: cursor, kind: 'refused', title: 'Appel refusé sans toucher au réseau', detail: response.error.message });
    }
  }

  /* --- Vie ---------------------------------------------------------------------- */

  const ticker = window.setInterval(() => {
    const lively = active || Date.now() - lastEventAt < ACTIVE_GRACE_MS;
    if (!items.size || (!lively && timeline.state.now === null)) return;
    timeline.setNow(lively && origin !== null ? serverNow() - origin : null);
  }, 160);

  const offs = [ws.on('call', onCall), ws.on('resilience', onResilience), ws.on('network', onNetwork), store.subscribe((state) => state.status?.servers ?? null, () => render())];
  render();

  return {
    el,
    setProtocol(id) {
      selected = id;
      render();
    },
    setActivity(on) {
      active = on;
    },
    ingestResponse,
    clear() {
      items.clear();
      attempts.clear();
      seen.clear();
      origin = null;
      render();
    },
    destroy() {
      window.clearInterval(ticker);
      for (const off of offs) off();
      timeline.destroy();
      clear(el.body);
    },
  };
}
