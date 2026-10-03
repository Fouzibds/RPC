/**
 * Console RPC — modèle : configuration d'un appel, règles de disponibilité, corps de
 * `POST /api/call` et lecture de la chaîne de requête (`#/console?protocol=grpc&method=…`).
 * Aucun accès au DOM : tout ici est pur et sérialisable (l'historique en conserve des copies).
 */

import { PROTOCOL_IDS, protocol } from '../../core/protocols.js';

/** Les quatre formes d'appel du catalogue : libellé du badge, icône et explication. */
export const KINDS = Object.freeze({
  unary: { label: 'unaire', icon: 'arrow-right-left', tone: 'neutral', text: 'Une requête, une réponse.' },
  server_stream: { label: 'flux serveur', icon: 'activity', tone: 'info', text: 'Une requête, puis le serveur pousse plusieurs réponses.' },
  client_stream: { label: 'flux client', icon: 'upload', tone: 'accent', text: 'Le client envoie plusieurs messages, le serveur répond une fois.' },
  bidi_stream: { label: 'flux bidirectionnel', icon: 'arrow-up-down', tone: 'accent', text: 'Les deux côtés émettent sur le même flux.' },
});

/** Libellés des modes d'exécution. */
export const MODES = Object.freeze({
  sync: { label: 'Synchrone', short: 'sync', icon: 'arrow-right' },
  async: { label: 'Asynchrone × N', short: 'async', icon: 'shuffle' },
  stream: { label: 'Flux', short: 'flux', icon: 'activity' },
});

/** Plus petit lot asynchrone : en dessous de deux appels, rien ne peut se chevaucher. */
export const COUNT_MIN = 2;

/** Politique de résilience proposée quand on l'active (valeurs par défaut de `netsim.RetryPolicy`). */
export const POLICY_DEFAULTS = Object.freeze({ enabled: false, max_attempts: 3, base_delay_ms: 100, breaker: false, auto_idempotency_key: false });

/**
 * @typedef {Object} ConsoleConfig
 * @property {string} protocol `local` | `custom` | `grpc` | `rest`
 * @property {string} method Nom de la procédure du catalogue.
 * @property {Record<string, Record<string, *>>} values Valeurs saisies, par procédure puis par paramètre.
 * @property {'sync'|'async'|'stream'} mode
 * @property {number} count Nombre d'appels du mode asynchrone.
 * @property {boolean} viaProxy
 * @property {number} timeoutMs
 * @property {{enabled: boolean, max_attempts: number, base_delay_ms: number, breaker: boolean, auto_idempotency_key: boolean}} policy
 */

/**
 * Description d'une procédure du catalogue.
 * @param {Object|null} catalog Réponse de `GET /api/catalog`.
 * @param {string} name
 * @returns {Object|undefined}
 */
export function methodSpec(catalog, name) {
  return catalog?.methods?.find((spec) => spec.name === name);
}

/**
 * Valeurs par défaut des paramètres d'une procédure (copie profonde des listes).
 * @param {Object} spec `MethodSpec` du catalogue.
 * @returns {Record<string, *>}
 */
export function defaultValues(spec) {
  return Object.fromEntries((spec?.params ?? []).map((param) => [param.name, structuredClone(param.default)]));
}

/**
 * Configuration initiale : première procédure, JSON-RPC maison, appel synchrone.
 * @param {Object} catalog
 * @returns {ConsoleConfig}
 */
export function initialConfig(catalog) {
  const limits = catalog?.limits ?? {};
  return {
    protocol: 'custom',
    method: methodSpec(catalog, 'update_stock')?.name ?? catalog?.methods?.[0]?.name ?? '',
    values: {},
    mode: 'sync',
    count: Math.min(20, limits.async_calls_max ?? 200),
    viaProxy: false,
    timeoutMs: limits.timeout_ms_default ?? 5000,
    policy: { ...POLICY_DEFAULTS },
  };
}

/**
 * Pourquoi une procédure n'est pas proposée par un protocole (`''` si elle l'est).
 * @param {Object} spec
 * @param {string} protocolId
 * @returns {string}
 */
export function unavailableReason(spec, protocolId) {
  if (!spec || spec.protocols.includes(protocolId)) return '';
  const offered = spec.protocols.map((id) => protocol(id).short).join(' et ');
  const kind = KINDS[spec.kind]?.label ?? spec.kind;
  const why = {
    custom: 'le protocole maison n’envoie qu’une requête par appel',
    rest: 'HTTP/1.1 ne sait pas recevoir un flux de requêtes',
  }[protocolId];
  return `Indisponible avec ${protocol(protocolId).short} : un ${kind} n’existe qu’avec ${offered}${why ? ` (${why})` : ''}.`;
}

/**
 * Modes d'exécution permis pour une procédure : le flux est imposé aux procédures en flux.
 * @param {Object} spec
 * @returns {Array<'sync'|'async'|'stream'>}
 */
export function allowedModes(spec) {
  return spec && spec.kind !== 'unary' ? ['stream'] : ['sync', 'async'];
}

/**
 * Ramène une configuration à un état cohérent avec le catalogue : procédure connue, protocole
 * qui la propose, mode permis, bornes respectées.
 * @param {ConsoleConfig} config
 * @param {Object} catalog
 * @returns {ConsoleConfig} La même configuration, corrigée.
 */
export function normalize(config, catalog) {
  const limits = catalog?.limits ?? {};
  if (!PROTOCOL_IDS.includes(config.protocol)) config.protocol = 'custom';
  let spec = methodSpec(catalog, config.method);
  if (!spec) {
    spec = catalog?.methods?.[0];
    config.method = spec?.name ?? '';
  }
  if (spec && !spec.protocols.includes(config.protocol)) {
    config.protocol = spec.protocols.find((id) => id !== 'local') ?? spec.protocols[0];
  }
  const modes = allowedModes(spec);
  if (!modes.includes(config.mode)) config.mode = modes[0];
  config.count = clampInt(config.count, COUNT_MIN, limits.async_calls_max ?? 200, 20);
  config.timeoutMs = clampInt(config.timeoutMs, 1, limits.timeout_ms_max ?? 60000, limits.timeout_ms_default ?? 5000);
  config.policy = { ...POLICY_DEFAULTS, ...config.policy };
  config.policy.max_attempts = clampInt(config.policy.max_attempts, 1, 10, 3);
  config.policy.base_delay_ms = clampInt(config.policy.base_delay_ms, 0, 10000, 100);
  if (config.protocol === 'local') config.viaProxy = false;
  if (spec) {
    // Complété en place : le formulaire garde une référence sur cet objet et y écrit ses saisies.
    const values = config.values[spec.name] ?? {};
    for (const [name, value] of Object.entries(defaultValues(spec))) if (!(name in values)) values[name] = value;
    config.values[spec.name] = values;
  }
  return config;
}

function clampInt(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(max, Math.max(min, Math.round(number)));
}

/**
 * Paramètres réellement transmis : les paramètres obligatoires, puis les facultatifs renseignés
 * (une chaîne vide facultative est omise : le serveur applique sa valeur par défaut).
 * @param {Object} spec
 * @param {Record<string, *>} values
 * @returns {Array<{name: string, value: *, required: boolean, spec: Object}>}
 */
export function sentParams(spec, values) {
  const required = new Set(spec.required ?? spec.params.map((param) => param.name));
  return spec.params
    .map((param) => ({ name: param.name, value: values?.[param.name] ?? param.default, required: required.has(param.name), spec: param }))
    .filter((entry) => entry.required || entry.value !== '');
}

/**
 * Contrôle les valeurs saisies avant l'envoi.
 * @param {Object} spec
 * @param {Record<string, *>} values
 * @returns {Record<string, string>} Message d'erreur par paramètre (objet vide si tout est valide).
 */
export function validate(spec, values) {
  const errors = {};
  for (const param of spec?.params ?? []) {
    const value = values?.[param.name];
    if (param.type === 'int') {
      if (!Number.isInteger(value)) errors[param.name] = 'Un entier est attendu.';
      else if (param.minimum !== null && value < param.minimum) errors[param.name] = `Minimum : ${param.minimum}.`;
      else if (param.maximum !== null && value > param.maximum) errors[param.name] = `Maximum : ${param.maximum}.`;
    } else if (param.type === 'updates') {
      if (value instanceof SyntaxError) errors[param.name] = 'JSON invalide : vérifiez les guillemets, les virgules et les crochets.';
      else if (!Array.isArray(value) || !value.every((item) => item && typeof item === 'object' && !Array.isArray(item))) {
        errors[param.name] = 'Une liste d’objets {"product_id", "delta"} est attendue.';
      } else if (!value.length) errors[param.name] = 'Ajoutez au moins un mouvement.';
    } else if (param.type === 'product_ids') {
      if (value instanceof SyntaxError) errors[param.name] = 'JSON invalide : vérifiez les guillemets, les virgules et les crochets.';
      else if (!Array.isArray(value) || !value.every((item) => typeof item === 'string')) errors[param.name] = 'Une liste de références (chaînes) est attendue.';
      else if (!value.length) errors[param.name] = 'Ajoutez au moins une référence.';
    } else if ((spec.required ?? []).includes(param.name) && (value === '' || value === null || value === undefined)) {
      errors[param.name] = 'Valeur obligatoire.';
    }
  }
  return errors;
}

/**
 * Corps de `POST /api/call` pour une configuration.
 * @param {ConsoleConfig} config
 * @param {Object} catalog
 * @returns {Object}
 */
export function buildRequest(config, catalog) {
  const spec = methodSpec(catalog, config.method);
  const params = Object.fromEntries(sentParams(spec, config.values[config.method]).map((entry) => [entry.name, entry.value]));
  const { enabled, ...policy } = config.policy;
  return {
    protocol: config.protocol,
    method: config.method,
    params,
    mode: config.mode,
    count: config.mode === 'async' ? config.count : 1,
    via_proxy: config.viaProxy && config.protocol !== 'local',
    timeout_ms: config.timeoutMs,
    policy: enabled ? policy : null,
  };
}

/**
 * Copie indépendante d'une configuration (pour l'historique).
 * @param {ConsoleConfig} config
 * @returns {ConsoleConfig}
 */
export function snapshot(config) {
  return {
    protocol: config.protocol,
    method: config.method,
    values: { [config.method]: structuredClone(config.values[config.method] ?? {}) },
    mode: config.mode,
    count: config.count,
    viaProxy: config.viaProxy,
    timeoutMs: config.timeoutMs,
    policy: { ...config.policy },
  };
}

function coerce(param, raw) {
  if (param.type === 'int') {
    const number = Number(raw);
    return Number.isInteger(number) ? number : undefined;
  }
  if (param.type === 'updates' || param.type === 'product_ids') {
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : undefined;
    } catch {
      return param.type === 'product_ids' ? raw.split(',').map((item) => item.trim()).filter(Boolean) : undefined;
    }
  }
  return String(raw);
}

/**
 * Applique la chaîne de requête de la route à une configuration :
 * `protocol`, `method`, `mode`, `count`, `proxy=1`, `timeout`, puis tout paramètre de la procédure
 * (`?method=update_stock&product_id=SKU-1002&delta=-3`).
 * @param {ConsoleConfig} config
 * @param {Record<string, string>} query
 * @param {Object} catalog
 * @returns {boolean} `true` si la requête portait au moins un réglage.
 */
export function applyQuery(config, query, catalog) {
  const keys = Object.keys(query ?? {});
  if (!keys.length) return false;
  if (query.method && methodSpec(catalog, query.method)) config.method = query.method;
  if (query.protocol && PROTOCOL_IDS.includes(query.protocol)) config.protocol = query.protocol;
  if (query.mode && query.mode in MODES) config.mode = query.mode;
  if (query.count) config.count = Number(query.count);
  if (query.timeout) config.timeoutMs = Number(query.timeout);
  if (query.proxy !== undefined || query.via_proxy !== undefined) config.viaProxy = ['1', 'true', 'oui'].includes(String(query.proxy ?? query.via_proxy));
  const spec = methodSpec(catalog, config.method);
  if (spec) {
    const values = { ...defaultValues(spec), ...config.values[spec.name] };
    for (const param of spec.params) {
      if (query[param.name] === undefined) continue;
      const value = coerce(param, query[param.name]);
      if (value !== undefined) values[param.name] = value;
    }
    config.values[spec.name] = values;
  }
  normalize(config, catalog);
  return true;
}
