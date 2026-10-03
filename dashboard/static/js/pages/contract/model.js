/**
 * Modèle de la page « Contrat & IDL » : tout ce qui se déduit des données du laboratoire
 * (tons, regroupements, annotations du diff, décomptes) sans toucher au DOM.
 */

/** Ton de l'interface selon le niveau de danger d'une issue (0 : inoffensif … 3 : le pire). */
const TONES_BY_DANGER = ['success', 'info', 'warning', 'danger'];

const OUTCOME_UI = {
  compatible: { short: 'Compatible', icon: 'circle-check' },
  rejected: { short: 'Rejet', icon: 'ban' },
  crash: { short: 'Plantage', icon: 'bug' },
  silent_corruption: { short: 'Corruption silencieuse', icon: 'eye-off' },
};

const KINDS = {
  breaking: { label: 'BREAKING', tone: 'danger', noun: 'cassant' },
  compatible: { label: 'COMPATIBLE', tone: 'success', noun: 'compatible' },
};

const SEVERITIES = {
  critical: { label: 'Critique', tone: 'danger' },
  major: { label: 'Majeure', tone: 'warning' },
  minor: { label: 'Mineure', tone: 'info' },
  info: { label: 'Sans gravité', tone: 'neutral' },
};

/** Les deux familles de contrat du laboratoire : avec IDL (Protobuf) et sans (JSON-RPC maison). */
export const CONTRACTS = Object.freeze({
  protobuf: { label: 'Protobuf', detail: 'contrat écrit : service.proto', protocol: 'grpc' },
  jsonrpc: { label: 'JSON-RPC maison', detail: 'contrat implicite : aucun fichier à comparer', protocol: 'custom' },
});

/**
 * @typedef {Object} Outcome
 * @property {string} id `compatible`, `rejected`, `crash` ou `silent_corruption`.
 * @property {string} label Libellé du laboratoire (« Rejet explicite »).
 * @property {string} short Libellé court du badge (« Rejet »).
 * @property {string} text Ce que l'issue signifie pour le client.
 * @property {number} danger 0 (inoffensif) à 3 (le pire).
 * @property {'success'|'info'|'warning'|'danger'|'neutral'} tone
 * @property {string} icon
 */

/**
 * Catalogue des issues possibles, trié de la moins à la plus dangereuse.
 * @param {Record<string, {label: string, danger: number, text: string}>} outcomes `outcomes` de `GET /api/contract`.
 * @returns {{list: Outcome[], get: (id: string) => Outcome}}
 */
export function outcomeCatalog(outcomes) {
  const list = Object.entries(outcomes ?? {})
    .map(([id, info]) => {
      const danger = Math.max(0, Number(info.danger) || 0);
      return {
        id,
        label: info.label ?? id,
        short: OUTCOME_UI[id]?.short ?? info.label ?? id,
        text: info.text ?? '',
        danger,
        tone: TONES_BY_DANGER[Math.min(TONES_BY_DANGER.length - 1, danger)],
        icon: OUTCOME_UI[id]?.icon ?? 'circle-dot',
      };
    })
    .sort((a, b) => a.danger - b.danger);
  const byId = new Map(list.map((outcome) => [outcome.id, outcome]));
  return {
    list,
    get: (id) => byId.get(id) ?? { id, label: String(id), short: String(id), text: '', danger: 0, tone: 'neutral', icon: 'circle-dot' },
  };
}

/**
 * Présentation d'une catégorie de changement (`breaking` / `compatible`).
 * @param {string} kind
 * @returns {{label: string, tone: string, noun: string}}
 */
export function kindInfo(kind) {
  return KINDS[kind] ?? { label: String(kind ?? '').toUpperCase(), tone: 'neutral', noun: String(kind ?? '') };
}

/**
 * Présentation d'une gravité (`critical`, `major`, `minor`, `info`).
 * @param {string} severity
 * @returns {{label: string, tone: string}}
 */
export function severityInfo(severity) {
  return SEVERITIES[severity] ?? { label: String(severity ?? ''), tone: 'neutral' };
}

/**
 * Vrai si le changement désigne au moins une ligne d'un des deux fichiers `.proto`.
 * @param {{lines?: {v1?: number[], v2?: number[]}}} change
 * @returns {boolean}
 */
export function hasLines(change) {
  return (change.lines?.v1?.length ?? 0) + (change.lines?.v2?.length ?? 0) > 0;
}

/**
 * Annotations du `DiffView` : un badge BREAKING / COMPATIBLE par ligne touchée (côté v2 quand la
 * ligne existe encore, côté v1 sinon). Le changement sélectionné reçoit en plus son intitulé,
 * affiché sous sa première ligne.
 * @param {Array<Object>} changes `changes` de `GET /api/contract`.
 * @param {string|null} selectedId
 * @returns {Array<{line: number, side: 'left'|'right', tone: string, label: string, text?: string}>}
 */
export function diffAnnotations(changes, selectedId) {
  const seen = new Set();
  const notes = [];
  const ordered = [...changes].sort((a, b) => Number(b.id === selectedId) - Number(a.id === selectedId));
  for (const change of ordered) {
    const kind = kindInfo(change.kind);
    const right = change.lines?.v2 ?? [];
    const targets = right.length ? right.map((line) => ['right', line]) : (change.lines?.v1 ?? []).map((line) => ['left', line]);
    targets.forEach(([side, line], index) => {
      const key = `${side}:${line}:${kind.label}`;
      if (seen.has(key)) return;
      seen.add(key);
      notes.push({ side, line, tone: kind.tone, label: kind.label, text: change.id === selectedId && index === 0 ? change.title : undefined });
    });
  }
  return notes;
}

const pairKey = (scenario) => JSON.stringify([scenario.change_id, scenario.method, scenario.params]);

/**
 * Regroupe les scénarios par middleware. Deux scénarios qui rejouent le même appel et ne
 * diffèrent que par la validation stricte du serveur partagent une carte (`variants`).
 * @param {Array<Object>} scenarios `scenarios` de `GET /api/contract`.
 * @returns {Array<{protocol: string, cards: Array<{key: string, variants: Object[]}>}>}
 */
export function scenarioGroups(scenarios) {
  const groups = new Map();
  for (const scenario of scenarios ?? []) {
    if (!groups.has(scenario.protocol)) groups.set(scenario.protocol, { protocol: scenario.protocol, cards: [] });
    const group = groups.get(scenario.protocol);
    const key = pairKey(scenario);
    const twin = group.cards.find((card) => card.key === key && card.variants.every((variant) => Boolean(variant.strict) !== Boolean(scenario.strict)));
    if (twin) {
      twin.variants.push(scenario);
      twin.variants.sort((a, b) => Number(Boolean(a.strict)) - Number(Boolean(b.strict)));
    } else {
      group.cards.push({ key, variants: [scenario] });
    }
  }
  return [...groups.values()];
}

/**
 * Décompte des issues observées : combien de ruptures se signalent par une erreur, combien
 * passent inaperçues.
 * @param {Array<Object>} scenarios
 * @param {Map<string, Object>} results Dernier résultat de chaque scénario joué.
 * @param {{list: Outcome[]}} outcomes
 * @returns {{total: number, played: Object[], counts: Map<string, number>, ruptures: number, detected: number,
 *   rejected: number, crashed: number, silent: Object[], compatible: number, unexpected: number}}
 */
export function summarize(scenarios, results, outcomes) {
  const played = (scenarios ?? []).filter((scenario) => results.has(scenario.id)).map((scenario) => results.get(scenario.id));
  const counts = new Map(outcomes.list.map((outcome) => [outcome.id, 0]));
  for (const result of played) counts.set(result.outcome, (counts.get(result.outcome) ?? 0) + 1);
  const of = (id) => counts.get(id) ?? 0;
  return {
    total: (scenarios ?? []).length,
    played,
    counts,
    ruptures: played.length - of('compatible'),
    detected: of('rejected') + of('crash'),
    rejected: of('rejected'),
    crashed: of('crash'),
    silent: played.filter((result) => result.outcome === 'silent_corruption'),
    compatible: of('compatible'),
    unexpected: played.filter((result) => result.matches === false).length,
  };
}

/**
 * Aplatit une valeur JSON en paires `[chemin, valeur]` (`products[0].price`, `SKU-1001.stock`).
 * @param {*} value
 * @param {string} [prefix]
 * @returns {Array<[string, *]>}
 */
export function flatten(value, prefix = '') {
  if (Array.isArray(value)) return value.flatMap((item, index) => flatten(item, `${prefix}[${index}]`));
  if (value !== null && typeof value === 'object') {
    return Object.entries(value).flatMap(([key, item]) => flatten(item, prefix ? `${prefix}.${key}` : key));
  }
  return [[prefix, value]];
}

/**
 * Écriture d'une valeur telle que le code la manipule (`"SKU-1001"`, `-3`, `true`, `null`).
 * @param {*} value
 * @returns {string}
 */
export function literal(value) {
  if (value === undefined) return '—';
  return typeof value === 'string' ? JSON.stringify(value) : String(value);
}

const VARINT_TYPES = new Set(['int32', 'int64', 'uint32', 'uint64', 'sint32', 'sint64', 'bool', 'enum']);
const I64_TYPES = new Set(['double', 'fixed64', 'sfixed64']);
const I32_TYPES = new Set(['float', 'fixed32', 'sfixed32']);

function wireTypeOf(type) {
  if (VARINT_TYPES.has(type)) return { wire: 0, wireName: 'VARINT' };
  if (I64_TYPES.has(type)) return { wire: 1, wireName: 'I64' };
  if (I32_TYPES.has(type)) return { wire: 5, wireName: 'I32' };
  return { wire: 2, wireName: 'LEN' };
}

function varintHex(value) {
  const bytes = [];
  let rest = value;
  do {
    const low = rest & 0x7f;
    rest >>>= 7;
    bytes.push((rest ? low | 0x80 : low).toString(16).padStart(2, '0'));
  } while (rest);
  return bytes.join(' ');
}

/**
 * Lit une déclaration de champ Protobuf (`sint32 delta = 2;`) et calcule le tag qu'elle produit
 * sur le fil : `(numéro << 3) | type de fil`.
 * @param {string|null} declaration
 * @returns {{type: string, name: string, number: number, wire: number, wireName: string, tagHex: string}|null}
 */
export function parseField(declaration) {
  const match = /^\s*(?:repeated\s+|optional\s+)?([\w.]+)\s+(\w+)\s*=\s*(\d+)\s*;/.exec(declaration ?? '');
  if (!match) return null;
  const number = Number(match[3]);
  const { wire, wireName } = wireTypeOf(match[1]);
  return { type: match[1], name: match[2], number, wire, wireName, tagHex: varintHex((number << 3) | wire) };
}

/**
 * Choisit dans la liste des changements un champ dont le numéro est resté et dont le reste a
 * changé : l'exemple de la carte « ce qui voyage sur le fil ». Préfère un changement de type de fil.
 * @param {Array<Object>} changes
 * @returns {{change: Object, v1: Object, v2: Object}|null}
 */
export function wireExample(changes) {
  const candidates = (changes ?? [])
    .map((change) => ({ change, v1: parseField(change.v1), v2: parseField(change.v2) }))
    .filter((item) => item.v1 && item.v2 && item.v1.number === item.v2.number);
  return candidates.find((item) => item.v1.wire !== item.v2.wire) ?? candidates[0] ?? null;
}

/**
 * Compte les RPC et les messages déclarés dans une source `.proto`.
 * @param {string} source
 * @returns {{rpcs: number, messages: number}}
 */
export function protoStats(source) {
  const text = String(source ?? '');
  return { rpcs: (text.match(/^\s*rpc\s+\w+/gm) ?? []).length, messages: (text.match(/^\s*message\s+\w+/gm) ?? []).length };
}

/**
 * Accord en nombre : `plural(2, 'rejet')` → « 2 rejets », `plural(1, 'plantage')` → « 1 plantage ».
 * @param {number} count
 * @param {string} singular
 * @param {string} [many] Pluriel irrégulier.
 * @returns {string}
 */
export function plural(count, singular, many) {
  return `${count} ${count > 1 ? (many ?? `${singular}s`) : singular}`;
}
