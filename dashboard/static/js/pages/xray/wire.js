/**
 * Sous le capot — « Les octets sur le fil » : la requête et la réponse telles qu'elles circulent.
 * En haut, les octets (hexadécimal coloré par segment, ou texte brut) ; en dessous, leur lecture :
 * champs Protobuf et en-têtes HTTP/2 pour gRPC, préfixe de longueur puis JSON pour le RPC maison,
 * ligne de requête, en-têtes et corps pour REST.
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtPercent } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { CodeBlock } from '../../components/codeblock.js';
import { HexView, SegmentList } from '../../components/hexview.js';
import { Badge, Card, EmptyState, KeyValue, Segmented } from '../../components/ui.js';
import { ProtocolSwitch } from './timeline.js';
import { decodeText, parseJson, printableShare } from './model.js';

/** Seuil au-delà duquel un message est considéré comme du texte lisible. */
const TEXT_THRESHOLD = 0.9;

const hexPairs = (hex, start, end) => (hex.slice(start * 2, end * 2).match(/../g) ?? []).join(' ');

/**
 * Lisibilité d'un message d'après ses octets.
 * @param {{hex: string}|null} message
 * @returns {{share: number, text: boolean}}
 */
export function readability(message) {
  const share = message ? printableShare(message.hex) : 0;
  return { share, text: share >= TEXT_THRESHOLD };
}

/** Octets rendus comme du texte : chaque segment garde sa teinte, les octets non imprimables deviennent « · ». */
function rawText(message) {
  const segments = message.segments.length ? message.segments : [{ start: 0, end: message.hex.length / 2, kind: 'raw', label: 'Message' }];
  return h(
    'pre.xr-raw.scroll-y',
    { tabIndex: 0, 'aria-label': 'Octets lus comme du texte' },
    segments.map((segment) => {
      const chars = [];
      for (let i = segment.start; i < Math.min(segment.end, message.hex.length / 2); i += 1) {
        const byte = parseInt(message.hex.slice(i * 2, i * 2 + 2), 16);
        if (byte === 0x0a) chars.push('␊\n');
        else if (byte === 0x0d) chars.push('␍');
        else chars.push(byte >= 0x20 && byte <= 0x7e ? String.fromCharCode(byte) : '·');
      }
      return h('span.xr-raw__segment', { dataset: { kind: segment.kind }, title: segment.label, textContent: chars.join('') });
    }),
  );
}

/**
 * Section « Les octets sur le fil ».
 * @param {{onProtocol: (protocolId: string) => void}} props
 * @returns {HTMLElement & {update: (model: Object, protocols: string[]) => void, destroy: () => void}}
 */
export function Wire({ onProtocol }) {
  let model = null;
  let mode = 'hex';
  let owned = [];
  const protocolSwitch = ProtocolSwitch({ onChange: onProtocol });
  const modeSwitch = Segmented({
    options: [
      { value: 'hex', label: 'Hexadécimal', icon: 'binary' },
      { value: 'text', label: 'Texte', icon: 'type' },
    ],
    value: mode,
    size: 'sm',
    ariaLabel: 'Affichage des octets',
    onChange: (value) => {
      mode = value;
      render();
    },
  });
  const columns = h('div.xr-wires');
  const el = Card(
    {
      title: 'Requête et réponse',
      subtitle: 'Les octets exacts écrits sur la connexion, puis leur lecture segment par segment.',
      actions: [protocolSwitch, modeSwitch],
    },
    columns,
  );

  const own = (component) => {
    owned.push(component);
    return component;
  };

  /** Lecture d'un message : tableau des champs pour Protobuf, blocs de texte par segment sinon. */
  function reading(message, view) {
    const blocks = [];
    if (message.segments.some((segment) => segment.kind === 'tag')) {
      const list = own(SegmentList({ hex: message.hex, segments: message.segments, maxHeight: 340, onHover: (indices) => view?.highlight(indices) }));
      view?.update({ onSegmentHover: (_, index) => list.highlight(index) });
      blocks.push(h('div.xr-reading__block', h('span.t-label', `Champs Protobuf${message.messageType ? ` · ${message.messageType}` : ''}`), list));
    } else {
      const frames = message.segments.filter((segment) => segment.kind === 'frame');
      if (frames.length) {
        blocks.push(
          KeyValue({
            dense: true,
            items: frames.map((segment) => ({
              label: segment.label,
              value: `${hexPairs(message.hex, segment.start, segment.end)}${segment.value !== undefined ? ` → ${segment.value}` : ''}`,
              mono: true,
            })),
          }),
        );
      }
      for (const segment of message.segments.filter((item) => item.kind !== 'frame')) {
        const text = decodeText(message.hex, segment.start, segment.end);
        const parsed = segment.kind === 'body' ? parseJson(text) : { ok: false };
        const title = `${segment.label} · ${fmtBytes(segment.end - segment.start, { exact: true })}${parsed.ok ? ' · indenté pour la lecture' : ''}`;
        blocks.push(
          own(
            CodeBlock({
              title,
              code: parsed.ok ? JSON.stringify(parsed.value, null, 2) : text,
              language: parsed.ok ? 'json' : segment.kind === 'header' ? 'http' : 'text',
              lineNumbers: false,
              wrap: true,
              maxHeight: 280,
            }),
          ),
        );
      }
    }
    const headers = [
      ...Object.entries(message.headers ?? {}).map(([key, value]) => ({ label: key, value: String(value), mono: true })),
      ...Object.entries(message.trailers ?? {}).map(([key, value]) => ({ label: `${key} (trailer)`, value: String(value), mono: true })),
    ];
    if (headers.length) {
      blocks.push(h('div.xr-reading__block', h('span.t-label', 'En-têtes HTTP/2 · transmis hors du message, compressés (HPACK)'), KeyValue({ dense: true, items: headers })));
    }
    if (message.truncated) blocks.push(h('p.xr-detail__note', 'Capture tronquée : le message réel est plus long que la limite de capture de la trace.'));
    return h('div.xr-reading', blocks);
  }

  function missing(role) {
    const trailers = model.failure?.serverDetail?.http2_trailers;
    if (role === 'response' && trailers) {
      return h(
        'div.xr-msg__empty',
        EmptyState({
          icon: 'circle-slash',
          size: 'sm',
          tone: 'danger',
          title: 'Aucun message de réponse',
          text: 'gRPC ne sérialise rien en cas d’échec : l’erreur revient dans les trailers HTTP/2 qui ferment le flux.',
        }),
        KeyValue({ dense: true, items: Object.entries(trailers).map(([key, value]) => ({ label: key, value: String(value), mono: true })) }),
      );
    }
    return EmptyState({
      icon: 'circle-slash',
      size: 'sm',
      title: role === 'request' ? 'Aucune requête émise' : 'Aucune réponse reçue',
      text: role === 'request' ? 'L’appel n’a écrit aucun octet sur la connexion.' : 'L’appel s’est terminé sans qu’un message de réponse soit lu.',
    });
  }

  function column(role, message) {
    const heading = role === 'request' ? 'Requête' : 'Réponse';
    const arrow = icon(role === 'request' ? 'arrow-right' : 'arrow-left', { size: 14 });
    if (!message) return h('section.xr-msg', h('header.xr-msg__head', arrow, h('h4.xr-msg__title', heading)), missing(role));
    const read = readability(message);
    const view =
      mode === 'hex'
        ? own(HexView({ hex: message.hex, segments: message.segments, title: `Publié par ${message.stage}`, maxBytes: 256, maxHeight: 300 }))
        : null;
    return h(
      'section.xr-msg',
      h(
        'header.xr-msg__head',
        arrow,
        h('h4.xr-msg__title', heading),
        h('span.xr-msg__size.num', fmtBytes(message.size, { exact: true })),
        Badge({ label: read.text ? 'Texte' : 'Binaire', tone: read.text ? 'info' : 'accent', size: 'sm' }),
        h('span.xr-msg__share', `${fmtPercent(read.share, { decimals: 0 })} d’octets imprimables`),
      ),
      h('div.xr-msg__body', h('div.xr-msg__bytes', view ?? rawText(message)), reading(message, view)),
    );
  }

  function render() {
    owned.forEach((component) => component.destroy?.());
    owned = [];
    if (!model) return;
    clear(columns, column('request', model.request), column('response', model.response));
  }

  el.update = (next, protocols) => {
    model = next;
    protocolSwitch.set(protocols, model.protocol);
    render();
  };
  el.destroy = () => {
    owned.forEach((component) => component.destroy?.());
    owned = [];
  };
  return el;
}
