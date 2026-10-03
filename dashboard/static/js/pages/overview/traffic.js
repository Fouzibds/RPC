/**
 * Vue d'ensemble — « Générer du trafic » : une rafale mixte de vrais appels, pour voir le
 * laboratoire s'animer. Trois lots asynchrones (un par protocole distant) et quelques appels
 * synchrones, dont une écriture aussitôt compensée : le stock retrouve sa valeur de départ.
 */

import { REMOTE_PROTOCOL_IDS } from '../../core/protocols.js';

const BURST_COUNT = 12;
const TIMEOUT_MS = 4000;
const STOCK_DELTA = 3;

/** Référence produit réelle : tirée du catalogue du laboratoire, jamais inventée. */
async function pickProduct(api, store) {
  let products = store.get().catalog?.products;
  if (!products?.length) products = (await api.get('/api/catalog', { timeoutMs: 6000 }))?.products;
  if (!products?.length) return null;
  return products[Math.floor(Math.random() * products.length)].id;
}

function tally(result) {
  if (result?.mode === 'async') return { calls: Number(result.count) || 0, errors: Number(result.errors) || 0 };
  return { calls: 1, errors: result?.ok ? 0 : 1 };
}

/**
 * Lance la rafale et attend la fin de tous les appels.
 * Les appels distants passent par le proxy de chaos : ils subissent le réseau simulé affiché.
 * @param {{get: Function, post: Function}} api Client HTTP du laboratoire.
 * @param {{get: () => Object}} store Magasin de l'application (catalogue).
 * @returns {Promise<{calls: number, errors: number, wallMs: number}>}
 *   Lève l'`ApiError` du premier appel refusé si aucun n'a abouti (laboratoire injoignable).
 */
export async function generateTraffic(api, store) {
  const started = performance.now();
  const productId = await pickProduct(api, store);
  const call = (protocol, method, params, extra = {}) =>
    api.post('/api/call', { protocol, method, params, via_proxy: protocol !== 'local', timeout_ms: TIMEOUT_MS, ...extra }, { timeoutMs: 20000 });

  const jobs = [];
  if (productId) {
    for (const protocol of REMOTE_PROTOCOL_IDS) {
      jobs.push(call(protocol, 'get_product_details', { product_id: productId }, { mode: 'async', count: BURST_COUNT }));
    }
    // Une écriture par REST, compensée par JSON-RPC : le même objet métier derrière deux middlewares.
    jobs.push(
      call('rest', 'update_stock', { product_id: productId, delta: STOCK_DELTA }).then(async (first) => {
        if (!first?.ok) return [first];
        return [first, await call('custom', 'update_stock', { product_id: productId, delta: -STOCK_DELTA })];
      }),
    );
  }
  jobs.push(call('local', 'calculate_factorial', { n: 20 }));
  jobs.push(call('custom', 'calculate_factorial', { n: 12 }));
  jobs.push(call('grpc', 'calculate_factorial', { n: 30 }));

  const settled = await Promise.allSettled(jobs);
  const done = settled.filter((item) => item.status === 'fulfilled').flatMap((item) => item.value);
  if (!done.length) throw settled[0].reason;

  let calls = 0;
  let errors = 0;
  for (const result of done) {
    const part = tally(result);
    calls += part.calls;
    errors += part.errors;
  }
  return { calls, errors, wallMs: performance.now() - started };
}
