/**
 * Comparaison de textes : diff ligne à ligne par plus longue sous-séquence commune (LCS),
 * appariement des lignes modifiées et repérage des mots qui changent à l'intérieur d'une ligne.
 */

/**
 * Plus longue sous-séquence commune de deux listes : indices appariés `[i, j]`, dans l'ordre.
 * @param {string[]} a
 * @param {string[]} b
 * @returns {Array<[number, number]>}
 */
function lcsPairs(a, b) {
  const n = a.length;
  const m = b.length;
  const width = m + 1;
  const table = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i * width + j] = a[i] === b[j] ? table[(i + 1) * width + j + 1] + 1 : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }
  const pairs = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      pairs.push([i, j]);
      i += 1;
      j += 1;
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      i += 1;
    } else {
      j += 1;
    }
  }
  return pairs;
}

/** En dessous de ce taux de ressemblance, deux lignes appariées sont teintées en entier, sans détail par mot. */
const SIMILAR = 0.35;

/** Noms acceptés pour une ligne identique dans des lignes de diff précalculées (`alignRows`). */
const EQUAL_KINDS = new Set(['equal', 'same', 'context', 'unchanged']);

function words(line) {
  return line.match(/[A-Za-z0-9_]+|\s+|[^A-Za-z0-9_\s]/g) ?? [];
}

/**
 * Plages de caractères qui diffèrent entre deux versions d'une même ligne.
 * @param {string} left
 * @param {string} right
 * @returns {{left: Array<[number, number]>, right: Array<[number, number]>, similarity: number}}
 *   Plages `[début, fin[` de chaque côté et taux de ressemblance entre 0 et 1.
 */
export function diffWords(left, right) {
  const a = words(left);
  const b = words(right);
  const pairs = lcsPairs(a, b);
  const keptA = new Set(pairs.map((pair) => pair[0]));
  const keptB = new Set(pairs.map((pair) => pair[1]));
  const ranges = (tokens, kept) => {
    const out = [];
    let offset = 0;
    tokens.forEach((token, index) => {
      const end = offset + token.length;
      if (!kept.has(index)) {
        const last = out[out.length - 1];
        if (last && last[1] === offset) last[1] = end;
        else out.push([offset, end]);
      }
      offset = end;
    });
    return out;
  };
  const common = pairs.reduce((sum, pair) => sum + (a[pair[0]].trim() ? a[pair[0]].length : 0), 0);
  const total = Math.max(left.replace(/\s/g, '').length, right.replace(/\s/g, '').length) || 1;
  return { left: ranges(a, keptA), right: ranges(b, keptB), similarity: common / total };
}

/**
 * @typedef {Object} DiffRow
 * @property {'equal'|'change'|'remove'|'add'} type
 * @property {number} [left] Indice de la ligne à gauche (absent pour `add`).
 * @property {number} [right] Indice de la ligne à droite (absent pour `remove`).
 * @property {Array<[number, number]>} [leftRanges] Mots modifiés à gauche (`change`, lignes assez proches).
 * @property {Array<[number, number]>} [rightRanges] Mots modifiés à droite (`change`, lignes assez proches).
 */

/**
 * Apparie les lignes supprimées et ajoutées d'un même bloc de changements : la k-ième de chaque côté
 * forme une ligne « modifiée » (mots différents repérés si les deux lignes se ressemblent assez), le
 * surplus reste en suppressions ou en ajouts.
 * @param {{rows: DiffRow[], added: number, removed: number}} out Résultat complété sur place.
 * @param {number[]} gone Indices des lignes supprimées (à gauche).
 * @param {number[]} fresh Indices des lignes ajoutées (à droite).
 * @param {string[]} left
 * @param {string[]} right
 */
function pairBlock(out, gone, fresh, left, right) {
  const span = Math.max(gone.length, fresh.length);
  for (let k = 0; k < span; k += 1) {
    const i = gone[k];
    const j = fresh[k];
    if (i !== undefined && j !== undefined) {
      const detail = diffWords(left[i], right[j]);
      const close = detail.similarity >= SIMILAR;
      out.rows.push({ type: 'change', left: i, right: j, leftRanges: close ? detail.left : undefined, rightRanges: close ? detail.right : undefined });
      out.added += 1;
      out.removed += 1;
    } else if (i !== undefined) {
      out.rows.push({ type: 'remove', left: i });
      out.removed += 1;
    } else {
      out.rows.push({ type: 'add', right: j });
      out.added += 1;
    }
  }
}

const indexRange = (from, to) => Array.from({ length: Math.max(0, to - from) }, (_, k) => from + k);

/**
 * Compare deux textes ligne à ligne.
 * @param {string[]} left Lignes de l'ancienne version.
 * @param {string[]} right Lignes de la nouvelle version.
 * @returns {{rows: DiffRow[], added: number, removed: number}} Lignes alignées, dans l'ordre de lecture,
 *   et nombre de lignes ajoutées / supprimées (une ligne modifiée compte dans les deux).
 */
export function diffLines(left, right) {
  let head = 0;
  while (head < left.length && head < right.length && left[head] === right[head]) head += 1;
  let tail = 0;
  while (tail < left.length - head && tail < right.length - head && left[left.length - 1 - tail] === right[right.length - 1 - tail]) tail += 1;
  const a = left.slice(head, left.length - tail);
  const b = right.slice(head, right.length - tail);
  const pairs = a.length * b.length > 4_000_000 ? [] : lcsPairs(a, b);

  /** @type {{rows: DiffRow[], added: number, removed: number}} */
  const out = { rows: [], added: 0, removed: 0 };
  for (let i = 0; i < head; i += 1) out.rows.push({ type: 'equal', left: i, right: i });

  const flush = (fromA, toA, fromB, toB) => pairBlock(out, indexRange(head + fromA, head + toA), indexRange(head + fromB, head + toB), left, right);

  let i = 0;
  let j = 0;
  for (const [pi, pj] of pairs) {
    flush(i, pi, j, pj);
    out.rows.push({ type: 'equal', left: head + pi, right: head + pj });
    i = pi + 1;
    j = pj + 1;
  }
  flush(i, a.length, j, b.length);
  for (let k = 0; k < tail; k += 1) out.rows.push({ type: 'equal', left: left.length - tail + k, right: right.length - tail + k });
  return out;
}

/**
 * Met en forme un diff calculé ailleurs (par le serveur) pour `DiffView` : mêmes lignes, mêmes
 * compteurs que la source, et les suppressions / ajouts contigus appariés côte à côte comme le fait
 * `diffLines`. Deux écritures de ligne sont acceptées :
 *
 * - `{ type: 'equal'|'change'|'remove'|'add', left?, right? }` — indices à partir de 0 ;
 * - `{ kind: 'same'|'removed'|'added', left_no?, right_no? }` — numéros de ligne à partir de 1
 *   (format de `GET /api/contract`).
 *
 * Une ligne dont le type n'est pas « identique » compte comme supprimée si elle porte un indice à
 * gauche, ajoutée si elle en porte un à droite ; les indices hors du texte sont ignorés.
 * @param {Array<Object>} rows Lignes précalculées, dans l'ordre de lecture.
 * @param {string[]} left Lignes de l'ancienne version.
 * @param {string[]} right Lignes de la nouvelle version.
 * @returns {{rows: DiffRow[], added: number, removed: number}}
 */
export function alignRows(rows, left, right) {
  const out = { rows: [], added: 0, removed: 0 };
  const indexOf = (zeroBased, oneBased, lines) => {
    const index = Number.isInteger(zeroBased) ? zeroBased : Number.isInteger(oneBased) ? oneBased - 1 : -1;
    return index >= 0 && index < lines.length ? index : undefined;
  };
  let gone = [];
  let fresh = [];
  const flush = () => {
    pairBlock(out, gone, fresh, left, right);
    gone = [];
    fresh = [];
  };
  for (const row of rows ?? []) {
    const l = indexOf(row.left, row.left_no, left);
    const r = indexOf(row.right, row.right_no, right);
    if (EQUAL_KINDS.has(row.type ?? row.kind) && l !== undefined && r !== undefined) {
      flush();
      out.rows.push({ type: 'equal', left: l, right: r });
    } else {
      if (l !== undefined) gone.push(l);
      if (r !== undefined) fresh.push(r);
    }
  }
  flush();
  return out;
}
