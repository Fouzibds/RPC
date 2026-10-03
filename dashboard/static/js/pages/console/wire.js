/**
 * Console RPC — lecture d'une trace (`GET /api/traces/{call_id}`) : les octets de la requête et de
 * la réponse (« Sur le fil ») et la cascade des étapes de l'appel (« Étapes »).
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtPercent, fmtUs } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { Waterfall } from '../../components/charts.js';
import { CodeBlock } from '../../components/codeblock.js';
import { HexView } from '../../components/hexview.js';
import { EmptyState, Segmented } from '../../components/ui.js';
import { Insight, strong } from './parts.js';

const TEXT_LANGUAGES = { rest: 'http', custom: 'json', grpc: 'protobuf' };

function findEvent(events, stage, { last = false, withPayload = false } = {}) {
  const matches = events.filter((event) => event.stage === stage && (!withPayload || event.payload_hex));
  return last ? matches[matches.length - 1] : matches[0];
}

function prettyJson(text) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

/** Texte lisible d'un message : le fil lui-même s'il est imprimable, sinon le message avant tramage. */
function readableText(wireEvent, marshalEvent, protocolId) {
  if (wireEvent?.payload_text) return { text: wireEvent.payload_text.replace(/\r\n/g, '\n'), language: TEXT_LANGUAGES[protocolId] ?? 'text', framed: true };
  const raw = marshalEvent?.payload_text || marshalEvent?.detail?.text;
  if (!raw) return null;
  return { text: protocolId === 'custom' ? prettyJson(raw) : raw.replace(/\n$/, ''), language: TEXT_LANGUAGES[protocolId] ?? 'text', framed: false };
}

function wireMeta(event) {
  const detail = event.detail ?? {};
  const headers = detail.http2_headers;
  if (headers?.[':path']) return `${headers[':method'] ?? 'POST'} ${headers[':path']} · HTTP/2`;
  if (headers?.[':status']) return `HTTP/2 ${headers[':status']}${detail.http2_trailers?.['grpc-status'] !== undefined ? ` · grpc-status ${detail.http2_trailers['grpc-status']}` : ''}`;
  if (detail.http?.method && detail.http?.status == null) return `${detail.http.method} ${detail.http.path} · HTTP/1.1`;
  if (detail.http?.status != null) return `HTTP/1.1 ${detail.http.status}`;
  if (detail.frame_header_hex) return `préfixe de trame 0x${detail.frame_header_hex}`;
  return '';
}

function Message({ title, iconName, event, readable, track }) {
  const body = h('div.console-wire__body');
  const size = event.size ?? (event.payload_hex?.length ?? 0) / 2;
  const captured = (event.payload_hex?.length ?? 0) / 2;
  const hex = track(HexView({ hex: event.payload_hex, segments: event.detail?.segments ?? [], title: event.stage, maxHeight: 248, maxBytes: 192 }));
  let text = null;
  const show = (view) => {
    if (view === 'text' && !text) {
      text = track(CodeBlock({ code: readable.text, language: readable.language, title: readable.framed ? 'Octets lus comme du texte' : 'Message avant tramage (vue lisible)', maxHeight: 248, wrap: true }));
    }
    clear(body, view === 'text' ? text : hex);
  };
  show('hex');
  const meta = wireMeta(event);
  return h(
    'section.console-wire__message',
    h(
      'header.console-wire__head',
      h('span.console-wire__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: 15 })),
      h('div.console-wire__titles', h('h4.console-wire__title', title), meta ? h('p.console-wire__meta.mono', meta) : null),
      size > captured ? h('span.console-wire__note', `aperçu : ${fmtBytes(captured)} sur ${fmtBytes(size)}`) : null,
      readable
        ? Segmented({ size: 'sm', value: 'hex', ariaLabel: `Affichage — ${title}`, options: [{ value: 'hex', label: 'Hex', icon: 'binary' }, { value: 'text', label: 'Texte', icon: 'type' }], onChange: show })
        : null,
    ),
    body,
  );
}

/**
 * Onglet « Sur le fil » : octets émis par le client, puis octets reçus en réponse.
 * @param {Object} props
 * @param {{summary: Object|null, events: Object[]}} props.trace Réponse de `GET /api/traces/{call_id}`.
 * @param {string} props.protocolId
 * @param {(node: T) => T} props.track Enregistre un composant à détruire au nettoyage.
 * @returns {HTMLElement}
 * @template T
 */
export function WirePanel({ trace, protocolId, track }) {
  const events = trace.events ?? [];
  const sent = findEvent(events, 'client.send', { withPayload: true });
  const received = findEvent(events, 'client.receive', { last: true, withPayload: true });
  if (!sent && !received) {
    return EmptyState({
      icon: protocolId === 'local' ? 'cpu' : 'binary',
      size: 'sm',
      title: protocolId === 'local' ? 'Rien ne circule sur le fil' : 'Aucun octet capturé pour cet appel',
      text:
        protocolId === 'local'
          ? 'Un appel local reste dans la mémoire du processus : ni sérialisation, ni trame, ni réseau. C’est toute la différence avec un appel distant.'
          : 'L’appel a échoué avant l’envoi, ou sa trace ne contient pas les messages bruts.',
    });
  }
  const requestBytes = sent?.size ?? 0;
  const responseBytes = received?.size ?? 0;
  const info = protocol(protocolId);
  return h(
    'div.console-wire',
    sent
      ? Message({ title: 'Requête — écrite par le client', iconName: 'arrow-up-right', event: sent, readable: readableText(sent, findEvent(events, 'client.marshal'), protocolId), track })
      : null,
    received
      ? Message({ title: 'Réponse — lue par le client', iconName: 'arrow-down-left', event: received, readable: readableText(received, findEvent(events, 'server.marshal', { last: true }), protocolId), track })
      : EmptyState({ icon: 'cloud-off', size: 'sm', title: 'Aucune réponse reçue', text: 'Le client n’a lu aucun octet en retour : échéance dépassée, connexion coupée ou refus avant l’envoi.' }),
    sent && received
      ? Insight([`Cet aller-retour ${info.short} pèse `, strong(fmtBytes(requestBytes + responseBytes, { exact: true })), ' sur le fil : ', strong(fmtBytes(requestBytes, { exact: true })), ' à l’aller, ', strong(fmtBytes(responseBytes, { exact: true })), ' au retour, tramage et en-têtes applicatifs compris.'], 'ruler')
      : null,
  );
}

/** Couloir d'une étape : les étapes d'écriture et de lecture appartiennent au transport. */
function laneOf(event) {
  if (/\.(send|receive)$/.test(event.stage) || event.side === 'network') return 'network';
  return event.side === 'server' ? 'server' : 'client';
}

/**
 * Étapes d'une trace au format de `Waterfall`. Un évènement est publié à la FIN de son étape :
 * le début est donc son décalage moins sa durée.
 * @param {Object[]} events Évènements (`offset_us`, `duration_us`, `stage`, `side`, `size`).
 * @param {Record<string, {label: string, role: string, text: string}>} stageInfo `catalog.stages`.
 * @returns {Array<import('../../components/charts/waterfall.js').WaterfallStage>}
 */
export function stagesOf(events, stageInfo = {}) {
  const origin = events[0]?.t_ns ?? 0;
  return events.map((event, index) => {
    const offset = event.offset_us ?? (event.t_ns - origin) / 1000;
    const duration = Number.isFinite(event.duration_us) ? event.duration_us : 0;
    const info = stageInfo[event.stage];
    const detail = [info?.text, Number.isFinite(event.size) ? `Message : ${fmtBytes(event.size, { exact: true })}.` : ''].filter(Boolean).join(' ');
    return { id: `${event.stage}#${index}`, label: event.stage, lane: laneOf(event), start: Math.max(0, offset - duration), duration, detail };
  });
}

/**
 * Onglet « Étapes » : cascade des étapes réelles de l'appel, avec leur durée mesurée.
 * @param {Object} props
 * @param {{summary: Object|null, events: Object[]}} props.trace
 * @param {Object} props.catalog `stages` et `pipeline` du catalogue.
 * @param {string} props.protocolId
 * @param {(node: T) => T} props.track
 * @returns {HTMLElement}
 * @template T
 */
export function StagesPanel({ trace, catalog, protocolId, track }) {
  const events = trace.events ?? [];
  const stages = stagesOf(events, catalog.stages);
  const total = trace.summary?.duration_us ?? Math.max(1, ...stages.map((stage) => stage.start + stage.duration));
  const pipeline = catalog.pipeline?.length ?? 12;
  const seen = new Set(events.map((event) => event.stage));
  const covered = (catalog.pipeline ?? []).filter((stage) => seen.has(stage)).length;
  const server = trace.summary?.server_us;
  const waterfall = track(Waterfall({ stages, total: Math.max(total, ...stages.map((stage) => stage.start + stage.duration)), format: fmtUs, ariaLabel: 'Étapes de l’appel et leur durée' }));

  let insight;
  if (protocolId === 'local') {
    insight = ['Appel local : ', strong(`${covered} étapes`), ` sur les ${pipeline} d’un appel distant. Ni marshalling, ni transport, ni squelette : la fonction est appelée directement.`];
  } else if (Number.isFinite(server) && total > 0) {
    insight = ['La procédure elle-même n’a occupé que ', strong(fmtUs(server)), ' sur ', strong(fmtUs(total)), ' : ', strong(fmtPercent(1 - server / total, { decimals: 0 })), ' du temps de cet appel est le coût du middleware et du transport.'];
  } else {
    insight = [strong(`${covered} étapes`), ` sur ${pipeline} ont été observées : l’appel s’est interrompu avant la fin du pipeline.`];
  }
  return h('div.console-stages', waterfall, Insight(insight, 'timer'));
}
