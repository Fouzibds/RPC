/**
 * Accès au laboratoire : client HTTP (`api`), flux temps réel (`ws`) et boucle de
 * connexion (`startRuntime`). Aucune erreur n'est écrite dans la console : un échec
 * devient un état du magasin (`api: 'offline'`, `ws: 'offline'`) ou une `ApiError`
 * que l'appelant choisit de traiter.
 */

import { appStore } from './store.js';

/** Erreur renvoyée par `api.get/post/put`. `status === 0` : laboratoire injoignable. */
export class ApiError extends Error {
  /**
   * @param {number} status Code HTTP, ou `0` si aucune réponse n'a été reçue.
   * @param {string} message Message lisible, en français.
   * @param {*} [body] Corps de la réponse d'erreur, s'il a pu être lu.
   */
  constructor(status, message, body = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }

  /** Vrai si le serveur n'a pas répondu du tout (réseau, serveur arrêté, délai dépassé). */
  get offline() {
    return this.status === 0;
  }
}

function errorMessage(status, body) {
  if (body && typeof body === 'object') {
    const detail = body.detail ?? body.error?.message ?? body.message ?? body.error;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail) && detail.length) return detail.map((item) => item.msg ?? String(item)).join(' · ');
  }
  if (status === 404) return 'Ressource introuvable sur le laboratoire.';
  return `Le laboratoire a répondu par une erreur (HTTP ${status}).`;
}

async function request(method, path, { body, query, signal, timeoutMs = 30000 } = {}) {
  const entries = Object.entries(query ?? {}).filter(([, value]) => value !== null && value !== undefined);
  const search = new URLSearchParams(entries.map(([key, value]) => [key, String(value)])).toString();
  const url = search ? `${path}?${search}` : path;

  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, { once: true });

  let response;
  try {
    response = await fetch(url, {
      method,
      headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      cache: 'no-store',
    });
  } catch {
    throw new ApiError(0, signal?.aborted ? 'Requête annulée.' : 'Laboratoire injoignable.');
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }

  const isJson = (response.headers.get('content-type') ?? '').includes('json');
  let payload = null;
  try {
    payload = isJson ? await response.json() : await response.text();
  } catch {
    payload = null;
  }
  if (!response.ok) throw new ApiError(response.status, errorMessage(response.status, payload), payload);
  if (!isJson && path.startsWith('/api/')) throw new ApiError(response.status, 'Réponse inattendue du laboratoire.', payload);
  return payload;
}

/**
 * Client HTTP du laboratoire. Chaque méthode renvoie le JSON décodé et lève une `ApiError`
 * (`status`, `message`, `body`, `offline`) en cas d'échec — jamais d'erreur brute.
 *
 * Options communes : `query` (objet → `?a=1`), `signal` (`AbortSignal`), `timeoutMs` (30 s par défaut).
 */
export const api = Object.freeze({
  /**
   * @param {string} path Chemin absolu, ex. `/api/status`.
   * @param {{query?: Record<string, *>, signal?: AbortSignal, timeoutMs?: number}} [options]
   * @returns {Promise<*>}
   */
  get: (path, options) => request('GET', path, options),
  /**
   * @param {string} path
   * @param {*} [body] Corps JSON.
   * @param {{query?: Record<string, *>, signal?: AbortSignal, timeoutMs?: number}} [options]
   * @returns {Promise<*>}
   */
  post: (path, body = {}, options) => request('POST', path, { ...options, body }),
  /**
   * @param {string} path
   * @param {*} [body] Corps JSON.
   * @param {{query?: Record<string, *>, signal?: AbortSignal, timeoutMs?: number}} [options]
   * @returns {Promise<*>}
   */
  put: (path, body = {}, options) => request('PUT', path, { ...options, body }),
});

/* --- État partagé --------------------------------------------------------- */

/**
 * Recharge `GET /api/status` dans le magasin (`status`, `network`, `api`).
 * @returns {Promise<boolean>} `true` si le laboratoire a répondu.
 */
export async function refreshStatus() {
  try {
    const status = await api.get('/api/status', { timeoutMs: 6000 });
    appStore.set({ api: 'online', status, network: status?.network ?? appStore.get().network });
    return true;
  } catch {
    appStore.set({ api: 'offline' });
    return false;
  }
}

/**
 * Recharge `GET /api/catalog` dans le magasin (`catalog`).
 * @returns {Promise<boolean>} `true` si le catalogue a été chargé.
 */
export async function refreshCatalog() {
  try {
    appStore.set({ catalog: await api.get('/api/catalog', { timeoutMs: 6000 }) });
    return true;
  } catch {
    return false;
  }
}

/* --- WebSocket ------------------------------------------------------------ */

const handlers = new Map();
const topics = new Map();
let socket = null;
let wanted = false;

function emit(type, message) {
  for (const key of [type, '*']) {
    const set = handlers.get(key);
    if (!set) continue;
    for (const handler of Array.from(set)) {
      try {
        handler(message);
      } catch {
        /* un abonné défaillant ne doit pas interrompre le flux */
      }
    }
  }
}

function rawSend(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  try {
    socket.send(JSON.stringify(message));
    return true;
  } catch {
    return false;
  }
}

function openSocket() {
  if (socket || !wanted) return;
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  appStore.set({ ws: 'connecting' });
  let opened;
  try {
    opened = new WebSocket(`${scheme}://${window.location.host}/ws`);
  } catch {
    appStore.set({ ws: 'offline' });
    emit('close', { type: 'close' });
    return;
  }
  socket = opened;

  opened.addEventListener('open', () => {
    appStore.set({ ws: 'live' });
    if (topics.size) rawSend({ type: 'subscribe', topics: Array.from(topics.keys()) });
    emit('open', { type: 'open' });
  });

  opened.addEventListener('message', (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message && typeof message.type === 'string') emit(message.type, message);
  });

  opened.addEventListener('close', () => {
    if (socket === opened) socket = null;
    appStore.set({ ws: 'offline' });
    emit('close', { type: 'close' });
  });
}

/**
 * Flux temps réel (singleton). Les messages sont des objets `{type, …}` ; outre les types du
 * serveur (`hello`, `stats`, `call`, `trace`, `network`, `resilience`, `job`, `stream`,
 * `stream_end`), deux types locaux existent : `open` et `close`. L'état de la connexion est
 * dans `appStore` (`ws: 'offline' | 'connecting' | 'live'`).
 */
export const ws = Object.freeze({
  /**
   * Écoute un type de message (`'*'` = tous).
   * @param {string} type
   * @param {(message: Object) => void} callback
   * @returns {() => void} Désabonnement.
   */
  on(type, callback) {
    if (!handlers.has(type)) handlers.set(type, new Set());
    handlers.get(type).add(callback);
    return () => handlers.get(type)?.delete(callback);
  },

  /**
   * Envoie un message JSON.
   * @param {Object} message
   * @returns {boolean} `false` si la connexion n'est pas ouverte (rien n'est mis en file).
   */
  send(message) {
    return rawSend(message);
  },

  /**
   * Demande au serveur des sujets facultatifs (ex. `['trace']`). Les abonnements sont comptés :
   * le sujet reste actif tant qu'un abonné le demande, et il est redemandé après une reconnexion.
   * @param {string[]} names
   * @returns {() => void} Fin de l'abonnement.
   */
  subscribe(names) {
    for (const name of names) topics.set(name, (topics.get(name) ?? 0) + 1);
    rawSend({ type: 'subscribe', topics: Array.from(topics.keys()) });
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      const dropped = [];
      for (const name of names) {
        const count = (topics.get(name) ?? 1) - 1;
        if (count <= 0) {
          topics.delete(name);
          dropped.push(name);
        } else {
          topics.set(name, count);
        }
      }
      if (!dropped.length) return;
      rawSend({ type: 'unsubscribe', topics: dropped });
      rawSend({ type: 'subscribe', topics: Array.from(topics.keys()) });
    };
  },

  /** Ouvre la connexion (sans effet si elle l'est déjà). */
  connect() {
    wanted = true;
    openSocket();
  },

  /** Ferme la connexion et suspend la reconnexion automatique. */
  close() {
    wanted = false;
    socket?.close();
  },

  /** @returns {'offline'|'connecting'|'live'} */
  get state() {
    return appStore.get().ws;
  },
});

/* --- Boucle de connexion --------------------------------------------------- */

const STATUS_KEYS = ['app', 'servers', 'network', 'proxies', 'totals', 'inventory'];
let started = false;
let attempt = 0;
let timer = 0;

function backoff() {
  const base = Math.min(15000, 2000 * 2 ** Math.max(0, attempt - 1));
  return Math.round(base * (0.85 + Math.random() * 0.3));
}

function schedule() {
  window.clearTimeout(timer);
  attempt += 1;
  timer = window.setTimeout(probe, backoff());
}

async function probe() {
  window.clearTimeout(timer);
  const online = await refreshStatus();
  if (!online) {
    schedule();
    return;
  }
  if (!appStore.get().catalog) refreshCatalog();
  ws.connect();
}

/**
 * Démarre la liaison avec le laboratoire : charge `/api/status` et `/api/catalog`, ouvre le
 * WebSocket, applique les messages `stats` au magasin et se reconnecte seule (délai croissant
 * de 2 à 15 s). Le WebSocket n'est tenté que si l'API HTTP répond. À appeler une seule fois.
 * @returns {void}
 */
export function startRuntime() {
  if (started) return;
  started = true;

  ws.on('stats', (message) => {
    const status = { ...(appStore.get().status ?? {}) };
    for (const key of STATUS_KEYS) if (message[key] !== undefined) status[key] = message[key];
    appStore.set({ stats: message, status, network: message.network ?? appStore.get().network });
  });
  ws.on('open', () => {
    attempt = 0;
  });
  ws.on('close', () => {
    if (wanted) schedule();
  });

  const wake = () => {
    if (document.visibilityState === 'visible' && appStore.get().ws === 'offline') {
      attempt = 0;
      probe();
    }
  };
  document.addEventListener('visibilitychange', wake);
  window.addEventListener('online', wake);

  probe();
}
