/**
 * Magasin d'état réactif minimal + `appStore`, l'état partagé de toute l'application.
 */

function shallowEqual(a, b) {
  if (Object.is(a, b)) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const keysA = Object.keys(a);
  const keysB = Object.keys(b);
  if (keysA.length !== keysB.length) return false;
  return keysA.every((key) => Object.is(a[key], b[key]));
}

/**
 * @template T
 * @typedef {Object} Store
 * @property {() => T} get État courant (ne pas le muter).
 * @property {(patch: Partial<T> | ((state: T) => Partial<T>)) => void} set Fusion superficielle d'un correctif.
 * @property {<K extends keyof T>(key: K, updater: (value: T[K]) => T[K]) => void} update Transforme une clé.
 * @property {(selector: keyof T | ((state: T) => *) | null, callback: (value: *, previous: *) => void, options?: {immediate?: boolean, equals?: (a: *, b: *) => boolean}) => () => void} subscribe
 *   Abonnement à une tranche de l'état ; renvoie la fonction de désabonnement.
 */

/**
 * Crée un magasin d'état.
 *
 * - `set(patch)` fusionne superficiellement (`patch` peut être une fonction de l'état).
 * - `update(key, fn)` remplace `state[key]` par `fn(state[key])`.
 * - `subscribe(selector, cb, {immediate, equals})` : `selector` est une fonction de l'état, le nom
 *   d'une clé, ou `null` (tout l'état). `cb(valeur, précédente)` n'est appelé que si la valeur
 *   sélectionnée change (égalité superficielle par défaut). `immediate: true` appelle `cb` tout de suite.
 * @template {Record<string, *>} T
 * @param {T} initial
 * @returns {Store<T>}
 */
export function createStore(initial) {
  let state = { ...initial };
  const subscriptions = new Set();

  function get() {
    return state;
  }

  function set(patch) {
    const next = typeof patch === 'function' ? patch(state) : patch;
    if (!next) return;
    const changed = Object.keys(next).some((key) => !Object.is(state[key], next[key]));
    if (!changed) return;
    state = { ...state, ...next };
    for (const subscription of Array.from(subscriptions)) subscription.notify();
  }

  function update(key, updater) {
    set({ [key]: updater(state[key]) });
  }

  function subscribe(selector, callback, { immediate = false, equals = shallowEqual } = {}) {
    const select =
      typeof selector === 'function' ? selector : selector === null || selector === undefined ? (s) => s : (s) => s[selector];
    let current = select(state);
    const subscription = {
      notify() {
        const value = select(state);
        if (equals(value, current)) return;
        const previous = current;
        current = value;
        callback(value, previous);
      },
    };
    subscriptions.add(subscription);
    if (immediate) callback(current, undefined);
    return () => subscriptions.delete(subscription);
  }

  return { get, set, update, subscribe };
}

/**
 * État partagé de l'application.
 * @typedef {Object} AppState
 * @property {'unknown'|'online'|'offline'} api Joignabilité de l'API HTTP du laboratoire.
 * @property {'offline'|'connecting'|'live'} ws État du WebSocket temps réel.
 * @property {Object|null} status Dernière réponse de `GET /api/status` (tenue à jour par les messages `stats`).
 * @property {Object|null} network Conditions réseau actives (`NetworkConditions.snapshot()`).
 * @property {Object|null} catalog Réponse de `GET /api/catalog` (procédures, protocoles, produits, étapes).
 * @property {Object|null} stats Dernier message WebSocket `stats` brut.
 * @property {boolean} wiretap Le tiroir « Sous le capot » est-il ouvert ?
 */

/** @type {Store<AppState>} */
export const appStore = createStore({
  api: 'unknown',
  ws: 'offline',
  status: null,
  network: null,
  catalog: null,
  stats: null,
  wiretap: false,
});
