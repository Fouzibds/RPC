/**
 * Routeur par `location.hash` : `#/console?protocol=grpc` → `{ id: 'console', query: { protocol: 'grpc' } }`.
 * Le routeur ne connaît pas les pages : il valide l'identifiant et prévient l'application.
 */

/**
 * @typedef {Object} Route
 * @property {string} id Identifiant de page.
 * @property {Record<string, string>} query Paramètres après « ? ».
 */

let current = { id: '', query: {} };

/**
 * Analyse un fragment d'URL.
 * @param {string} [hash=location.hash]
 * @returns {Route}
 */
export function parseHash(hash = window.location.hash) {
  const raw = hash.replace(/^#\/?/, '');
  const [path, search = ''] = raw.split('?');
  const id = path.split('/')[0].trim().toLowerCase();
  return { id, query: Object.fromEntries(new URLSearchParams(search)) };
}

/**
 * Construit le fragment d'URL d'une page.
 * @param {string} id
 * @param {Record<string, string|number|boolean>} [query]
 * @returns {string} Par exemple `#/xray?call=grpc-000042`.
 */
export function href(id, query) {
  const entries = Object.entries(query ?? {}).filter(([, value]) => value !== null && value !== undefined && value !== '');
  const search = new URLSearchParams(entries.map(([key, value]) => [key, String(value)])).toString();
  return `#/${id}${search ? `?${search}` : ''}`;
}

/**
 * Navigue vers une page.
 * @param {string} id Identifiant (`'chaos'`) ou fragment complet (`'#/chaos'`).
 * @param {Record<string, string|number|boolean>} [query]
 * @param {{replace?: boolean}} [options] `replace: true` n'ajoute pas d'entrée à l'historique.
 * @returns {void}
 */
export function navigate(id, query, { replace = false } = {}) {
  const target = id.startsWith('#') ? id : href(id, query);
  if (target === window.location.hash) return;
  if (replace) window.location.replace(target);
  else window.location.hash = target;
}

/**
 * Route actuellement affichée.
 * @returns {Route}
 */
export function currentRoute() {
  return current;
}

/**
 * Démarre le routeur.
 * @param {Object} options
 * @param {string[]} options.ids Identifiants de pages valides.
 * @param {string} options.fallback Page par défaut (URL vide ou inconnue).
 * @param {(route: Route, previous: Route) => void} options.onChange Appelé au démarrage puis à chaque changement.
 * @returns {() => void} Arrêt du routeur.
 */
export function startRouter({ ids, fallback, onChange }) {
  const valid = new Set(ids);

  function sync() {
    const route = parseHash();
    if (!valid.has(route.id)) {
      window.location.replace(href(fallback));
      return;
    }
    const previous = current;
    current = route;
    onChange(route, previous);
  }

  window.addEventListener('hashchange', sync);
  sync();
  return () => window.removeEventListener('hashchange', sync);
}
