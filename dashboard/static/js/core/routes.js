/**
 * Table des pages : source unique pour la barre latérale, la palette de commandes,
 * les raccourcis « g + lettre » et le chargement paresseux des modules de page.
 */

/**
 * @typedef {Object} RouteInfo
 * @property {string} id Identifiant et segment d'URL (`#/<id>`).
 * @property {string} label Libellé de navigation.
 * @property {string} subtitle Sous-titre d'une ligne (barre haute).
 * @property {string} icon Nom d'icône.
 * @property {string|null} group Identifiant du groupe de navigation (`null` = page masquée).
 * @property {string|null} key Lettre du raccourci « g puis lettre ».
 * @property {() => Promise<{default: Object}>} load Chargement du module de page.
 */

/** Groupes de la barre latérale, dans l'ordre d'affichage. */
export const NAV_GROUPS = Object.freeze([
  { id: 'explore', label: 'Explorer' },
  { id: 'lab', label: 'Laboratoire' },
  { id: 'synthesis', label: 'Synthèse' },
]);

/** @type {ReadonlyArray<RouteInfo>} */
export const ROUTES = Object.freeze([
  {
    id: 'overview',
    label: 'Vue d’ensemble',
    subtitle: 'État du laboratoire, compteurs et architecture en direct',
    icon: 'layout-dashboard',
    group: 'explore',
    key: 'o',
    load: () => import('../pages/overview.js'),
  },
  {
    id: 'console',
    label: 'Console RPC',
    subtitle: 'Appeler une procédure distante, protocole par protocole',
    icon: 'square-terminal',
    group: 'explore',
    key: 'c',
    load: () => import('../pages/console.js'),
  },
  {
    id: 'xray',
    label: 'Sous le capot',
    subtitle: 'Du stub au squelette : chaque étape, chaque octet',
    icon: 'scan-search',
    group: 'explore',
    key: 'x',
    load: () => import('../pages/xray.js'),
  },
  {
    id: 'benchmark',
    label: 'Benchmark',
    subtitle: 'Tailles, latences et débit mesurés sur cette machine',
    icon: 'gauge',
    group: 'lab',
    key: 'b',
    load: () => import('../pages/benchmark.js'),
  },
  {
    id: 'chaos',
    label: 'Chaos réseau',
    subtitle: 'Latence, coupures et pannes : le réseau n’est pas fiable',
    icon: 'zap',
    group: 'lab',
    key: 'h',
    load: () => import('../pages/chaos.js'),
  },
  {
    id: 'contract',
    label: 'Contrat & IDL',
    subtitle: 'Faire évoluer un contrat sans casser ses clients',
    icon: 'file-code',
    group: 'lab',
    key: 'i',
    load: () => import('../pages/contract.js'),
  },
  {
    id: 'compare',
    label: 'Transparence',
    subtitle: 'Le même appel écrit de quatre façons',
    icon: 'git-compare',
    group: 'synthesis',
    key: 't',
    load: () => import('../pages/compare.js'),
  },
  {
    id: 'learn',
    label: 'Bilan',
    subtitle: 'Avantages et inconvénients, chiffrés par vos mesures',
    icon: 'scale',
    group: 'synthesis',
    key: 'l',
    load: () => import('../pages/learn.js'),
  },
  {
    id: 'kit',
    label: 'Kit d’interface',
    subtitle: 'Composants, couleurs et typographie du laboratoire',
    icon: 'component',
    group: null,
    key: null,
    load: () => import('../pages/kit.js'),
  },
]);

/** Page affichée quand l'URL ne désigne aucune page connue. */
export const DEFAULT_ROUTE = 'overview';

/**
 * Fiche d'une page.
 * @param {string} id
 * @returns {RouteInfo|undefined}
 */
export function routeInfo(id) {
  return ROUTES.find((route) => route.id === id);
}

/**
 * Pages d'un groupe de navigation.
 * @param {string} groupId
 * @returns {RouteInfo[]}
 */
export function routesOf(groupId) {
  return ROUTES.filter((route) => route.group === groupId);
}
