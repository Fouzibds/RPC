/**
 * Sous le capot — modèle. Transforme une trace brute (`POST /api/inspect`, `GET /api/traces/{id}`)
 * en structures prêtes à afficher : étapes du pipeline, chronologie, décomposition du temps,
 * messages sur le fil. Aucun accès au DOM ; chaque nombre vient de la trace.
 */

/** Étapes dont le temps appartient au transport : écriture et attente sur la connexion. */
const TRANSPORT_STAGES = new Set(['client.send', 'client.receive', 'server.receive', 'server.send']);

/** Étapes terminales : leur durée est celle de l'appel entier. */
const TERMINAL_STAGES = new Set(['client.return', 'client.error']);

/** Libellés des étapes hors pipeline nominal, absentes du catalogue. */
const EXTRA_LABELS = { 'server.error': 'Erreur levée côté serveur' };

const durationOf = (event) => (Number.isFinite(event?.duration_us) ? event.duration_us : 0);

/**
 * Couloir d'une étape dans la chronologie : stub client, transport et réseau, ou serveur.
 * @param {{stage: string, side: string}} event
 * @returns {'client'|'network'|'server'}
 */
export function laneOf(event) {
  if (TRANSPORT_STAGES.has(event.stage) || event.side === 'network') return 'network';
  return event.side === 'server' ? 'server' : 'client';
}

/**
 * Découpe d'un message sur le fil, tel que publié par `*.send` / `*.receive`.
 * @param {Object|null|undefined} event
 * @returns {{stage: string, hex: string, size: number, segments: Object[], text: string|null, truncated: boolean,
 *   headers: Object|null, trailers: Object|null, http: Object|null, messageType: string|null, note: string|null}|null}
 */
function wireOf(event) {
  if (!event || !event.payload_hex) return null;
  const detail = event.detail ?? {};
  return {
    stage: event.stage,
    hex: event.payload_hex,
    size: event.size ?? event.payload_hex.length / 2,
    segments: detail.segments ?? [],
    text: event.payload_text ?? null,
    truncated: Boolean(detail.payload_truncated),
    headers: detail.http2_headers ?? null,
    trailers: detail.http2_trailers ?? null,
    http: detail.http ?? null,
    messageType: detail.message_type ?? null,
    note: detail.note ?? null,
  };
}

/**
 * @typedef {Object} Step
 * @property {number} index Rang dans le pipeline (0 = `client.call`).
 * @property {string} slot Étape canonique occupée dans le schéma.
 * @property {string} stage Étape réellement observée (`client.error` à la place de `client.return`…).
 * @property {Object|null} event Évènement de trace, ou `null` si l'étape n'a pas eu lieu.
 * @property {{label: string, role: string, text: string}} info Libellé, rôle et explication du catalogue.
 * @property {'ok'|'failed'|'error'|'skipped'} state `failed` : point de rupture ; `error` : étape qui
 *   transporte l'erreur ; `skipped` : étape jamais atteinte.
 */

/**
 * Construit le modèle d'un appel à partir de sa trace.
 * @param {Object} raw Trace de `/api/inspect` (`protocol, ok, result, error, call_id, summary, events`)
 *   ou de `/api/traces/{id}` (`summary, events`).
 * @param {{pipeline: string[], stages: Record<string, {label: string, role: string, text: string}>}} catalog
 * @returns {Object} Modèle de l'appel (voir les champs renvoyés).
 */
export function buildModel(raw, catalog) {
  const summary = raw.summary ?? {};
  const origin = raw.events?.[0]?.t_ns ?? 0;
  const events = (raw.events ?? []).map((event) => ({
    ...event,
    detail: event.detail ?? {},
    offset_us: event.offset_us ?? (event.t_ns - origin) / 1000,
  }));
  const byStage = new Map();
  for (const event of events) if (!byStage.has(event.stage)) byStage.set(event.stage, event);

  const serverError = byStage.get('server.error') ?? null;
  const clientError = byStage.get('client.error') ?? null;
  const infoOf = (stage) => catalog.stages?.[stage] ?? { label: EXTRA_LABELS[stage] ?? stage, role: '', text: '' };

  /** @type {Step[]} */
  const steps = catalog.pipeline.map((slot, index) => {
    let stage = slot;
    let event = byStage.get(slot) ?? null;
    if (slot === 'client.return' && !event && clientError) {
      stage = 'client.error';
      event = clientError;
    }
    // REST : quand la procédure échoue, « server.error » tient lieu d'exécution.
    if (slot === 'server.execute' && !event && serverError) event = serverError;
    return { index, slot, stage, event, info: infoOf(stage), state: event ? 'ok' : 'skipped' };
  });

  const ok = raw.ok ?? (summary.status ? summary.status === 'ok' : !clientError);
  const error = raw.error ?? summary.error ?? (clientError ? clientError.detail : null);
  let failure = null;
  if (!ok || clientError) {
    const terminal = steps.length - 1;
    let at = serverError ? steps.findIndex((step) => step.slot === 'server.execute') : -1;
    const kind = at >= 0 ? 'server' : null;
    if (at < 0) for (let i = terminal - 1; i >= 0 && at < 0; i -= 1) if (steps[i].event) at = i;
    const sent = steps.findIndex((step) => step.slot === 'client.send');
    failure = {
      at,
      kind: kind ?? (at >= sent && sent >= 0 ? 'transport' : 'client'),
      code: error?.code ?? '',
      message: error?.message ?? '',
      skipped: steps.filter((step) => !step.event).map((step) => step.index),
      serverDetail: serverError?.detail ?? null,
    };
    for (const step of steps) {
      if (!step.event) continue;
      if (step.index === at) step.state = 'failed';
      else if (step.index > at) step.state = 'error';
    }
  }

  const terminalEvent = byStage.get('client.return') ?? clientError;
  const protocol = raw.protocol ?? summary.protocol ?? events[0]?.protocol ?? 'local';
  return {
    protocol,
    ok: Boolean(ok),
    result: raw.result,
    error,
    callId: raw.call_id || summary.call_id || events[0]?.call_id || '',
    method: summary.method ?? events[0]?.method ?? '',
    summary,
    events,
    byStage,
    steps,
    reach: steps.filter((step) => step.event),
    failure,
    totalUs: summary.duration_us ?? durationOf(terminalEvent),
    request: wireOf(byStage.get('client.send') ?? byStage.get('server.receive')),
    response: wireOf(byStage.get('client.receive') ?? byStage.get('server.send')),
    infoOf,
  };
}

/**
 * Lignes de la cascade : une par évènement, dans l'ordre d'émission. Un évènement est en général
 * publié à la FIN de son étape, qui a donc commencé `durée` plus tôt. Exception : un envoi est
 * annoncé juste AVANT l'écriture (pour rester causal vis-à-vis du serveur) ; son début calculé
 * précéderait alors l'étape précédente du même processus, et il est ramené à la fin de celle-ci.
 * @param {Object} model
 * @returns {{stages: Object[], total: number}} Étapes au format `Waterfall` et durée représentée (µs).
 */
export function buildTimeline(model) {
  const published = { client: 0, server: 0 };
  const stages = model.events.map((event) => {
    const chain = event.side === 'server' ? 'server' : 'client';
    const terminal = TERMINAL_STAGES.has(event.stage);
    const duration = terminal ? 0 : durationOf(event);
    const start = duration > 0 ? Math.max(event.offset_us - duration, published[chain]) : Math.max(0, event.offset_us);
    published[chain] = event.offset_us;
    const canonical = model.byStage.get(event.stage) === event;
    return {
      id: canonical ? event.stage : `${event.stage}#${event.seq}`,
      stage: event.stage,
      label: model.infoOf(event.stage).label,
      lane: laneOf(event),
      start,
      duration,
      size: event.size ?? null,
      terminal,
    };
  });
  const end = Math.max(0, ...stages.map((stage) => stage.start + stage.duration));
  return { stages, total: Math.max(model.totalUs || 0, end) };
}

/**
 * Décomposition du temps de l'appel. « Réseau et transport » = l'attente du client, de la fin de
 * son marshalling à la réception de la réponse, moins le travail propre du serveur (démarshalling,
 * dispatch, exécution, marshalling). Cette fenêtre se lit sur les horodatages des évènements : elle
 * vaut pour les trois protocoles, dont les étapes d'envoi ne chronomètrent pas la même chose.
 * Le reliquat est le temps passé dans le stub avant et après, plus le dispatch.
 * Un appel local n'a ni stub ni réseau : toute sa durée est celle de la procédure.
 * @param {Object} model
 * @returns {{total: number, serialization: number, deserialization: number, execution: number, network: number,
 *   other: number, outside: number|null, answered: boolean}} Durées en µs ; `outside` = part hors procédure.
 */
export function buildBreakdown(model) {
  const d = (stage) => durationOf(model.byStage.get(stage));
  const total = model.totalUs || 0;
  if (model.protocol === 'local') {
    return { total, serialization: 0, deserialization: 0, execution: total, network: 0, other: 0, answered: false, outside: total > 0 ? 0 : null };
  }
  const serialization = d('client.marshal') + d('server.marshal');
  const deserialization = d('server.unmarshal') + d('client.unmarshal');
  const execution = model.byStage.has('server.execute') ? d('server.execute') : d('server.error');
  const serverWork = d('server.unmarshal') + d('server.dispatch') + execution + d('server.marshal');
  const marshalled = model.byStage.get('client.marshal') ?? model.byStage.get('client.call');
  const answer = model.byStage.get('client.receive') ?? model.byStage.get('client.error') ?? model.byStage.get('client.return');
  const waited = marshalled && answer ? answer.offset_us - marshalled.offset_us : 0;
  const network = Math.max(0, Math.min(waited, total) - serverWork);
  const other = Math.max(0, total - serialization - deserialization - execution - network);
  return {
    total,
    serialization,
    deserialization,
    execution,
    network,
    other,
    answered: model.byStage.has('client.receive'),
    outside: total > 0 ? Math.max(0, 1 - execution / total) : null,
  };
}

/**
 * Part d'octets lisibles (ASCII imprimable, tabulation, retour à la ligne) d'un message.
 * @param {string} hex
 * @returns {number} Entre 0 et 1 (0 pour un message vide).
 */
export function printableShare(hex) {
  const count = Math.floor((hex ?? '').length / 2);
  if (!count) return 0;
  let readable = 0;
  for (let i = 0; i < count; i += 1) {
    const byte = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    if ((byte >= 0x20 && byte <= 0x7e) || byte === 0x09 || byte === 0x0a || byte === 0x0d) readable += 1;
  }
  return readable / count;
}

/**
 * Nature de chaque octet d'un message, d'après ses segments (`raw` hors de tout segment).
 * @param {{size: number, segments: Object[]}|null} message
 * @returns {string[]}
 */
export function byteKinds(message) {
  if (!message) return [];
  const kinds = new Array(message.size).fill('raw');
  for (const segment of message.segments) {
    const end = Math.min(message.size, segment.end);
    for (let i = Math.max(0, segment.start); i < end; i += 1) kinds[i] = segment.kind;
  }
  return kinds;
}

/**
 * Texte d'un message à partir de ses octets, entre deux positions (UTF-8, tolérant).
 * @param {string} hex
 * @param {number} [start=0]
 * @param {number} [end]
 * @returns {string}
 */
export function decodeText(hex, start = 0, end = hex.length / 2) {
  const bytes = new Uint8Array(Math.max(0, end - start));
  for (let i = 0; i < bytes.length; i += 1) bytes[i] = parseInt(hex.slice((start + i) * 2, (start + i) * 2 + 2), 16);
  return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
}

/**
 * Analyse un texte JSON sans lever d'exception.
 * @param {string|null|undefined} text
 * @returns {{ok: boolean, value: *}}
 */
export function parseJson(text) {
  if (typeof text !== 'string' || !text.trim()) return { ok: false, value: undefined };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false, value: undefined };
  }
}

/**
 * Écrit une valeur JSON comme un littéral Python (`True`, `None`, chaînes entre guillemets).
 * @param {*} value
 * @returns {string}
 */
export function pyRepr(value) {
  if (value === null || value === undefined) return 'None';
  if (value === true) return 'True';
  if (value === false) return 'False';
  if (typeof value === 'string') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(pyRepr).join(', ')}]`;
  if (typeof value === 'object') return `{${Object.entries(value).map(([key, item]) => `${JSON.stringify(key)}: ${pyRepr(item)}`).join(', ')}}`;
  return String(value);
}

/**
 * Appel Python équivalent : `update_stock(product_id="SKU-1001", delta=-3)`.
 * @param {string} name Nom de la fonction (ou chemin pointé).
 * @param {Array} [args]
 * @param {Object} [kwargs]
 * @returns {string}
 */
export function pyCall(name, args = [], kwargs = {}) {
  const parts = [...args.map(pyRepr), ...Object.entries(kwargs).map(([key, value]) => `${key}=${pyRepr(value)}`)];
  const inline = `${name}(${parts.join(', ')})`;
  return inline.length <= 72 || parts.length < 2 ? inline : `${name}(\n${parts.map((part) => `    ${part},`).join('\n')}\n)`;
}
