/**
 * Banc d'essai — modèle de la page : suites, préréglages, formats d'affichage et petites
 * fonctions pures qui lisent un rapport (`benchmark_perf.run_full_benchmark`). Aucune mesure
 * n'est écrite ici : tout nombre affiché vient du rapport ou se calcule à partir de lui.
 */

import { fmtMs, fmtNumber, fmtUs } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';

const NNBSP = ' ';
const DASH = '—';

/**
 * @typedef {Object} SuiteInfo
 * @property {'payload'|'serialization'|'latency'|'network'} id Nom de la suite côté serveur.
 * @property {string} label Libellé court (interrupteurs, étapes de progression).
 * @property {string} title Titre de la section de résultats.
 * @property {string} anchor Identifiant de l'ancre de la section.
 * @property {string} icon
 * @property {string} question La question à laquelle la suite répond.
 * @property {string} method Comment la mesure est prise.
 */

/** @type {ReadonlyArray<SuiteInfo>} Les quatre suites, dans l'ordre où le serveur les exécute. */
export const SUITES = Object.freeze([
  {
    id: 'payload',
    label: 'Tailles',
    title: 'Taille des messages',
    anchor: 'bench-payload',
    icon: 'ruler',
    question: 'Combien d’octets pour dire la même chose ?',
    method: 'Un appel tracé par procédure et par protocole, puis les compteurs des proxys : ce qui a vraiment traversé TCP.',
  },
  {
    id: 'serialization',
    label: 'Sérialisation',
    title: 'Sérialisation',
    anchor: 'bench-serialization',
    icon: 'binary',
    question: 'Que coûte la traduction en octets ?',
    method: 'Encodage et décodage des mêmes données en JSON et en Protobuf, sans aucun réseau.',
  },
  {
    id: 'latency',
    label: 'Latence',
    title: 'Latence',
    anchor: 'bench-latency',
    icon: 'timer',
    question: 'Combien de temps dure un appel ?',
    method: 'La même boucle d’appels sur chaque protocole : bus de traces coupé, connexion ouverte, après échauffement.',
  },
  {
    id: 'network',
    label: 'Réseau',
    title: 'Local vs distant',
    anchor: 'bench-network',
    icon: 'trending-up',
    question: 'Et quand le serveur s’éloigne ?',
    method: 'La même boucle à travers le proxy de chaos, avec de plus en plus de latence ajoutée.',
  },
]);

/** Identifiants des suites, dans l'ordre d'exécution. */
export const SUITE_IDS = Object.freeze(SUITES.map((suite) => suite.id));

/**
 * Suite d'après son identifiant.
 * @param {string} id
 * @returns {SuiteInfo|undefined}
 */
export function suiteInfo(id) {
  return SUITES.find((suite) => suite.id === id);
}

/**
 * Préréglages de la configuration. « Rapide » et « Standard » reprennent `QUICK_CONFIG` et
 * `DEFAULT_CONFIG` du serveur ; « Approfondi » multiplie les répétitions.
 */
export const PRESETS = Object.freeze([
  { id: 'quick', label: 'Rapide', iterations: 200, warmup: 20, serialization_iterations: 300, sweep_iterations: 8, sweep_latencies_ms: [0, 20, 50] },
  { id: 'standard', label: 'Standard', iterations: 1000, warmup: 50, serialization_iterations: 2000, sweep_iterations: 20, sweep_latencies_ms: [0, 10, 50, 100, 200] },
  { id: 'deep', label: 'Approfondi', iterations: 5000, warmup: 100, serialization_iterations: 5000, sweep_iterations: 30, sweep_latencies_ms: [0, 10, 50, 100, 200] },
]);

/** Latences proposées pour le balayage « local vs distant » (ms d'aller-retour ajoutés). */
export const SWEEP_CHOICES = Object.freeze([0, 10, 20, 50, 100, 200, 500]);

/** Procédures que le banc sait mesurer : les quatre appels unaires communs aux quatre protocoles. */
export const MEASURABLE_METHODS = Object.freeze(['get_product_details', 'calculate_factorial', 'update_stock', 'list_products']);

/** Appels d'échauffement du balayage réseau, par point et par protocole (constante du serveur). */
const SWEEP_WARMUP_CALLS = 2;

/* --- Formats ----------------------------------------------------------------------------- */

function missing(value) {
  return value === null || value === undefined || !Number.isFinite(Number(value));
}

/**
 * Durée donnée en millisecondes, dans l'unité qui la rend lisible : « 24,7 µs », « 0,55 ms », « 1,2 s ».
 * @param {number} ms
 * @returns {string}
 */
export function fmtDur(ms) {
  if (missing(ms)) return DASH;
  return Math.abs(ms) < 1 ? fmtUs(ms * 1000) : fmtMs(ms);
}

/**
 * Durée donnée en nanosecondes : « 657 ns », « 15,7 µs », « 1,47 ms ».
 * @param {number} ns
 * @returns {string}
 */
export function fmtNs(ns) {
  if (missing(ns)) return DASH;
  return Math.abs(ns) < 1000 ? `${fmtNumber(ns, { maxDecimals: 0 })}${NNBSP}ns` : fmtUs(ns / 1000);
}

/**
 * Taille en octets, multiples de 1000 comme dans les faits marquants du rapport :
 * « 268 o », « 46,8 ko », « 1,25 Mo ».
 * @param {number} bytes
 * @returns {string}
 */
export function fmtOctets(bytes) {
  if (missing(bytes)) return DASH;
  const value = Number(bytes);
  if (Math.abs(value) >= 1e6) return `${fmtNumber(value / 1e6, { decimals: 2 })}${NNBSP}Mo`;
  if (Math.abs(value) >= 1e4) return `${fmtNumber(value / 1e3, { decimals: 1 })}${NNBSP}ko`;
  return `${fmtNumber(value, { maxDecimals: 1 })}${NNBSP}o`;
}

/**
 * Graduation d'un axe d'octets : « 100 o », « 10 ko », « 1 Mo ».
 * @param {number} bytes
 * @returns {string}
 */
export function fmtOctetsTick(bytes) {
  if (missing(bytes)) return DASH;
  const value = Number(bytes);
  if (Math.abs(value) >= 1e6) return `${fmtNumber(value / 1e6, { maxDecimals: 1 })}${NNBSP}Mo`;
  if (Math.abs(value) >= 1e3) return `${fmtNumber(value / 1e3, { maxDecimals: 1 })}${NNBSP}ko`;
  return `${fmtNumber(value, { maxDecimals: 0 })}${NNBSP}o`;
}

/**
 * Facteur multiplicatif : « × 22,4 », sans décimale à partir de 100.
 * @param {number} factor
 * @returns {string}
 */
export function fmtFactor(factor) {
  if (missing(factor)) return DASH;
  return `×${NNBSP}${fmtNumber(factor, { decimals: Math.abs(factor) >= 100 ? 0 : 1 })}`;
}

/**
 * « 2,6 fois » — pour une phrase.
 * @param {number} factor
 * @returns {string}
 */
export function fmtTimes(factor) {
  if (missing(factor)) return DASH;
  return `${fmtNumber(factor, { decimals: Math.abs(factor) >= 100 ? 0 : 1 })} fois`;
}

/**
 * Pourcentage déjà exprimé en points (« −49,9 % »), signe typographique compris.
 * @param {number} percent
 * @param {{signed?: boolean, decimals?: number}} [options] `decimals` : nombre fixe de décimales.
 * @returns {string}
 */
export function fmtPct(percent, { signed = false, decimals = 1 } = {}) {
  if (missing(percent)) return DASH;
  const sign = percent < 0 ? '−' : signed && percent > 0 ? '+' : '';
  return `${sign}${fmtNumber(Math.abs(percent), { decimals })}${NNBSP}%`;
}

/**
 * Date et heure d'un rapport : « 3 oct. 2026, 18:29:08 ».
 * @param {string} iso Horodatage ISO 8601.
 * @returns {string}
 */
export function fmtDate(iso) {
  const date = new Date(iso);
  if (!iso || Number.isNaN(date.getTime())) return DASH;
  return new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date);
}

/* --- Lecture d'un rapport -------------------------------------------------------------- */

/**
 * @typedef {Object} RunState
 * @property {boolean} running Une mesure est en cours.
 * @property {string} phase Suite en cours d'exécution (`payload`, `latency`…).
 * @property {string[]} suites Suites demandées par la mesure en cours.
 */

/**
 * État d'affichage d'une suite.
 * @param {Object|null} view Rapport affiché (complet, ou partiel pendant une mesure).
 * @param {RunState} run
 * @param {string} suite
 * @returns {'ready'|'measuring'|'pending'|'skipped'} `ready` : données complètes ; `measuring` :
 *   la suite est en cours (des données partielles peuvent déjà exister) ; `pending` : elle
 *   attend son tour ; `skipped` : elle ne fait pas partie du rapport.
 */
export function suiteStatus(view, run, suite) {
  if (run.running && run.suites.includes(suite)) {
    if (run.phase === suite) return 'measuring';
    return view?.[suite] ? 'ready' : 'pending';
  }
  return view?.[suite] ? 'ready' : 'skipped';
}

/**
 * Propriétés `loading` / `empty` d'un graphique selon l'état de sa suite.
 * @param {'ready'|'measuring'|'pending'|'skipped'} status
 * @param {string} [text] Texte de l'état vide.
 * @returns {{loading: boolean, empty: {icon: string, title: string, text: string}}}
 */
export function chartState(status, text = 'Cette mesure ne fait pas partie du rapport affiché.') {
  return {
    loading: status === 'pending' || status === 'measuring',
    empty: { icon: 'chart-column', title: 'Aucune mesure', text },
  };
}

/**
 * Empreinte d'une section partielle : change dès qu'une mesure s'y ajoute. Sert à ne redessiner
 * une section que lorsqu'elle a réellement grossi.
 * @param {string} key `payload`, `serialization`, `latency`, `network` ou `highlights`.
 * @param {*} data
 * @returns {number}
 */
export function sectionSize(key, data) {
  if (!data) return 0;
  if (key === 'payload') return (data.rows?.length ?? 0) + 100 * (data.wire?.length ?? 0) + 10000 * (data.scaling?.length ?? 0);
  if (key === 'latency') return data.results?.length ?? 0;
  if (key === 'network') return data.points?.length ?? 0;
  if (key === 'serialization') return data.rows?.length ?? 0;
  return Array.isArray(data) ? data.length : 1;
}

/**
 * Résultats de latence exploitables (au moins un appel réussi).
 * @param {Object|null} latency Section `latency` du rapport.
 * @returns {Object[]}
 */
export function measuredResults(latency) {
  return (latency?.results ?? []).filter((result) => Number.isFinite(result.mean_ms) && result.mean_ms > 0);
}

/**
 * Élément d'une liste qui minimise (ou maximise) une valeur.
 * @template T
 * @param {T[]} items
 * @param {(item: T) => number} valueOf
 * @param {'min'|'max'} [which='min']
 * @returns {T|undefined}
 */
export function extreme(items, valueOf, which = 'min') {
  let best;
  let bestValue = which === 'min' ? Infinity : -Infinity;
  for (const item of items) {
    const value = valueOf(item);
    if (!Number.isFinite(value)) continue;
    if (which === 'min' ? value < bestValue : value > bestValue) {
      best = item;
      bestValue = value;
    }
  }
  return best;
}

/**
 * Libellé d'un protocole dans un rapport : celui du rapport, sinon celui de l'interface.
 * @param {{protocol: string, label?: string}} row
 * @returns {string}
 */
export function protocolLabel(row) {
  return row.label || protocol(row.protocol).label;
}

/* --- Configuration ------------------------------------------------------------------------ */

/**
 * Préréglage auquel correspond une configuration, ou `null` si elle a été retouchée.
 * @param {{iterations: number, warmup: number, sweep_latencies_ms: number[]}} config
 * @returns {string|null}
 */
export function matchPreset(config) {
  const same = (a, b) => a.length === b.length && a.every((value, index) => value === b[index]);
  const found = PRESETS.find(
    (preset) => preset.iterations === config.iterations && preset.warmup === config.warmup && same(preset.sweep_latencies_ms, config.sweep_latencies_ms),
  );
  return found ? found.id : null;
}

/**
 * Ce que la configuration va coûter, calculé à partir de ses seuls paramètres : nombre d'appels
 * chronométrés et temps d'attente minimal imposé par la latence simulée du balayage.
 * @param {{suites: string[], protocols: string[], iterations: number, sweep_iterations: number, sweep_latencies_ms: number[]}} config
 * @returns {{calls: number, sweepCalls: number, sweepWaitS: number}}
 */
export function estimateRun(config) {
  const remote = config.protocols.filter((id) => id !== 'local').length;
  const latency = config.suites.includes('latency');
  const network = config.suites.includes('network');
  const calls = latency ? config.iterations * config.protocols.length : 0;
  const sweepCalls = network ? config.sweep_iterations * config.protocols.length * config.sweep_latencies_ms.length : 0;
  const waitMs = network ? config.sweep_latencies_ms.reduce((sum, value) => sum + value, 0) * (config.sweep_iterations + SWEEP_WARMUP_CALLS) * remote : 0;
  return { calls, sweepCalls, sweepWaitS: waitMs / 1000 };
}
