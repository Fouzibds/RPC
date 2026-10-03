/**
 * Extraits de code et messages d'exemple de la section « Code & octets » du guide de style :
 * les octets sont ceux que le laboratoire échange réellement pour `update_stock("SKU-1001", -3)`.
 */

const encoder = new TextEncoder();

const toHex = (bytes) => Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');

/** Message gRPC `UpdateStockRequest` : préfixe de 5 octets puis Protobuf (segments de `wire_inspector.frame_segments`). */
export const GRPC_REQUEST = Object.freeze({
  hex: '000000000c0a08534b552d313030311005',
  segments: [
    { label: 'Drapeau de compression', start: 0, end: 1, kind: 'frame', value: 0 },
    { label: 'Longueur du message', start: 1, end: 5, kind: 'frame', value: 12 },
    { label: 'Tag — champ 1 « product_id », type de fil 2 (LEN)', start: 5, end: 6, kind: 'tag', value: 10, field: 'product_id', number: 1, wire_type: 2, wire_type_name: 'LEN', depth: 0, type: 'string' },
    { label: 'Longueur du champ 1 « product_id » : 8 octets', start: 6, end: 7, kind: 'len', value: 8, field: 'product_id', number: 1, wire_type: 2, wire_type_name: 'LEN', depth: 0, type: 'string' },
    { label: 'Valeur du champ 1 « product_id » (string)', start: 7, end: 15, kind: 'value', value: 'SKU-1001', field: 'product_id', number: 1, wire_type: 2, wire_type_name: 'LEN', depth: 0, type: 'string' },
    { label: 'Tag — champ 2 « delta », type de fil 0 (VARINT)', start: 15, end: 16, kind: 'tag', value: 16, field: 'delta', number: 2, wire_type: 0, wire_type_name: 'VARINT', depth: 0, type: 'sint32' },
    { label: 'Valeur du champ 2 « delta » (sint32)', start: 16, end: 17, kind: 'value', value: -3, field: 'delta', number: 2, wire_type: 0, wire_type_name: 'VARINT', depth: 0, type: 'sint32' },
  ],
});

/**
 * Les mêmes octets lus avec deux contrats : le lecteur v2 attend une chaîne « warehouse » au champ
 * n° 2 et y trouve un entier — la quantité « delta » du client v1 est perdue sans erreur.
 */
export const GRPC_REQUEST_READINGS = Object.freeze([
  { id: 'v1', label: 'Contrat v1', segments: GRPC_REQUEST.segments },
  {
    id: 'v2',
    label: 'Contrat v2',
    segments: [
      ...GRPC_REQUEST.segments.slice(0, 5),
      { label: 'Tag — champ 2 « warehouse » attendu en LEN, reçu en VARINT', start: 15, end: 16, kind: 'tag', value: 16, field: 'warehouse', number: 2, wire_type: 0, wire_type_name: 'VARINT', depth: 0, type: 'string', mismatch: true },
      { label: 'Valeur du champ 2 : incompatible avec « warehouse » (string), ignorée', start: 16, end: 17, kind: 'value', value: 5, field: 'warehouse', number: 2, wire_type: 0, wire_type_name: 'VARINT', depth: 0, type: 'string', mismatch: true },
    ],
  },
]);

/** Réponse gRPC `Product` : champs répétés et sous-messages (130 octets). */
export const GRPC_PRODUCT = Object.freeze({
  hex: '000000007d0a08534b552d313030311217436c6176696572206dc3a963616e6971756520373520251a0b7065726970686572616c73219a99999999795640287532055041522d313a057573622d633a08686f742d737761704212090000000000404040213d0ad7a3703dea3f4a100a084b65796368726f6e1202484b180c58016807',
  segments: (() => {
    const out = [
      { label: 'Drapeau de compression', start: 0, end: 1, kind: 'frame', value: 0 },
      { label: 'Longueur du message', start: 1, end: 5, kind: 'frame', value: 125 },
    ];
    let offset = 5;
    const field = (name, number, wire, wireName, type, depth, length, value) => {
      const shared = { field: name, number, wire_type: wire, wire_type_name: wireName, depth, type };
      out.push({ label: `Tag — champ ${number} « ${name} », type de fil ${wire} (${wireName})`, start: offset, end: offset + 1, kind: 'tag', value: (number << 3) | wire, ...shared });
      offset += 1;
      if (wire === 2) {
        out.push({ label: `Longueur du champ ${number} « ${name} » : ${length} octets`, start: offset, end: offset + 1, kind: 'len', value: length, ...shared });
        offset += 1;
      }
      if (type !== 'message') {
        out.push({ label: `Valeur du champ ${number} « ${name} » (${type})`, start: offset, end: offset + length, kind: 'value', value, ...shared });
        offset += length;
      }
    };
    field('id', 1, 2, 'LEN', 'string', 0, 8, 'SKU-1001');
    field('name', 2, 2, 'LEN', 'string', 0, 23, 'Clavier mécanique 75 %');
    field('category', 3, 2, 'LEN', 'string', 0, 11, 'peripherals');
    field('price', 4, 1, 'I64', 'double', 0, 8, 89.9);
    field('stock', 5, 0, 'VARINT', 'int32', 0, 1, 117);
    field('warehouse', 6, 2, 'LEN', 'string', 0, 5, 'PAR-1');
    field('tags', 7, 2, 'LEN', 'string', 0, 5, 'usb-c');
    field('tags', 7, 2, 'LEN', 'string', 0, 8, 'hot-swap');
    field('dimensions', 8, 2, 'LEN', 'message', 0, 18);
    field('width_cm', 1, 1, 'I64', 'double', 1, 8, 32.5);
    field('weight_kg', 4, 1, 'I64', 'double', 1, 8, 0.82);
    field('supplier', 9, 2, 'LEN', 'message', 0, 16);
    field('name', 1, 2, 'LEN', 'string', 1, 8, 'Keychron');
    field('country', 2, 2, 'LEN', 'string', 1, 2, 'HK');
    field('lead_time_days', 3, 0, 'VARINT', 'uint32', 1, 1, 12);
    field('active', 11, 0, 'VARINT', 'bool', 0, 1, true);
    field('version', 13, 0, 'VARINT', 'uint32', 0, 1, 7);
    return out;
  })(),
});

const jsonRpcMessage = { jsonrpc: '2.0', id: 'custom-000042', method: 'update_stock', params: { product_id: 'SKU-1001', delta: -3, idempotency_key: '' } };
const jsonRpcBody = encoder.encode(JSON.stringify(jsonRpcMessage));

/** Trame JSON-RPC maison : longueur sur 4 octets (gros-boutiste) puis le message JSON compact. */
export const JSONRPC_FRAME = Object.freeze({
  message: jsonRpcMessage,
  hex: toHex(new Uint8Array([0, 0, 0, jsonRpcBody.length])) + toHex(jsonRpcBody),
  segments: [
    { label: 'Longueur (uint32 big-endian)', start: 0, end: 4, kind: 'frame', value: jsonRpcBody.length },
    { label: 'Message JSON-RPC 2.0', start: 4, end: 4 + jsonRpcBody.length, kind: 'body' },
  ],
});

/** Réponse du service, telle que la console l'affiche. */
export const RPC_RESULT = Object.freeze({
  jsonrpc: '2.0',
  id: 'custom-000042',
  result: {
    product_id: 'SKU-1001',
    previous_stock: 120,
    new_stock: 117,
    delta: -3,
    applied: true,
    duplicate: false,
    updated_at_ms: 1790947927412,
    product: {
      id: 'SKU-1001',
      name: 'Clavier mécanique 75 %',
      category: 'peripherals',
      price: 89.9,
      tags: ['usb-c', 'hot-swap', 'rgb'],
      supplier: { name: 'Keychron', country: 'HK', lead_time_days: 12 },
      description:
        'Clavier mécanique compact à 84 touches, commutateurs remplaçables à chaud, connexion USB-C et Bluetooth 5.1, rétroéclairage par touche, châssis en aluminium usiné.',
      discontinued: null,
    },
  },
});

const httpBody = '{"delta":-3,"idempotency_key":""}';
const httpLine = 'POST /api/stock/SKU-1001 HTTP/1.1\r\n';
const httpHead = `${httpLine}Host: 127.0.0.1:8181\r\nContent-Type: application/json\r\nContent-Length: ${encoder.encode(httpBody).length}\r\nX-Call-Id: rest-000044\r\nConnection: keep-alive\r\n\r\n`;

/** Requête REST complète : ligne de requête, en-têtes, corps JSON. */
export const HTTP_REQUEST = Object.freeze({
  text: httpHead + httpBody,
  hex: toHex(encoder.encode(httpHead + httpBody)),
  segments: [
    { label: 'POST /api/stock/SKU-1001 HTTP/1.1', start: 0, end: httpLine.length, kind: 'header' },
    { label: 'En-têtes HTTP', start: httpLine.length, end: httpHead.length, kind: 'header' },
    { label: 'Corps JSON', start: httpHead.length, end: httpHead.length + encoder.encode(httpBody).length, kind: 'body' },
  ],
});

/** Réponse HTTP correspondante. */
export const HTTP_RESPONSE = 'HTTP/1.1 200 OK\nContent-Type: application/json; charset=utf-8\nContent-Length: 96\nX-Call-Id: rest-000044\n\n{"product_id":"SKU-1001","previous_stock":120,"new_stock":117,"applied":true,"duplicate":false}';

/** Le même appel écrit avec le stub maison (transparence de localisation). */
export const PYTHON_STUB = `from rpc_custom.client_stub import RpcClientStub
from common.errors import RpcTimeoutError

# Le stub se comporte comme un objet local.
with RpcClientStub("127.0.0.1", 9101, timeout=0.5) as stub:
    try:
        reply = stub.update_stock("SKU-1001", -3)
        print(f"stock restant : {reply['new_stock']}")
    except RpcTimeoutError as error:
        # L'effet a-t-il eu lieu ? Impossible de le savoir.
        raise SystemExit(f"appel perdu : {error}")
`;

/** L'appel REST écrit « à la main » dans un navigateur. */
export const JS_FETCH = `const response = await fetch(\`/api/stock/\${productId}\`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ delta: -3, idempotency_key: key }),
});
if (!response.ok) throw new Error(\`HTTP \${response.status}\`);
const { new_stock } = await response.json(); // 117
`;

/** Lancement du laboratoire. */
export const SHELL_COMMANDS = `# Banc d'essai complet, puis tableau de bord
$ python main.py --benchmark --iterations 1000
$ python main.py --call update_stock SKU-1001 -3 --protocol grpc
$ RPCX_PORT_OFFSET=100 python main.py --dashboard | tee lab.log
`;

/** Contrat v1 (extrait de `rpc_grpc/protos/service.proto`). */
export const PROTO_V1 = `syntax = "proto3";

package rpcexplorer.v1;

service InventoryService {
  rpc CalculateFactorial (FactorialRequest) returns (FactorialReply);
  rpc GetProductDetails (ProductRequest) returns (Product);
  rpc UpdateStock (UpdateStockRequest) returns (UpdateStockReply);
}

message FactorialRequest {
  uint32 n = 1;
}

message FactorialReply {
  uint32 n = 1;
  string result = 2;
  uint32 digits = 3;
  double compute_us = 4;
}

message ProductRequest {
  string product_id = 1;
}

message Product {
  string id = 1;
  string name = 2;
  string category = 3;
  double price = 4;
  int32 stock = 5;
  string warehouse = 6;
  repeated string tags = 7;
}

message UpdateStockRequest {
  string product_id = 1;
  sint32 delta = 2;
  string idempotency_key = 3;
}
`;

/** Contrat v2 « mal évolué » (extrait de `service_v2.proto`). */
export const PROTO_V2 = `syntax = "proto3";

package rpcexplorer.v2;

service InventoryService {
  rpc CalculateFactorial (FactorialRequest) returns (FactorialReply);
  rpc GetProduct (ProductRequest) returns (Product);
  rpc UpdateStock (UpdateStockRequest) returns (UpdateStockReply);
}

message FactorialRequest {
  uint32 n = 1;
  bool use_cache = 2;
}

message FactorialReply {
  uint32 n = 1;
  string result = 2;
  uint32 digits = 3;
  double compute_us = 4;
}

message ProductRequest {
  string product_id = 1;
}

message Product {
  string id = 1;
  string name = 2;
  string category = 3;
  int64 price_cents = 4;
  int32 reserved = 5;
  string warehouse = 6;
  repeated string tags = 7;
  int32 stock = 15;
}

message UpdateStockRequest {
  string product_id = 1;
  string warehouse = 2;
  string idempotency_key = 3;
  sint32 delta = 4;
}
`;

/**
 * Diff précalculé « façon serveur » (`GET /api/contract` : `kind`, `left_no`, `right_no`) des deux
 * réponses JSON de la démonstration du mode unifié.
 */
export const JSON_DIFF_ROWS = Object.freeze([
  { kind: 'same', left_no: 1, right_no: 1 },
  { kind: 'same', left_no: 2, right_no: 2 },
  { kind: 'same', left_no: 3, right_no: 3 },
  { kind: 'removed', left_no: 4, right_no: null },
  { kind: 'removed', left_no: 5, right_no: null },
  { kind: 'added', left_no: null, right_no: 4 },
  { kind: 'added', left_no: null, right_no: 5 },
  { kind: 'added', left_no: null, right_no: 6 },
  { kind: 'same', left_no: 6, right_no: 7 },
]);

/** Annotations du diff v1 → v2 (numéros de ligne dans le texte de droite). */
export const PROTO_NOTES = Object.freeze([
  { line: 7, side: 'right', tone: 'danger', label: 'BREAKING', text: 'RPC renommée : un client v1 appelle GetProductDetails et reçoit UNIMPLEMENTED.' },
  { line: 13, side: 'right', tone: 'success', label: 'COMPATIBLE', text: 'Nouveau champ, nouveau numéro : les anciens clients l’ignorent.' },
  { line: 31, side: 'right', tone: 'danger', label: 'BREAKING', text: 'Type de fil changé (64 bits → varint) : un client v1 lit un prix à 0.' },
  { line: 32, side: 'right', tone: 'danger', label: 'BREAKING' },
  { line: 40, side: 'right', tone: 'danger', label: 'BREAKING', text: 'N° 2 réutilisé avec un autre type : le serveur v2 lit « delta » à 0 — corruption silencieuse.' },
]);
