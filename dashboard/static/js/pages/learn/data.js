/**
 * Bilan — accès aux données : chargement du bilan (`GET /api/summary`) et des éléments qui
 * l'éclairent, mise en forme des chiffres, lien de chaque argument vers la page où le reproduire.
 */

import { fmtNumber, fmtUs, splitUnit } from '../../core/format.js';
import { href } from '../../core/router.js';
import { routeInfo } from '../../core/routes.js';

/** Procédure de référence du bilan (`dashboard/summary.py`) : celle des lignes mesurées de la matrice. */
const REFERENCE_METHOD = 'get_product_details';

/** Page où chaque argument se reproduit : `[page, paramètres d'URL]` (scénario, protocole ou mode à ouvrir). */
const ITEM_TARGETS = Object.freeze({
  location_transparency: ['compare'],
  compact_messages: ['benchmark'],
  light_framing: ['benchmark'],
  speed: ['benchmark'],
  multiplexing: ['console', { method: 'get_product_details', mode: 'async' }],
  typed_contract: ['contract'],
  remote_cost: ['benchmark'],
  latency_trap: ['chaos', { scenario: 'latency_trap' }],
  unknown_outcome: ['chaos', { scenario: 'timeout_spike' }],
  partial_failure: ['chaos', { scenario: 'connection_cut' }],
  outage: ['chaos', { scenario: 'server_outage' }],
  duplicate_execution: ['chaos', { scenario: 'duplicate_execution' }],
  contract_coupling: ['contract'],
  opaque_messages: ['xray', { protocol: 'grpc' }],
});

/** Page de repli quand l'argument est inconnu : celle de l'expérience qui l'a chiffré. */
const SOURCE_PAGES = Object.freeze({ benchmark: 'benchmark', failures: 'chaos', contract: 'contract', calls: 'console', code: 'compare' });

/** Origine d'un chiffre, telle que la nomme `item.source`. */
export const SOURCE_LABELS = Object.freeze({
  benchmark: 'Banc d’essai',
  failures: 'Scénario de panne',
  contract: 'Scénarios de contrat',
  calls: 'Lot d’appels simultanés',
  code: 'Lu dans le code',
});

/**
 * @typedef {Object} BilanItem Argument du bilan (`advantages[]` / `drawbacks[]` de `/api/summary`).
 * @property {string} id
 * @property {string} title
 * @property {string} text
 * @property {{label: string, value: number, unit: string}|null} metric
 * @property {string} evidence
 * @property {string|null} source
 */

/**
 * Lien vers la page du laboratoire où un argument se reproduit.
 * @param {BilanItem} item
 * @returns {{page: string, label: string, icon: string, href: string}|null}
 */
export function targetOf(item) {
  const [page, query] = ITEM_TARGETS[item.id] ?? [SOURCE_PAGES[item.source]];
  return pageLink(page, query);
}

/**
 * Lien vers une page de l'application.
 * @param {string|null|undefined} page Identifiant de page (`'chaos'`).
 * @param {Record<string, string>} [query]
 * @returns {{page: string, label: string, icon: string, href: string}|null} `null` si la page n'existe pas.
 */
export function pageLink(page, query) {
  const info = page ? routeInfo(page) : null;
  return info ? { page: info.id, label: info.label, icon: info.icon, href: href(info.id, query) } : null;
}

/**
 * État d'un argument : chiffré par une expérience, en attente de mesure, ou simple constat.
 * @param {BilanItem} item
 * @returns {'measured'|'pending'|'qualitative'}
 */
export function stateOf(item) {
  if (item.metric) return 'measured';
  return !item.source && /^Pas encore/.test(item.evidence ?? '') ? 'pending' : 'qualitative';
}

/**
 * Met en forme le chiffre d'un argument : valeur en grand, unité à part (devant pour « × »).
 * @param {{value: number, unit: string}} metric
 * @returns {{prefix: string, value: string, unit: string}}
 */
export function formatMetric(metric) {
  const value = Number(metric.value);
  const magnitude = Math.abs(value);
  switch (metric.unit) {
    case 'ms':
      return { prefix: '', ...splitUnit(fmtUs(value * 1000)) };
    case '%':
      return { prefix: '', value: `${value < 0 ? '−' : ''}${fmtNumber(magnitude, { maxDecimals: 1 })}`, unit: '%' };
    case '×':
      return { prefix: '×', value: fmtNumber(value, { maxDecimals: magnitude >= 100 ? 0 : 1 }), unit: '' };
    default:
      return { prefix: '', value: fmtNumber(value, { maxDecimals: magnitude >= 100 ? 0 : 1 }), unit: metric.unit ?? '' };
  }
}

/**
 * Retrouve un argument par son identifiant, avantages et inconvénients confondus.
 * @param {Object|null} summary
 * @param {string} id
 * @returns {BilanItem|undefined}
 */
export function findItem(summary, id) {
  return [...(summary?.advantages ?? []), ...(summary?.drawbacks ?? [])].find((item) => item.id === id);
}

/**
 * Part des arguments chiffrés.
 * @param {BilanItem[]} items
 * @returns {{measured: number, total: number}}
 */
export function coverage(items) {
  return { measured: items.filter((item) => item.metric).length, total: items.length };
}

/**
 * Valeurs brutes d'une ligne « mesurée » de la matrice, relues dans le dernier rapport : elles
 * servent à tracer les barres, les valeurs affichées restant celles du bilan.
 * @param {string} criterion Libellé de la ligne.
 * @param {Object|null} report `GET /api/benchmark/latest`.
 * @returns {{values: Record<string, number>, best: string}|null} `best` qualifie le plus petit des protocoles distants.
 */
export function measuredSeries(criterion, report) {
  if (!report) return null;
  let values = null;
  let best = 'le plus léger';
  if (/fiche produit/i.test(criterion)) {
    const row = (report.payload?.rows ?? []).find((entry) => entry.method === REFERENCE_METHOD && entry.direction === 'response');
    values = row ? { custom: row.json_rpc_bytes, grpc: row.grpc_bytes, rest: row.rest_bytes } : null;
  } else if (/TCP/.test(criterion)) {
    values = Object.fromEntries((report.payload?.wire ?? []).map((entry) => [entry.protocol, entry.total_per_call]));
  } else if (/temps moyen/i.test(criterion)) {
    values = Object.fromEntries((report.latency?.results ?? []).map((entry) => [entry.protocol, entry.mean_ms]));
    best = 'le plus rapide à distance';
  }
  if (!values) return null;
  const finite = Object.fromEntries(Object.entries(values).filter(([, value]) => Number.isFinite(value) && value > 0));
  return Object.keys(finite).length ? { values: finite, best } : null;
}

/**
 * Charge le bilan et, sans jamais faire échouer l'ensemble, ce qui l'éclaire : dernier rapport
 * (valeurs brutes, conditions de mesure) et listes de scénarios (totaux « joués / existants »).
 * @param {{get: Function}} api
 * @param {{signal?: AbortSignal, known?: {failures?: Object[]|null, contract?: Object[]|null}}} [options]
 *   `known` : listes de scénarios déjà chargées, à ne pas redemander.
 * @returns {Promise<{summary: Object, report: Object|null, failures: Object[]|null, contract: Object[]|null}>}
 *   Rejette avec l'`ApiError` de `/api/summary` si le bilan lui-même est indisponible.
 */
export async function loadBilan(api, { signal, known = {} } = {}) {
  const optional = (request) => request.catch(() => null);
  const [summary, report, failures, contract] = await Promise.all([
    api.get('/api/summary', { signal }),
    optional(api.get('/api/benchmark/latest', { signal })),
    known.failures ? known.failures : optional(api.get('/api/failures/scenarios', { signal }).then((body) => body?.scenarios ?? null)),
    known.contract ? known.contract : optional(api.get('/api/contract', { signal }).then((body) => body?.scenarios ?? null)),
  ]);
  if (!summary || typeof summary !== 'object' || !Array.isArray(summary.advantages) || !Array.isArray(summary.drawbacks)) {
    throw new Error('Le laboratoire a renvoyé un bilan illisible.');
  }
  return { summary, report: report && typeof report === 'object' ? report : null, failures, contract };
}
