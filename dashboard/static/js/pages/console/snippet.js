/**
 * Console RPC — « Code équivalent » : le Python qu'un développeur écrirait pour l'appel configuré,
 * avec les modules du laboratoire (service local, stub maison, stub gRPC généré, HTTP à la main).
 * Le code est mis en forme comme le ferait Black, sur une largeur étroite : il tient dans le volet.
 */

import { sentParams } from './model.js';

/** Largeur visée, en caractères (le volet de gauche offre environ 46 colonnes). */
const WIDTH = 46;

/** Messages de requête du contrat `service.proto`, par méthode gRPC (le catalogue ne les expose pas). */
const GRPC_REQUESTS = Object.freeze({
  CalculateFactorial: 'FactorialRequest',
  GetProductDetails: 'ProductRequest',
  UpdateStock: 'UpdateStockRequest',
  ListProducts: 'ListProductsRequest',
  StreamAnalytics: 'AnalyticsRequest',
  BulkUpdateStock: 'UpdateStockRequest',
  CheckStock: 'ProductRequest',
});

/** Client `InventoryClient` de chaque protocole : c'est lui qu'enveloppe `ResilientClient`. */
const CLIENTS = Object.freeze({
  custom: { module: 'rpc_custom.inventory_binding', name: 'CustomInventoryClient' },
  grpc: { module: 'rpc_grpc.grpc_client', name: 'GrpcInventoryClient' },
  rest: { module: 'rest_api.rest_client', name: 'RestInventoryClient' },
});

const ITEM_NAMES = { stream_analytics: 'snapshot', check_stock: 'level' };

/* --- Mini-formateur : expressions imbriquées, éclatées quand la ligne déborde ----------- */

const call = (callee, ...items) => ({ open: `${callee}(`, close: ')', items: items.filter((item) => item !== null) });
const group = (open, close, items) => ({ open, close, items });
const kw = (name, value) => ({ key: `${name}=`, value });
const comprehension = (value, clause, open = '[', close = ']') => ({ comprehension: value, clause, open, close });

function flat(node) {
  if (typeof node === 'string') return node;
  if (node.key) return node.key + flat(node.value);
  if (node.comprehension) return `${node.open}${flat(node.comprehension)} ${node.clause}${node.close}`;
  return node.open + node.items.map(flat).join(', ') + node.close;
}

/** Écrit `lead + node + tail` à l'indentation donnée, sur une ligne si elle tient, éclatée sinon. */
function emit(out, node, { indent = 0, lead = '', tail = '' } = {}) {
  const pad = ' '.repeat(indent);
  const line = pad + lead + flat(node) + tail;
  if (typeof node === 'string' || line.length <= WIDTH) out.push(line);
  else if (node.key) emit(out, node.value, { indent, lead: lead + node.key, tail });
  else if (node.comprehension) {
    out.push(`${pad}${lead}${node.open}`);
    emit(out, node.comprehension, { indent: indent + 4 });
    out.push(`${pad}    ${node.clause}`, `${pad}${node.close}${tail}`);
  } else {
    // Comme Black : d'abord tous les arguments sur une seule ligne indentée, sinon un par ligne.
    const hugged = `${pad}    ${node.items.map(flat).join(', ')}`;
    out.push(pad + lead + node.open);
    if (hugged.length <= WIDTH) out.push(hugged);
    else for (const item of node.items) emit(out, item, { indent: indent + 4, tail: ',' });
    out.push(pad + node.close + tail);
  }
}

function importFrom(out, module, ...names) {
  emit(out, group('(', ')', names), { lead: `from ${module} import ` });
  const last = out.length - 1;
  if (!out[last].endsWith(')') || out[last] === ')') return;
  out[last] = out[last].replace(/ import \((.*)\)$/, ' import $1');
}

/**
 * Littéral Python d'une valeur JSON, sous forme d'expression formatable.
 * @param {*} value
 * @returns {string|Object}
 */
function py(value) {
  if (value === null || value === undefined) return 'None';
  if (value === true) return 'True';
  if (value === false) return 'False';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'string') return JSON.stringify(value);
  if (Array.isArray(value)) return group('[', ']', value.map(py));
  return group('{', '}', Object.entries(value).map(([key, item]) => ({ key: `${JSON.stringify(key)}: `, value: py(item) })));
}

const seconds = (ms) => (Number.isInteger(ms / 1000) ? `${ms / 1000}.0` : String(ms / 1000));

/** Arguments d'un appel Python : obligatoires en position, facultatifs nommés. */
const callArgs = (params) => params.map((entry) => (entry.required ? py(entry.value) : kw(entry.name, py(entry.value))));
const keywordArgs = (params) => params.map((entry) => kw(entry.name, py(entry.value)));

function threadPool(out, workers, count, submit) {
  out.push(`with ThreadPoolExecutor(${workers}) as pool:`);
  emit(out, comprehension(submit, `for _ in range(${count})`), { indent: 4, lead: 'futures = ' });
  out.push('    results = [f.result() for f in futures]');
}

/* --- Un générateur par protocole ----------------------------------------------------------- */

function local(out, ctx) {
  const { spec, params, mode, count, workers } = ctx;
  if (mode === 'async') importFrom(out, 'concurrent.futures', 'ThreadPoolExecutor');
  importFrom(out, 'common.inventory', 'InventoryService');
  out.push('', '# Même processus, même mémoire : aucun réseau.', 'service = InventoryService()');
  const target = `service.${spec.name}`;
  if (spec.kind === 'bidi_stream') {
    emit(out, py(params[0]?.value ?? []), { lead: 'for product_id in ', tail: ':' });
    out.push('    level = service.check_stock(product_id)');
  } else if (spec.kind === 'server_stream') {
    emit(out, call(target, ...callArgs(params)), { lead: 'for snapshot in ', tail: ':' });
    out.push('    print(snapshot)');
  } else if (mode === 'async') {
    threadPool(out, workers, count, call('pool.submit', target, ...callArgs(params)));
  } else {
    emit(out, call(target, ...callArgs(params)), { lead: `${spec.kind === 'client_stream' ? 'summary' : 'result'} = ` });
  }
}

function custom(out, ctx) {
  const { spec, params, mode, count, host, port, timeoutMs } = ctx;
  importFrom(out, 'rpc_custom', 'RpcClientStub');
  out.push('');
  emit(out, call('RpcClientStub', py(host), String(port), kw('timeout', seconds(timeoutMs))), { lead: 'stub = ' });
  if (spec.kind === 'server_stream') {
    out.push('# Une requête, puis N notifications.');
    emit(out, call('stub.stream', py(spec.name), ...callArgs(params)), { lead: 'for snapshot in ', tail: ':' });
    out.push('    print(snapshot)');
  } else if (mode === 'async') {
    out.push(`# ${count} requêtes sur une seule connexion TCP.`);
    emit(out, comprehension(call('stub.call_async', py(spec.name), ...callArgs(params)), `for _ in range(${count})`), { lead: 'futures = ' });
    out.push('results = [f.result() for f in futures]');
  } else {
    out.push('# Le stub sérialise, envoie, attend, décode.');
    emit(out, call(`stub.${spec.name}`, ...callArgs(params)), { lead: 'result = ' });
  }
}

function grpc(out, ctx) {
  const { spec, params, mode, count, host, port, timeoutMs } = ctx;
  const message = `pb.${GRPC_REQUESTS[spec.grpc_method] ?? `${spec.grpc_method}Request`}`;
  const timeout = kw('timeout', seconds(timeoutMs));
  const rpc = `stub.${spec.grpc_method}`;
  out.push('import grpc');
  importFrom(out, 'rpc_grpc.generated', 'service_pb2 as pb', 'service_pb2_grpc as pb_grpc');
  out.push('');
  emit(out, call('grpc.insecure_channel', py(`${host}:${port}`)), { lead: 'channel = ' });
  out.push('stub = pb_grpc.InventoryServiceStub(channel)');
  if (spec.kind === 'client_stream') {
    const updates = params[0]?.value ?? [];
    emit(out, group('[', ']', updates.map((update) => call(message, ...Object.entries(update).map(([key, value]) => kw(key, py(value)))))), { lead: 'requests = ' });
    out.push(`# ${updates.length} messages envoyés, une seule réponse.`);
    emit(out, call(rpc, 'iter(requests)', timeout), { lead: 'summary = ' });
  } else if (spec.kind === 'bidi_stream') {
    emit(out, py(params[0]?.value ?? []), { lead: 'product_ids = ' });
    emit(out, comprehension(call(message, kw('product_id', 'pid')), 'for pid in product_ids', '(', ')'), { lead: 'requests = ' });
    out.push('# Une réponse par référence, sur le même flux.');
    emit(out, call(rpc, 'requests', timeout), { lead: 'for level in ', tail: ':' });
    out.push('    print(level.product_id, level.stock)');
  } else if (spec.kind === 'server_stream') {
    emit(out, call(message, ...keywordArgs(params)), { lead: 'request = ' });
    emit(out, call(rpc, 'request', timeout), { lead: 'for snapshot in ', tail: ':' });
    out.push('    print(snapshot.orders_per_min)');
  } else if (mode === 'async') {
    emit(out, call(message, ...keywordArgs(params)), { lead: 'request = ' });
    out.push(`# ${count} flux HTTP/2 sur un seul canal.`);
    emit(out, comprehension(call(`${rpc}.future`, 'request', timeout), `for _ in range(${count})`), { lead: 'futures = ' });
    out.push('replies = [f.result() for f in futures]');
  } else {
    emit(out, call(rpc, call(message, ...keywordArgs(params)), timeout), { lead: 'reply = ' });
  }
}

/**
 * Traduit la route REST du catalogue (`POST /api/stock/{product_id}`) en verbe, chemin et corps.
 * @param {Object} spec
 * @param {Array<{name: string, value: *}>} params
 * @returns {{verb: string, path: string, body: Record<string, *>|null}}
 */
export function restRequest(spec, params) {
  const [verb, template = ''] = String(spec.rest_route ?? '').split(' ');
  const values = new Map(params.map((entry) => [entry.name, entry.value]));
  const used = new Set();
  const fill = (text) =>
    text.replace(/\{(\w+)\}/g, (_, name) => {
      used.add(name);
      return encodeURIComponent(String(values.get(name) ?? ''));
    });
  const [route, queryTemplate] = template.split('?');
  const query = (queryTemplate ?? '')
    .split('&')
    .filter(Boolean)
    .filter((pair) => {
      const name = /\{(\w+)\}/.exec(pair)?.[1];
      if (name) used.add(name);
      return !name || (values.has(name) && values.get(name) !== '');
    })
    .map(fill)
    .join('&');
  const path = `${fill(route)}${query ? `?${query}` : ''}`;
  const rest = params.filter((entry) => !used.has(entry.name));
  const body = verb !== 'GET' && rest.length ? Object.fromEntries(rest.map((entry) => [entry.name, entry.value])) : null;
  return { verb, path, body };
}

function rest(out, ctx) {
  const { spec, params, mode, count, host, port, timeoutMs, workers } = ctx;
  const { verb, path, body } = restRequest(spec, params);
  const stream = spec.kind === 'server_stream';
  const headers = py(body ? { 'Content-Type': 'application/json', Accept: 'application/json' } : { Accept: stream ? 'application/x-ndjson' : 'application/json' });
  const open = call('http.client.HTTPConnection', py(host), String(port), kw('timeout', seconds(timeoutMs)));
  const send = call('conn.request', py(verb), py(path), body ? kw('body', call('json.dumps', py(body))) : null, kw('headers', headers));
  const batch = mode === 'async' && !stream;
  out.push('import http.client', 'import json');
  if (batch) importFrom(out, 'concurrent.futures', 'ThreadPoolExecutor');
  out.push('');
  if (batch) {
    out.push('# HTTP/1.1 : une connexion par appel en cours.', 'def call():');
    emit(out, open, { indent: 4, lead: 'conn = ' });
    emit(out, send, { indent: 4 });
    out.push('    response = conn.getresponse()', '    return json.loads(response.read())', '');
    threadPool(out, workers, count, call('pool.submit', 'call'));
    return;
  }
  out.push('# Sans stub : tout s’écrit à la main.');
  emit(out, open, { lead: 'conn = ' });
  emit(out, send);
  out.push('response = conn.getresponse()');
  if (stream) out.push('# NDJSON : un objet JSON par ligne.', 'for line in response:', '    snapshot = json.loads(line)');
  else out.push('# Le statut HTTP reste à vérifier soi-même.', 'result = json.loads(response.read())');
}

/** Avec une politique : le même contrat, enveloppé par `netsim.ResilientClient`. */
function resilient(out, ctx) {
  const { spec, params, mode, count, host, port, timeoutMs, policy, protocol, workers } = ctx;
  const client = CLIENTS[protocol];
  if (mode === 'async') importFrom(out, 'concurrent.futures', 'ThreadPoolExecutor');
  importFrom(out, 'netsim', ...(policy.breaker ? ['CircuitBreaker'] : []), 'ResilientClient', 'RetryPolicy');
  if (client) importFrom(out, client.module, client.name);
  else {
    importFrom(out, 'common.client_api', 'LocalInventoryClient');
    importFrom(out, 'common.inventory', 'InventoryService');
  }
  out.push('');
  const inner = client ? call(client.name, py(host), String(port), kw('timeout', seconds(timeoutMs))) : call('LocalInventoryClient', 'InventoryService()');
  emit(
    out,
    group('ResilientClient(', ')', [
      inner,
      kw('retry', call('RetryPolicy', kw('max_attempts', String(policy.max_attempts)), kw('base_delay_ms', String(policy.base_delay_ms)))),
      ...(policy.breaker ? [kw('breaker', 'CircuitBreaker()')] : []),
      ...(policy.auto_idempotency_key ? [kw('auto_idempotency_key', 'True')] : []),
    ]),
    { lead: 'client = ' },
  );
  const item = ITEM_NAMES[spec.name] ?? 'item';
  const target = `client.${spec.name}`;
  if (spec.kind === 'server_stream' || spec.kind === 'bidi_stream') {
    emit(out, call(target, ...callArgs(params)), { lead: `for ${item} in `, tail: ':' });
    out.push(`    print(${item})`);
  } else if (mode === 'async') {
    threadPool(out, workers, count, call('pool.submit', target, ...callArgs(params)));
  } else {
    out.push('# Même appel : le client gère les tentatives.');
    emit(out, call(target, ...callArgs(params)), { lead: 'result = ' });
  }
}

const BUILDERS = { local, custom, grpc, rest };

/**
 * Code Python équivalent à l'appel configuré.
 * @param {Object} options
 * @param {import('./model.js').ConsoleConfig} options.config
 * @param {Object} options.spec `MethodSpec` de la procédure choisie.
 * @param {{host: string, port: number|null}} options.endpoint Adresse visée (serveur ou proxy de chaos).
 * @param {number} [options.workers=16] Appels simultanés d'un lot (`limits.async_workers`).
 * @returns {{code: string, title: string}}
 */
export function buildSnippet({ config, spec, endpoint, workers = 16 }) {
  const ctx = {
    spec,
    params: sentParams(spec, config.values[spec.name]),
    protocol: config.protocol,
    mode: config.mode,
    count: config.count,
    host: endpoint.host,
    port: endpoint.port ?? 0,
    timeoutMs: config.timeoutMs,
    policy: config.policy,
    workers,
  };
  const out = [];
  (config.policy.enabled ? resilient : BUILDERS[config.protocol])(out, ctx);
  const suffix = { local: 'local', custom: 'rpc_maison', grpc: 'grpc', rest: 'rest' }[config.protocol];
  return { code: out.join('\n'), title: `${spec.name}_${suffix}.py` };
}
