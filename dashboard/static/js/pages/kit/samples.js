/**
 * Données d'exemple réalistes pour le guide de style : mesures de benchmark et
 * évènements de trace (octets réellement conformes aux trois formats de fil).
 */

const encoder = new TextEncoder();

function toHex(bytes) {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function concat(...parts) {
  const out = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

function uint32be(value) {
  return new Uint8Array([(value >>> 24) & 0xff, (value >>> 16) & 0xff, (value >>> 8) & 0xff, value & 0xff]);
}

/** Trame JSON-RPC maison : longueur sur 4 octets (gros-boutiste) + JSON compact. */
function jsonRpcFrame(message) {
  const body = encoder.encode(JSON.stringify(message));
  return concat(uint32be(body.length), body);
}

function varint(value) {
  const bytes = [];
  let rest = value;
  while (rest > 0x7f) {
    bytes.push((rest & 0x7f) | 0x80);
    rest >>>= 7;
  }
  bytes.push(rest);
  return new Uint8Array(bytes);
}

function protoString(field, text) {
  const body = encoder.encode(text);
  return concat(varint((field << 3) | 2), varint(body.length), body);
}

function protoInt(field, value) {
  return concat(varint(field << 3), varint(value));
}

/** Message gRPC : drapeau de compression (1 octet) + longueur (4 octets) + Protobuf. */
function grpcFrame(...fields) {
  const body = concat(...fields);
  return concat(new Uint8Array([0]), uint32be(body.length), body);
}

/** Résultats de latence par protocole (ordre de grandeur d'une boucle locale). */
export const BENCH_ROWS = Object.freeze([
  { protocol: 'local', method: 'get_product_details', count: 1000, mean_ms: 0.0061, p95_ms: 0.0092, p99_ms: 0.021, request_bytes: 0, response_bytes: 0, rps: 158400, errors: 0 },
  { protocol: 'custom', method: 'get_product_details', count: 1000, mean_ms: 0.142, p95_ms: 0.213, p99_ms: 0.482, request_bytes: 96, response_bytes: 318, rps: 6890, errors: 0 },
  { protocol: 'grpc', method: 'get_product_details', count: 1000, mean_ms: 0.318, p95_ms: 0.471, p99_ms: 0.925, request_bytes: 15, response_bytes: 121, rps: 3092, errors: 0 },
  { protocol: 'rest', method: 'get_product_details', count: 1000, mean_ms: 0.521, p95_ms: 0.784, p99_ms: 1.63, request_bytes: 172, response_bytes: 486, rps: 1894, errors: 2 },
]);

/** Derniers appels (historique de console). */
export const CALL_ROWS = Object.freeze([
  { call_id: 'grpc-000128', protocol: 'grpc', method: 'update_stock', status: 'ok', duration_ms: 0.41, bytes: 58 },
  { call_id: 'custom-000127', protocol: 'custom', method: 'list_products', status: 'ok', duration_ms: 1.92, bytes: 4812 },
  { call_id: 'rest-000126', protocol: 'rest', method: 'get_product_details', status: 'error', duration_ms: 5004, bytes: 172 },
  { call_id: 'custom-000125', protocol: 'custom', method: 'calculate_factorial', status: 'ok', duration_ms: 0.18, bytes: 141 },
  { call_id: 'grpc-000124', protocol: 'grpc', method: 'stream_analytics', status: 'ok', duration_ms: 2013, bytes: 1260 },
]);

/**
 * Évènements de trace d'exemple, horodatés à partir de maintenant.
 * @returns {Object[]} Évènements au format `TraceEvent.to_dict()`.
 */
export function sampleTraceEvents() {
  const now = Date.now() / 1000;
  const base = performance.now() * 1e6;
  const event = (index, offsetUs, fields) => ({ seq: index, ts: now + offsetUs / 1e6, t_ns: base + offsetUs * 1000, detail: {}, ...fields });

  const customRequest = jsonRpcFrame({
    jsonrpc: '2.0',
    id: 'custom-000042',
    method: 'update_stock',
    params: { product_id: 'SKU-1001', delta: -3, idempotency_key: '' },
  });
  const customResponse = jsonRpcFrame({
    jsonrpc: '2.0',
    id: 'custom-000042',
    result: { product_id: 'SKU-1001', previous_stock: 120, new_stock: 117, applied: true, duplicate: false },
  });
  const grpcRequest = grpcFrame(protoString(1, 'SKU-1001'));
  const grpcResponse = grpcFrame(
    protoString(1, 'SKU-1001'),
    protoString(2, 'Clavier mécanique 75 %'),
    protoString(3, 'peripherals'),
    protoInt(5, 117),
    protoInt(6, 8990),
  );
  const restRequest = encoder.encode(
    'GET /api/products/SKU-1001 HTTP/1.1\r\nHost: 127.0.0.1:8181\r\nAccept: application/json\r\nX-Call-Id: rest-000044\r\nConnection: keep-alive\r\n\r\n',
  );
  const restResponse = encoder.encode(
    'HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: 87\r\n\r\n{"id":"SKU-1001","name":"Clavier mécanique 75 %","category":"peripherals","stock":117}',
  );

  const custom = { call_id: 'custom-000042', protocol: 'custom', method: 'update_stock' };
  const grpc = { call_id: 'grpc-000043', protocol: 'grpc', method: 'get_product_details' };
  const rest = { call_id: 'rest-000044', protocol: 'rest', method: 'get_product_details' };
  const lost = { call_id: 'custom-000045', protocol: 'custom', method: 'update_stock' };

  return [
    event(1, 0, { ...custom, side: 'client', stage: 'client.send', size: customRequest.length, payload_hex: toHex(customRequest), duration_us: 21 }),
    event(2, 96, { ...custom, side: 'server', stage: 'server.receive', size: customRequest.length, payload_hex: toHex(customRequest) }),
    event(3, 131, { ...custom, side: 'server', stage: 'server.execute', duration_us: 12.4, detail: { target: 'InventoryService.update_stock' } }),
    event(4, 164, { ...custom, side: 'server', stage: 'server.send', size: customResponse.length, payload_hex: toHex(customResponse), duration_us: 9 }),
    event(5, 238, { ...custom, side: 'client', stage: 'client.receive', size: customResponse.length, payload_hex: toHex(customResponse), duration_us: 217 }),

    event(6, 900000, { ...grpc, side: 'client', stage: 'client.send', size: grpcRequest.length, payload_hex: toHex(grpcRequest), duration_us: 38 }),
    event(7, 900210, { ...grpc, side: 'server', stage: 'server.receive', size: grpcRequest.length, payload_hex: toHex(grpcRequest) }),
    event(8, 900395, { ...grpc, side: 'server', stage: 'server.send', size: grpcResponse.length, payload_hex: toHex(grpcResponse), duration_us: 14 }),
    event(9, 900612, { ...grpc, side: 'client', stage: 'client.receive', size: grpcResponse.length, payload_hex: toHex(grpcResponse), duration_us: 574 }),

    event(10, 1800000, { ...rest, side: 'client', stage: 'client.send', size: restRequest.length, payload_hex: toHex(restRequest), duration_us: 44 }),
    event(11, 1840300, { ...rest, side: 'network', stage: 'network.delay', duration_us: 40000, detail: { direction: 'up', delay_ms: 40 } }),
    event(12, 1882100, { ...rest, side: 'client', stage: 'client.receive', size: restResponse.length, payload_hex: toHex(restResponse), duration_us: 82050 }),

    event(13, 2700000, { ...lost, side: 'client', stage: 'client.send', size: customRequest.length, payload_hex: toHex(customRequest), duration_us: 19 }),
    event(14, 2700400, { ...lost, side: 'network', stage: 'network.lost_reply', detail: { hold_ms: 250 } }),
    event(15, 7700400, { ...lost, side: 'client', stage: 'client.error', duration_us: 5000400, detail: { code: 'TIMEOUT', message: 'Aucune réponse en 5,00 s' } }),
  ];
}
