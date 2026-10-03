/**
 * Transparence — lecture des données de `GET /api/code-compare` : ligne d'intention de chaque
 * écriture, matrice des préoccupations, phrases calculées. Aucune valeur n'est inventée ici :
 * tout est dérivé des extraits reçus.
 */

import { fmtMs, fmtNumber, fmtUs } from '../../core/format.js';

/**
 * Place de chaque écriture vis-à-vis du middleware (explication, pas mesure) :
 * `none` = aucun réseau, `stub` = un mandataire s'occupe du protocole, `hand` = rien ne s'en occupe.
 * @type {Readonly<Record<string, {kind: 'none'|'stub'|'hand', label: string, relief: string}>>}
 */
export const ROLES = Object.freeze({
  local: { kind: 'none', label: 'Même processus, aucun réseau', relief: 'Rien à gérer : aucun réseau n’est traversé.' },
  custom: { kind: 'stub', label: 'Stub écrit pour le laboratoire', relief: 'Rien : le stub sérialise, envoie, attend et traduit les erreurs.' },
  grpc: { kind: 'stub', label: 'Stub généré par protoc', relief: 'Rien : le stub généré s’occupe de tout.' },
  rest: { kind: 'hand', label: 'Aucun stub : HTTP et JSON à la main', relief: 'Rien à gérer.' },
});

/**
 * Titre court d'une écriture : ce qui précède le tiret (« RPC maison — stub JSON-RPC » → « RPC maison »).
 * @param {{title?: string, id?: string}} snippet
 * @returns {string}
 */
export function shortTitle(snippet) {
  return String(snippet.title ?? snippet.id ?? '').split(' — ')[0];
}

/**
 * Énumération française : « a », « a et b », « a, b et c ».
 * @param {string[]} items
 * @returns {string}
 */
export function listFr(items) {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} et ${items[items.length - 1]}`;
}

/**
 * Accord en nombre : `plural(2, 'ligne')` → « 2 lignes ».
 * @param {number} count
 * @param {string} singular
 * @param {string} [many] Pluriel irrégulier (par défaut : singulier + « s »).
 * @returns {string}
 */
export function plural(count, singular, many = `${singular}s`) {
  return `${fmtNumber(count)} ${count > 1 ? many : singular}`;
}

/**
 * Durée d'un appel donnée en millisecondes, dans l'unité qui la rend lisible (« 34 µs », « 201 ms »).
 * @param {number|null|undefined} ms
 * @returns {string}
 */
export function fmtCall(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(Number(ms))) return '—';
  return Number(ms) < 1 ? fmtUs(Number(ms) * 1000) : fmtMs(Number(ms));
}

/**
 * Facteur multiplicatif : « × 6,3 », « × 243 », « × 6 200 ».
 * @param {number} ratio
 * @returns {string}
 */
export function fmtTimes(ratio) {
  if (!Number.isFinite(ratio)) return '—';
  return `× ${fmtNumber(ratio, { maxDecimals: ratio >= 10 ? 0 : 1 })}`;
}

/**
 * Facteur en toutes lettres : « 6,3 fois », « 4 472 fois ».
 * @param {number} ratio
 * @returns {string}
 */
export function fmtFold(ratio) {
  if (!Number.isFinite(ratio)) return '—';
  return `${fmtNumber(ratio, { maxDecimals: ratio >= 10 ? 0 : 1 })} fois`;
}

/**
 * Lignes qui portent l'intention métier d'un extrait : l'appel de la procédure (`.update_stock(`,
 * `.UpdateStock(`) ou, sans stub, l'envoi de la requête (`.request(`, `fetch(`). Si l'appel s'étend
 * sur plusieurs lignes, l'intervalle va jusqu'à la parenthèse fermante.
 * @param {string} code Source de l'extrait.
 * @param {string} method Nom de la procédure (`update_stock`).
 * @returns {[number, number]|null} Intervalle `[première, dernière]`, lignes comptées à partir de 1.
 */
export function intentRange(code, method) {
  const lines = String(code ?? '').split('\n');
  const call = new RegExp(`\\.${String(method).split('_').join('_?')}\\(`, 'i');
  let start = lines.findIndex((line) => call.test(line));
  if (start < 0) start = lines.findIndex((line) => /\bfetch\(|\.request\(/.test(line));
  if (start < 0) return null;
  let depth = 0;
  for (let index = start; index < lines.length; index += 1) {
    for (const char of lines[index]) {
      if (char === '(') depth += 1;
      else if (char === ')') depth -= 1;
    }
    if (depth <= 0) return [start + 1, index + 1];
  }
  return [start + 1, start + 1];
}

/**
 * Texte de la ligne d'intention, sans indentation.
 * @param {string} code
 * @param {string} method
 * @returns {string}
 */
export function intentText(code, method) {
  const range = intentRange(code, method);
  return range ? String(code).split('\n')[range[0] - 1].trim() : '';
}

/**
 * Ce qui distingue deux lignes de code, mot par mot : les mots restants une fois retirés le
 * préfixe et le suffixe communs (`service.update_stock(…)` / `stub.update_stock(…)` → `service` / `stub`).
 * @param {string} left
 * @param {string} right
 * @returns {{left: string, right: string}} Chaînes vides si les lignes sont identiques.
 */
export function splitDifference(left, right) {
  const words = (text) => String(text).match(/\w+|\W/g) ?? [];
  const a = words(left);
  const b = words(right);
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail += 1;
  return { left: a.slice(head, a.length - tail).join(''), right: b.slice(head, b.length - tail).join('') };
}

/**
 * @typedef {Object} MatrixRow
 * @property {string} concern Préoccupation, telle que la nomme le laboratoire.
 * @property {Array<{id: string, state: 'hand'|'middleware'|'na'}>} cells Une cellule par écriture :
 *   `hand` = écrit par l'appelant, `middleware` = pris en charge par le stub, `na` = sans objet.
 */

/**
 * Matrice « préoccupation × écriture ». Les lignes sont l'union des préoccupations déclarées,
 * en commençant par l'écriture qui en porte le plus.
 * @param {Array<{id: string, concerns: string[]}>} snippets
 * @returns {MatrixRow[]}
 */
export function concernMatrix(snippets) {
  const byLoad = [...snippets].sort((a, b) => b.concerns.length - a.concerns.length);
  const concerns = [];
  for (const snippet of byLoad) {
    for (const concern of snippet.concerns) if (!concerns.includes(concern)) concerns.push(concern);
  }
  return concerns.map((concern) => ({
    concern,
    cells: snippets.map((snippet) => ({
      id: snippet.id,
      state: snippet.concerns.includes(concern) ? 'hand' : ROLES[snippet.id]?.kind === 'stub' ? 'middleware' : 'na',
    })),
  }));
}

const named = (snippets) => listFr(snippets.map((snippet) => `« ${shortTitle(snippet)} »`));

/**
 * Phrase calculée sur les lignes de code : l'écriture la plus longue rapportée à la plus courte.
 * @param {Array<{title: string, lines: number}>} snippets
 * @returns {string}
 */
export function linesSentence(snippets) {
  if (!snippets.length) return '';
  const min = Math.min(...snippets.map((snippet) => snippet.lines));
  const max = Math.max(...snippets.map((snippet) => snippet.lines));
  if (min === max) return `Les ${snippets.length} écritures comptent le même nombre de lignes utiles (${min}).`;
  const shortest = snippets.filter((snippet) => snippet.lines === min);
  const longest = snippets.filter((snippet) => snippet.lines === max);
  const verb = longest.length > 1 ? 'comptent' : 'compte';
  const ratio = min > 0 ? `, soit ${fmtFold(max / min)} plus que ` : ', contre ';
  return `${named(longest)} ${verb} ${plural(max, 'ligne utile', 'lignes utiles')}${ratio}${named(shortest)} (${plural(min, 'ligne')}${shortest.length > 1 ? ' chacune' : ''}).`;
}

/**
 * Phrase calculée sur les préoccupations : combien l'appelant en écrit, écriture par écriture.
 * @param {Array<{title: string, concerns: string[]}>} snippets
 * @param {number} total Nombre de préoccupations distinctes (lignes de la matrice).
 * @returns {string}
 */
export function concernsSentence(snippets, total) {
  if (!snippets.length) return '';
  if (!total) return 'Aucune écriture ne laisse de préoccupation de protocole à l’appelant.';
  const counts = [...new Set(snippets.map((snippet) => snippet.concerns.length))].sort((a, b) => b - a);
  const parts = counts.map((count) => {
    const group = snippets.filter((snippet) => snippet.concerns.length === count);
    return `${count === 0 ? 'aucune' : fmtNumber(count)} avec ${named(group)}`;
  });
  return `Sur les ${plural(total, 'préoccupation distincte', 'préoccupations distinctes')} recensées, l’appelant en écrit ${listFr(parts)}.`;
}

/**
 * Résumé de la variante navigateur : ses préoccupations rapportées à celles de l'écriture sans stub.
 * @param {{lines: number, concerns: string[]}} script Bloc `javascript_fetch`.
 * @param {{concerns: string[]}|undefined} reference Écriture Python sans stub.
 * @returns {string}
 */
export function fetchSummary(script, reference) {
  const base = `${plural(script.concerns.length, 'préoccupation')} à la charge de la page`;
  if (!reference) return base;
  const shared = script.concerns.filter((concern) => reference.concerns.includes(concern)).length;
  return shared ? `${base}, dont ${fmtNumber(shared)} mot pour mot celles de l’écriture Python` : base;
}
