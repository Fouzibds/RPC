/**
 * Les quatre « protocoles » comparés par le laboratoire : identité visuelle unique,
 * utilisée par les puces, les graphiques et les pipelines.
 */

/**
 * @typedef {Object} ProtocolInfo
 * @property {'local'|'custom'|'grpc'|'rest'} id
 * @property {string} label Libellé complet (« JSON-RPC maison »).
 * @property {string} short Libellé court (« JSON-RPC »).
 * @property {string} transport Résumé du transport.
 * @property {string} icon Nom d'icône (voir `core/icons.js`).
 * @property {string} cssVar Nom de la variable CSS de couleur (`--proto-grpc`).
 * @property {string} color Couleur pleine, prête pour une déclaration CSS (`var(--proto-grpc)`).
 * @property {string} fg Couleur de texte lisible dans les deux thèmes.
 * @property {string} soft Fond translucide.
 * @property {string} line Liseré translucide.
 * @property {number|null} port Port par défaut du serveur (accès direct), `null` pour l'appel local.
 */

function define(id, label, short, transport, icon, port) {
  return Object.freeze({
    id,
    label,
    short,
    transport,
    icon,
    port,
    cssVar: `--proto-${id}`,
    color: `var(--proto-${id})`,
    fg: `var(--proto-${id}-fg)`,
    soft: `var(--proto-${id}-soft)`,
    line: `var(--proto-${id}-line)`,
  });
}

/** @type {Readonly<Record<string, ProtocolInfo>>} */
export const PROTOCOLS = Object.freeze({
  local: define('local', 'Appel local', 'Local', 'Aucun — appel de fonction en mémoire', 'cpu', null),
  custom: define('custom', 'JSON-RPC maison', 'JSON-RPC', 'TCP + trame préfixée par sa longueur + JSON-RPC 2.0', 'braces', 9101),
  grpc: define('grpc', 'gRPC / Protobuf', 'gRPC', 'HTTP/2 + Protobuf binaire (contrat IDL)', 'binary', 50051),
  rest: define('rest', 'REST / JSON', 'REST', 'HTTP/1.1 + JSON (ressources & verbes)', 'globe', 8081),
});

/** Identifiants dans l'ordre d'affichage canonique. */
export const PROTOCOL_IDS = Object.freeze(['local', 'custom', 'grpc', 'rest']);

/** Protocoles qui traversent réellement le réseau. */
export const REMOTE_PROTOCOL_IDS = Object.freeze(['custom', 'grpc', 'rest']);

/**
 * Description d'un protocole. Un identifiant inconnu donne une fiche neutre (jamais d'exception).
 * @param {string} id
 * @returns {ProtocolInfo}
 */
export function protocol(id) {
  return (
    PROTOCOLS[id] ??
    Object.freeze({
      id,
      label: String(id ?? '—'),
      short: String(id ?? '—'),
      transport: '',
      icon: 'circle-dashed',
      port: null,
      cssVar: '--neutral',
      color: 'var(--neutral)',
      fg: 'var(--fg-1)',
      soft: 'var(--neutral-soft)',
      line: 'var(--neutral-line)',
    })
  );
}

/**
 * Couleur calculée d'un protocole (`#2FD9C4`), pour les contextes qui n'acceptent pas `var()`.
 * @param {string} id
 * @returns {string}
 */
export function protocolColor(id) {
  return getComputedStyle(document.documentElement).getPropertyValue(protocol(id).cssVar).trim();
}
