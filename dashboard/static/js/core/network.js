/**
 * Conditions réseau simulées : préréglages connus, lecture « humaine » des conditions
 * actives et application d'un préréglage (`PUT /api/network`).
 */

import { api } from './api.js';
import { appStore } from './store.js';
import { fmtMs } from './format.js';

/**
 * @typedef {Object} NetworkPreset
 * @property {string} id Identifiant côté serveur (`netsim.conditions.PRESETS`).
 * @property {string} label Libellé court.
 * @property {string} description Ce que le préréglage simule.
 * @property {string} icon Nom d'icône.
 * @property {'neutral'|'warning'|'danger'} tone Gravité visuelle.
 */

/** @type {ReadonlyArray<NetworkPreset>} */
export const NETWORK_PRESETS = Object.freeze([
  { id: 'ideal', label: 'Idéal', description: 'Boucle locale : aucune latence ajoutée, aucune panne', icon: 'wifi', tone: 'neutral' },
  { id: 'lan', label: 'Réseau local', description: 'Quelques millisecondes, très stable', icon: 'ethernet-port', tone: 'warning' },
  { id: 'wan', label: 'Internet (WAN)', description: 'Latence continentale et un peu de gigue', icon: 'globe', tone: 'warning' },
  { id: 'mobile_3g', label: 'Mobile 3G', description: 'Latence élevée, gigue forte, débit limité', icon: 'signal', tone: 'warning' },
  { id: 'satellite', label: 'Satellite', description: 'Plus d’une demi-seconde d’aller-retour', icon: 'satellite-dish', tone: 'warning' },
  { id: 'flaky', label: 'Instable', description: 'Pics de latence et coupures aléatoires', icon: 'activity', tone: 'warning' },
  { id: 'blackhole', label: 'Trou noir', description: 'Les octets partent, rien ne revient : seul un délai protège', icon: 'circle-slash', tone: 'danger' },
  { id: 'outage', label: 'Panne', description: 'Serveur injoignable : connexions refusées', icon: 'wifi-off', tone: 'danger' },
]);

const CUSTOM = Object.freeze({
  id: 'custom',
  label: 'Personnalisé',
  description: 'Conditions réglées à la main',
  icon: 'sliders-horizontal',
  tone: 'warning',
});

/**
 * Fiche d'un préréglage (les identifiants inconnus donnent « Personnalisé »).
 * @param {string} id
 * @returns {NetworkPreset}
 */
export function presetInfo(id) {
  return NETWORK_PRESETS.find((preset) => preset.id === id) ?? CUSTOM;
}

function isDegraded(conditions) {
  return ['latency_ms', 'jitter_ms', 'spike_probability', 'reset_probability', 'bandwidth_kbps'].some(
    (field) => Number(conditions[field] ?? 0) > 0,
  );
}

/**
 * Résumé affichable des conditions réseau actives.
 * @param {Object|null} conditions `NetworkConditions.snapshot()` ou `null` si inconnues.
 * @returns {{tone: 'neutral'|'warning'|'danger', label: string, detail: string, icon: string, known: boolean}}
 *   `label` = préréglage, `detail` = fait saillant (« 80 ms », « aucune réponse »…), `known` = données reçues.
 */
export function describeNetwork(conditions) {
  if (!conditions) return { tone: 'neutral', label: 'Réseau', detail: '—', icon: 'wifi', known: false };
  const preset = presetInfo(conditions.preset);
  if (conditions.down) return { tone: 'danger', label: preset.id === 'custom' ? 'Panne' : preset.label, detail: 'injoignable', icon: 'wifi-off', known: true };
  if (conditions.blackhole) {
    return { tone: 'danger', label: preset.id === 'custom' ? 'Trou noir' : preset.label, detail: 'aucune réponse', icon: 'circle-slash', known: true };
  }
  if (!isDegraded(conditions)) return { tone: 'neutral', label: 'Réseau idéal', detail: '', icon: 'wifi', known: true };
  const latency = Number(conditions.latency_ms ?? 0);
  const detail = latency > 0 ? `+${fmtMs(latency, { decimals: 0 })}` : Number(conditions.reset_probability ?? 0) > 0 ? 'coupures' : 'dégradé';
  return { tone: 'warning', label: preset.label, detail, icon: preset.icon, known: true };
}

/**
 * Applique un préréglage réseau aux trois proxys de chaos et met le magasin à jour.
 * @param {string} id Identifiant de préréglage (`'wan'`, `'ideal'`…).
 * @returns {Promise<Object>} Les nouvelles conditions. Lève une `ApiError` si le laboratoire refuse ou est injoignable.
 */
export async function applyPreset(id) {
  const response = await api.put('/api/network', { preset: id });
  const conditions = response?.conditions ?? response;
  appStore.set({ network: conditions });
  return conditions;
}
