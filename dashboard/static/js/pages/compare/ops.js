/**
 * Transparence — opérations sur le laboratoire partagées par les sections exécutables :
 * appel d'une procédure, lecture du stock, remise du stock à sa valeur de départ.
 */

/** Nom des arguments de `update_stock` (catalogue du laboratoire). */
const PRODUCT_PARAM = 'product_id';
const DELTA_PARAM = 'delta';

/**
 * Exécute `update_stock(product_id, delta)` par `POST /api/call`.
 * @param {Object} api Client HTTP (`core/api.js`).
 * @param {Object} options
 * @param {string} options.protocol `local`, `custom`, `grpc` ou `rest`.
 * @param {string} options.method Procédure du catalogue.
 * @param {string} options.productId
 * @param {number} options.delta Variation de stock (négative = sortie).
 * @param {boolean} [options.viaProxy] Passe par le proxy de chaos.
 * @param {number} [options.timeoutMs] Délai d'attente du client.
 * @param {AbortSignal} [options.signal]
 * @returns {Promise<Object>} `{ok, result, error, duration_ms, request_bytes, response_bytes, cold, call_id…}`.
 */
export function updateStock(api, { protocol, method, productId, delta, viaProxy = false, timeoutMs, signal }) {
  const body = { protocol, method, params: { [PRODUCT_PARAM]: productId, [DELTA_PARAM]: delta }, via_proxy: viaProxy };
  if (timeoutMs) body.timeout_ms = timeoutMs;
  return api.post('/api/call', body, { signal });
}

/**
 * Fiche du produit lue par un appel local (`get_product_details`) : la source de vérité du stock.
 * @param {Object} api
 * @param {string} productId
 * @param {AbortSignal} [signal]
 * @returns {Promise<{stock: number|null, name: string}>} `stock` vaut `null` si le produit est introuvable.
 */
export async function readProduct(api, productId, signal) {
  const reply = await api.post('/api/call', { protocol: 'local', method: 'get_product_details', params: { [PRODUCT_PARAM]: productId } }, { signal });
  const stock = Number(reply?.result?.stock);
  return { stock: reply?.ok && Number.isFinite(stock) ? stock : null, name: reply?.result?.name ?? '' };
}

/**
 * Remet le stock à `expected` : lit le stock réel puis corrige l'écart par un appel local.
 * Lire avant de corriger compte : après un délai dépassé, on ignore si le serveur a exécuté l'appel.
 * @param {Object} api
 * @param {string} method Procédure de mise à jour (`update_stock`).
 * @param {string} productId
 * @param {number} expected Stock à retrouver.
 * @param {AbortSignal} [signal]
 * @returns {Promise<{found: number|null, stock: number|null}>} `found` = stock constaté avant correction.
 */
export async function restoreStock(api, method, productId, expected, signal) {
  const { stock: found } = await readProduct(api, productId, signal);
  if (found === null || found === expected) return { found, stock: found };
  const reply = await updateStock(api, { protocol: 'local', method, productId, delta: expected - found, signal });
  const stock = Number(reply?.result?.new_stock);
  return { found, stock: reply?.ok && Number.isFinite(stock) ? stock : null };
}

/**
 * Pause annulable.
 * @param {number} ms
 * @param {AbortSignal} [signal]
 * @returns {Promise<void>}
 */
export function sleep(ms, signal) {
  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, ms);
    signal?.addEventListener(
      'abort',
      () => {
        window.clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}
