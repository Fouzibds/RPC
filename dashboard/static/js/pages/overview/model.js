/**
 * Vue d'ensemble — lecture des données du laboratoire : rien n'est affiché qui ne sorte d'ici,
 * c'est-à-dire de `/api/status`, du message `stats` ou d'un résumé d'appel.
 */

import { PROTOCOL_IDS, REMOTE_PROTOCOL_IDS } from '../../core/protocols.js';

/** Codes d'erreur produits par le transport ou le client : l'appel n'a pas atteint la procédure. */
const TRANSPORT_CODES = new Set(['TIMEOUT', 'UNAVAILABLE', 'CIRCUIT_OPEN', 'CANCELLED', 'PROTOCOL']);

const num = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0);

/**
 * Les trois serveurs comparés (les serveurs « contrat v2 » ne font pas partie du schéma).
 * @param {Object|null} status Réponse de `GET /api/status`.
 * @returns {Record<string, {id: string, port: number, proxy_port: number|null, up: boolean}>}
 */
export function serversById(status) {
  const out = {};
  for (const server of status?.servers ?? []) {
    if (REMOTE_PROTOCOL_IDS.includes(server.id)) out[server.id] = server;
  }
  return out;
}

/**
 * Cumuls depuis le démarrage, tous protocoles confondus.
 * @param {Object|null} status
 * @returns {{calls: number, errors: number, bytesOut: number, bytesIn: number, remoteCalls: number,
 *   remoteAvgMs: number|null, localAvgMs: number|null, errorRate: number|null}}
 *   `remoteAvgMs` : moyenne des appels distants pondérée par leur nombre ; `localAvgMs` : celle de
 *   l'appel local ; `null` tant qu'aucun appel n'a eu lieu.
 */
export function sumTotals(status) {
  const totals = status?.totals ?? {};
  let calls = 0;
  let errors = 0;
  let bytesOut = 0;
  let bytesIn = 0;
  let remoteCalls = 0;
  let remoteMs = 0;
  for (const id of PROTOCOL_IDS) {
    const row = totals[id] ?? {};
    calls += num(row.calls);
    errors += num(row.errors);
    bytesOut += num(row.bytes_out);
    bytesIn += num(row.bytes_in);
    if (id !== 'local') {
      remoteCalls += num(row.calls);
      remoteMs += num(row.calls) * num(row.avg_ms);
    }
  }
  const localCalls = num(totals.local?.calls);
  return {
    calls,
    errors,
    bytesOut,
    bytesIn,
    remoteCalls,
    remoteAvgMs: remoteCalls ? remoteMs / remoteCalls : null,
    localAvgMs: localCalls ? num(totals.local.avg_ms) : null,
    errorRate: calls ? errors / calls : null,
  };
}

/**
 * Débits de la dernière seconde (message `stats`), tous protocoles confondus.
 * @param {Object|null} stats Dernier message WebSocket `stats`.
 * @returns {{calls: number, errors: number, bytes: number, avgMs: number|null, byProtocol: Record<string, number>}}
 *   `avgMs` : durée moyenne des appels distants terminés pendant l'intervalle (`null` s'il n'y en a eu aucun).
 */
export function sumRates(stats) {
  const rates = stats?.rates ?? {};
  let calls = 0;
  let errors = 0;
  let bytes = 0;
  let weight = 0;
  let weighted = 0;
  const byProtocol = {};
  for (const id of PROTOCOL_IDS) {
    const row = rates[id] ?? {};
    const perSecond = num(row.calls_per_s);
    byProtocol[id] = perSecond;
    calls += perSecond;
    errors += num(row.errors_per_s);
    bytes += num(row.bytes_out_per_s) + num(row.bytes_in_per_s);
    if (id !== 'local' && row.avg_ms !== null && row.avg_ms !== undefined && perSecond > 0) {
      weight += perSecond;
      weighted += perSecond * num(row.avg_ms);
    }
  }
  return { calls, errors, bytes, avgMs: weight ? weighted / weight : null, byProtocol };
}

/**
 * Vrai si l'erreur vient du réseau ou du client (délai, panne, disjoncteur) et non de la procédure.
 * @param {{error?: {code?: string}|null}} call Résumé d'appel.
 * @returns {boolean}
 */
export function isTransportError(call) {
  return TRANSPORT_CODES.has(String(call?.error?.code ?? ''));
}

/**
 * Liste des résumés d'appels renvoyée par `GET /api/traces`, du plus récent au plus ancien.
 * @param {*} payload Corps de la réponse (`{traces: […]}`).
 * @returns {Object[]}
 */
export function tracesOf(payload) {
  const list = Array.isArray(payload) ? payload : (payload?.traces ?? []);
  return list
    .filter((item) => item && typeof item.call_id === 'string' && item.status !== 'pending')
    .sort((a, b) => num(b.started_at) - num(a.started_at));
}
