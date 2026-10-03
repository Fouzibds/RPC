/**
 * Chaos réseau — vocabulaire partagé par les sections de la page : types de marques de la
 * chronologie, lecture des évènements du proxy et du client résilient, petites statistiques.
 * Aucune valeur mesurée ici : seulement la façon de lire celles que le laboratoire envoie.
 */

import { fmtMs, fmtNumber, fmtPercent } from '../../core/format.js';

/** Au-delà de ce seuil, un succès est dessiné comme « lent » : l'attente devient perceptible. */
export const SLOW_MS = 150;

/** Nombre d'appels logiques pris en compte par les jauges. */
export const GAUGE_WINDOW = 50;

/** Types de marques ajoutés à ceux de la Timeline partagée. */
export const CHAOS_KINDS = Object.freeze({
  ok: { label: 'Succès', tone: 'success', style: 'solid' },
  slow: { label: `Succès lent (≥ ${fmtMs(SLOW_MS, { decimals: 0 })})`, tone: 'warning', style: 'hatch' },
  retry: { label: 'Succès après nouvelle tentative', tone: 'info', style: 'solid' },
  timeout: { label: 'Échéance dépassée', tone: 'warning', style: 'solid' },
  error: { label: 'Erreur', tone: 'danger', style: 'solid' },
  backoff: { label: 'Attente avant nouvelle tentative', tone: 'neutral', style: 'thin' },
  refused: { label: 'Refus du disjoncteur', tone: 'danger', style: 'hatch' },
  dedup: { label: 'Rejeu dédupliqué', tone: 'accent', style: 'solid' },
  breaker_open: { label: 'Disjoncteur ouvert', tone: 'danger', style: 'solid' },
  breaker_half: { label: 'Disjoncteur semi-ouvert', tone: 'warning', style: 'solid' },
  breaker_closed: { label: 'Disjoncteur refermé', tone: 'success', style: 'solid' },
  give_up: { label: 'Abandon', tone: 'danger', style: 'solid' },
  net_reset: { label: 'Coupure (proxy)', tone: 'danger', style: 'solid' },
  net_refuse: { label: 'Connexion refusée (proxy)', tone: 'danger', style: 'solid' },
  net_lost: { label: 'Réponse perdue (proxy)', tone: 'warning', style: 'solid' },
  net_blackhole: { label: 'Octets avalés (proxy)', tone: 'neutral', style: 'solid' },
  net_spike: { label: 'Pic de latence (proxy)', tone: 'info', style: 'solid' },
});

/** Libellés des évènements du proxy et du client résilient (flux en direct). */
export const STAGE_INFO = Object.freeze({
  'network.delay': { label: 'Retard', icon: 'clock', tone: 'neutral', kind: null },
  'network.reset': { label: 'Coupure', icon: 'unplug', tone: 'danger', kind: 'net_reset' },
  'network.refuse': { label: 'Refus', icon: 'ban', tone: 'danger', kind: 'net_refuse' },
  'network.blackhole': { label: 'Trou noir', icon: 'circle-slash', tone: 'neutral', kind: 'net_blackhole' },
  'network.lost_reply': { label: 'Réponse perdue', icon: 'cloud-off', tone: 'warning', kind: 'net_lost' },
  'resilience.attempt': { label: 'Tentative', icon: 'arrow-right', tone: 'neutral', kind: null },
  'resilience.backoff': { label: 'Attente', icon: 'hourglass', tone: 'info', kind: 'backoff' },
  'resilience.breaker': { label: 'Disjoncteur', icon: 'shield', tone: 'warning', kind: null },
  'resilience.dedup': { label: 'Dédupliqué', icon: 'fingerprint', tone: 'accent', kind: 'dedup' },
  'resilience.give_up': { label: 'Abandon', icon: 'circle-x', tone: 'danger', kind: 'give_up' },
});

/** États du disjoncteur : libellé et ton. */
export const BREAKER_STATES = Object.freeze({
  closed: { label: 'Fermé', tone: 'success', hint: 'les appels passent' },
  open: { label: 'Ouvert', tone: 'danger', hint: 'appels refusés sans réseau' },
  half_open: { label: 'Semi-ouvert', tone: 'warning', hint: 'un appel d’essai autorisé' },
});

/**
 * Type de marque d'une tentative terminée.
 * @param {{ok: boolean, code?: string, durationMs?: number, attempt?: number}} attempt
 * @returns {string} Clé de `CHAOS_KINDS`.
 */
export function attemptKind({ ok, code, durationMs = 0, attempt = 1 }) {
  if (!ok) return code === 'TIMEOUT' ? 'timeout' : 'error';
  if (attempt > 1) return 'retry';
  return durationMs >= SLOW_MS ? 'slow' : 'ok';
}

/**
 * Percentile par interpolation linéaire.
 * @param {number[]} values
 * @param {number} p Entre 0 et 1.
 * @returns {number|null} `null` si la série est vide.
 */
export function percentile(values, p) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const rank = (sorted.length - 1) * p;
  const low = Math.floor(rank);
  const high = Math.ceil(rank);
  return sorted[low] + (sorted[high] - sorted[low]) * (rank - low);
}

/**
 * Durée courte pour un réglage ou un résumé : millisecondes entières sous la seconde
 * (« 40 ms »), secondes au-delà (« 1,50 s ») — jamais d'arrondi trompeur.
 * @param {number} ms
 * @returns {string}
 */
export function fmtDelay(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value)) return fmtMs(ms);
  if (Math.abs(value) >= 1000) return fmtMs(value, { decimals: value % 1000 === 0 ? 0 : value % 100 === 0 ? 1 : 2 });
  return fmtMs(value, { decimals: value > 0 && value < 10 && !Number.isInteger(value) ? 1 : 0 });
}

/**
 * Rapport « tant de fois plus » lisible : « × 3 611 », « × 12,4 ».
 * @param {number} ratio
 * @returns {string}
 */
export function fmtTimes(ratio) {
  if (!Number.isFinite(ratio)) return '—';
  return `× ${fmtNumber(ratio, { maxDecimals: ratio >= 100 ? 0 : 1 })}`;
}

/**
 * Résumé chiffré d'un jeu de conditions réseau : « 200 ms ± 60 · pics 5 % · coupures 1 % ».
 * @param {Object} conditions `NetworkConditions.snapshot()` ou conditions d'un préréglage.
 * @returns {string}
 */
export function conditionsSummary(conditions) {
  if (!conditions) return '—';
  if (conditions.down) return 'serveur injoignable';
  if (conditions.blackhole) return 'aucune réponse';
  const parts = [];
  const latency = Number(conditions.latency_ms ?? 0);
  const jitter = Number(conditions.jitter_ms ?? 0);
  if (latency > 0 || jitter > 0) parts.push(`${fmtDelay(latency)}${jitter > 0 ? ` ± ${fmtNumber(jitter, { maxDecimals: 1 })}` : ''}`);
  if (Number(conditions.spike_probability) > 0) parts.push(`pics ${fmtPercent(conditions.spike_probability, { decimals: 0 })}`);
  if (Number(conditions.reset_probability) > 0) parts.push(`coupures ${fmtPercent(conditions.reset_probability, { decimals: 0 })}`);
  if (Number(conditions.bandwidth_kbps) > 0) parts.push(`${fmtNumber(conditions.bandwidth_kbps)} kb/s`);
  return parts.length ? parts.join(' · ') : 'aucun délai ajouté';
}

/**
 * Version très courte du résumé, pour une tuile étroite : « 200 ms · pics · coupures ».
 * @param {Object} conditions
 * @returns {string}
 */
export function conditionsBrief(conditions) {
  if (!conditions) return '—';
  if (conditions.down) return 'serveur injoignable';
  if (conditions.blackhole) return 'aucune réponse';
  const latency = Number(conditions.latency_ms ?? 0);
  const parts = [latency > 0 ? fmtDelay(latency) : 'sans délai'];
  if (Number(conditions.spike_probability) > 0) parts.push('pics');
  if (Number(conditions.reset_probability) > 0) parts.push('coupures');
  return parts.join(' · ');
}

/**
 * Vrai si les conditions s'écartent du réseau idéal.
 * @param {Object|null} conditions
 * @returns {boolean}
 */
export function isDegraded(conditions) {
  if (!conditions) return false;
  if (conditions.down || conditions.blackhole) return true;
  return ['latency_ms', 'jitter_ms', 'spike_probability', 'reset_probability', 'bandwidth_kbps'].some((field) => Number(conditions[field] ?? 0) > 0);
}

/**
 * Message lisible d'une erreur d'API ou RPC.
 * @param {*} error
 * @returns {string}
 */
export function errorText(error) {
  if (!error) return '';
  if (typeof error === 'string') return error;
  return error.message || error.code || 'Erreur inconnue';
}

/**
 * Regroupe des appels de rendu dans la prochaine image : un flux d'évènements dense ne
 * provoque qu'un seul rendu par image.
 * @param {() => void} render
 * @returns {{schedule: () => void, cancel: () => void}}
 */
export function frameBatch(render) {
  let handle = 0;
  return {
    schedule() {
      if (handle) return;
      handle = requestAnimationFrame(() => {
        handle = 0;
        render();
      });
    },
    cancel() {
      if (handle) cancelAnimationFrame(handle);
      handle = 0;
    },
  };
}
