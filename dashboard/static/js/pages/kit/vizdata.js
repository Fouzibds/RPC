/**
 * Données d'exemple des sections « Graphiques » et « Code & octets » du guide de style :
 * ordres de grandeur réalistes d'un laboratoire tournant en boucle locale. Les tirages
 * sont déterministes (générateur à graine fixe) : le guide est identique à chaque visite.
 */

/** Taille des messages par procédure et par protocole, en octets (requête, réponse). */
export const PAYLOADS = Object.freeze({
  procedures: ['calculate_factorial', 'get_product_details', 'update_stock', 'list_products'],
  request: { custom: [75, 96, 118, 82], grpc: [7, 15, 17, 9], rest: [118, 172, 214, 164] },
  response: { custom: [141, 318, 142, 2890], grpc: [52, 121, 38, 1104], rest: [262, 486, 298, 3105] },
});

/** Lignes de code côté client pour écrire le même `update_stock` (transparence de localisation). */
export const CLIENT_CODE_LINES = Object.freeze([
  { protocol: 'rest', label: 'REST « à la main »', lines: 14, hint: 'URL, verbe, en-têtes, corps JSON, statut, décodage' },
  { protocol: 'grpc', label: 'Stub gRPC généré', lines: 6, hint: 'Canal, stub, message de requête typé' },
  { protocol: 'custom', label: 'Stub maison', lines: 3, hint: 'Proxy dynamique : stub.update_stock(…)' },
  { protocol: 'local', label: 'Appel local', lines: 1, hint: 'service.update_stock(…)' },
]);

/** Taille de la réponse `list_products` selon le nombre de produits, en octets. */
export const SCALING = Object.freeze({
  sizes: [1, 10, 100, 1000],
  custom: [338, 2890, 28412, 283650],
  grpc: [131, 1104, 10836, 108154],
  rest: [512, 3105, 28630, 283871],
});

function mulberry32(seed) {
  let state = seed;
  return () => {
    state |= 0;
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function percentile(sorted, ratio) {
  return sorted[Math.min(sorted.length - 1, Math.floor(ratio * sorted.length))];
}

/** Échantillons log-normaux (médiane, dispersion) avec quelques valeurs aberrantes, comme une vraie mesure. */
function latencySamples(seed, median, sigma, count = 1000) {
  const random = mulberry32(seed);
  const samples = [];
  for (let i = 0; i < count; i += 1) {
    const gauss = Math.sqrt(-2 * Math.log(1 - random())) * Math.cos(2 * Math.PI * random());
    const outlier = random() < 0.012 ? 2 + random() * 3 : 1;
    samples.push(median * Math.exp(sigma * gauss) * outlier);
  }
  return samples.sort((a, b) => a - b);
}

function profile(protocol, seed, median, sigma) {
  const samples = latencySamples(seed, median, sigma);
  const min = samples[0];
  const max = samples[samples.length - 1];
  const bins = 40;
  const ratio = (max / min) ** (1 / bins);
  const edges = Array.from({ length: bins + 1 }, (_, index) => min * ratio ** index);
  const counts = new Array(bins).fill(0);
  for (const sample of samples) {
    const index = Math.floor(Math.log(sample / min) / Math.log(ratio));
    counts[Math.min(bins - 1, Math.max(0, index))] += 1;
  }
  return {
    protocol,
    count: samples.length,
    mean_ms: samples.reduce((sum, value) => sum + value, 0) / samples.length,
    min_ms: min,
    median_ms: percentile(samples, 0.5),
    p95_ms: percentile(samples, 0.95),
    p99_ms: percentile(samples, 0.99),
    max_ms: max,
    histogram: { edges_ms: edges, counts },
  };
}

/**
 * Résultats de latence d'un benchmark `get_product_details` (1 000 appels par protocole), au
 * format de `run_latency_benchmark` : percentiles et histogramme `{ edges_ms, counts }`.
 */
export const LATENCY = Object.freeze([
  profile('local', 11, 0.0058, 0.16),
  profile('custom', 23, 0.132, 0.2),
  profile('grpc', 37, 0.296, 0.22),
  profile('rest', 51, 0.488, 0.24),
]);

/** Débit de départ de chaque protocole pour la courbe en direct (requêtes par seconde). */
export const THROUGHPUT = Object.freeze({ custom: 6890, grpc: 3092, rest: 1894 });

/**
 * Générateur de débit plausible : marche aléatoire bornée autour de la valeur nominale.
 * @param {number} seed
 * @returns {(nominal: number, previous?: number) => number}
 */
export function throughputWalk(seed) {
  const random = mulberry32(seed);
  return (nominal, previous = nominal) => {
    const pull = (nominal - previous) * 0.3;
    return Math.max(nominal * 0.6, previous + pull + (random() - 0.5) * nominal * 0.14);
  };
}

/** Décomposition du temps d'un appel JSON-RPC `update_stock` à travers le proxy, en microsecondes. */
export const CALL_STAGES = Object.freeze([
  { id: 'client.call', label: 'client.call', lane: 'client', start: 0, duration: 0, detail: 'update_stock("SKU-1001", -3)' },
  { id: 'client.marshal', label: 'client.marshal', lane: 'client', start: 2, duration: 21, detail: 'Message JSON-RPC 2.0 · 114 octets' },
  { id: 'client.send', label: 'client.send', lane: 'client', start: 23, duration: 9, detail: 'Trame : 4 octets de longueur + 114 octets' },
  { id: 'network.up', label: 'réseau · aller', lane: 'network', start: 32, duration: 62, detail: 'Traversée du proxy de chaos' },
  { id: 'server.receive', label: 'server.receive', lane: 'server', start: 94, duration: 0, detail: '118 octets reçus' },
  { id: 'server.unmarshal', label: 'server.unmarshal', lane: 'server', start: 96, duration: 14 },
  { id: 'server.dispatch', label: 'server.dispatch', lane: 'server', start: 110, duration: 6, detail: 'InventoryService.update_stock' },
  { id: 'server.execute', label: 'server.execute', lane: 'server', start: 116, duration: 12.4, detail: 'La procédure distante elle-même' },
  { id: 'server.marshal', label: 'server.marshal', lane: 'server', start: 128.4, duration: 9 },
  { id: 'server.send', label: 'server.send', lane: 'server', start: 137.4, duration: 6, detail: '142 octets émis' },
  { id: 'network.down', label: 'réseau · retour', lane: 'network', start: 143.4, duration: 58 },
  { id: 'client.receive', label: 'client.receive', lane: 'client', start: 201.4, duration: 0, detail: '142 octets reçus' },
  { id: 'client.unmarshal', label: 'client.unmarshal', lane: 'client', start: 203, duration: 17 },
  { id: 'client.return', label: 'client.return', lane: 'client', start: 221, duration: 0, detail: '{ new_stock: 117, applied: true }' },
]);

/** Scénario « panne serveur » : un client naïf et un client résilient face à la même coupure (millisecondes). */
export const OUTAGE_ITEMS = Object.freeze([
  { id: 'n1', lane: 'naive', start: 0, end: 38, kind: 'ok', label: '1', title: 'Appel 1', detail: 'update_stock → appliqué' },
  { id: 'n2', lane: 'naive', start: 600, end: 1600, kind: 'timeout', label: '2', title: 'Appel 2', detail: 'TIMEOUT — aucune réponse en 1,00 s' },
  { id: 'n3', lane: 'naive', start: 2000, end: 2012, kind: 'error', label: '3', title: 'Appel 3', detail: 'UNAVAILABLE — connexion refusée' },
  { id: 'n4', lane: 'naive', start: 3300, end: 3312, kind: 'error', label: '4', title: 'Appel 4', detail: 'UNAVAILABLE — connexion refusée' },
  { id: 'n5', lane: 'naive', start: 5200, end: 5236, kind: 'ok', label: '5', title: 'Appel 5', detail: 'Le serveur est revenu' },

  { id: 'r1', lane: 'resilient', start: 0, end: 36, kind: 'ok', label: '1', title: 'Appel 1', detail: 'update_stock → appliqué' },
  { id: 'r2a', lane: 'resilient', start: 600, end: 1100, kind: 'timeout', label: '2', title: 'Appel 2 · tentative 1', detail: 'TIMEOUT — échéance de 500 ms' },
  { id: 'r2b', lane: 'resilient', start: 1100, end: 1200, kind: 'backoff', title: 'Attente avant nouvel essai', detail: '100 ms (base)' },
  { id: 'r2c', lane: 'resilient', start: 1200, end: 1212, kind: 'error', title: 'Appel 2 · tentative 2', detail: 'UNAVAILABLE — connexion refusée' },
  { id: 'r2d', lane: 'resilient', start: 1212, end: 1412, kind: 'backoff', title: 'Attente avant nouvel essai', detail: '200 ms (× 2)' },
  { id: 'r2e', lane: 'resilient', start: 1412, end: 1424, kind: 'error', title: 'Appel 2 · tentative 3', detail: 'UNAVAILABLE — tentatives épuisées' },
  { id: 'rb', lane: 'resilient', start: 1424, end: 3424, kind: 'breaker', label: 'ouvert', title: 'Disjoncteur ouvert', detail: '3 échecs consécutifs : les appels sont refusés sans toucher le réseau pendant 2 s' },
  { id: 'r3', lane: 'resilient', start: 2000, kind: 'breaker', title: 'Appel 3 refusé', detail: 'CIRCUIT_OPEN — échec immédiat' },
  { id: 'r4', lane: 'resilient', start: 3300, kind: 'breaker', title: 'Appel 4 refusé', detail: 'CIRCUIT_OPEN — échec immédiat' },
  { id: 'r5', lane: 'resilient', start: 5200, end: 5238, kind: 'retry', label: '5', title: 'Appel 5 · essai en semi-ouvert', detail: 'Succès : le disjoncteur se referme' },
  { id: 'r6', lane: 'resilient', start: 5600, end: 5634, kind: 'ok', label: '6', title: 'Appel 6', detail: 'Retour à la normale' },
]);
