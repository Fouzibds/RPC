/**
 * Code et comparaison de code : `CodeBlock` (coloration syntaxique, numéros de ligne, lignes
 * mises en avant, copie) et `DiffView` (diff ligne à ligne côte à côte ou unifié, mots modifiés
 * surlignés, annotations BREAKING / COMPATIBLE).
 */

import { h, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { alignRows, diffLines } from './code/diff.js';
import { highlight, languageName } from './code/highlight.js';
import { Badge, CopyButton, Segmented } from './ui.js';

const LANGUAGE_LABELS = { json: 'JSON', python: 'Python', protobuf: 'Protobuf', javascript: 'JavaScript', http: 'HTTP', shell: 'Shell' };

/**
 * Nœuds DOM d'une ligne de jetons. `ranges` (plages `[début, fin[` en caractères) reçoit la
 * classe `diff__word` : les mots modifiés d'une ligne de diff.
 */
function tokenNodes(tokens, ranges = []) {
  const nodes = [];
  let offset = 0;
  let cursor = 0;
  for (const token of tokens) {
    const end = offset + token.text.length;
    let start = offset;
    while (start < end) {
      while (cursor < ranges.length && ranges[cursor][1] <= start) cursor += 1;
      const range = ranges[cursor];
      const marked = Boolean(range) && range[0] <= start;
      const stop = marked ? Math.min(end, range[1]) : Math.min(end, range ? range[0] : end);
      const text = token.text.slice(start - offset, stop - offset);
      if (token.type === 'plain' && !marked) nodes.push(document.createTextNode(text));
      else nodes.push(h('span', { class: [token.type === 'plain' ? null : `tok tok--${token.type}`, marked ? 'diff__word' : null], textContent: text }));
      start = stop;
    }
    offset = end;
  }
  return nodes;
}

function lineSet(spec) {
  const set = new Set();
  for (const item of spec ?? []) {
    if (Array.isArray(item)) for (let line = item[0]; line <= item[1]; line += 1) set.add(line);
    else set.add(item);
  }
  return set;
}

const cssSize = (value) => (value === undefined || value === null ? '' : typeof value === 'number' ? `${value}px` : value);

const TONES = new Set(['accent', 'success', 'warning', 'danger', 'info', 'neutral']);

/** Largeur, en caractères, de l'indentation d'une ligne (une tabulation en vaut deux, comme `tab-size`). */
function indentOf(line) {
  let columns = 0;
  for (const char of line) {
    if (char === ' ') columns += 1;
    else if (char === '\t') columns += 2;
    else break;
  }
  return columns;
}

/**
 * Bloc de code coloré.
 *
 *     CodeBlock({ title: 'service.proto', language: 'protobuf', code, highlightLines: [[12, 14]] })
 *
 * @param {Object} [props]
 * @param {string} [props.code]
 * @param {string} [props.language='text'] `json`, `python`, `protobuf`, `javascript`, `http`, `shell`
 *   (alias `js`, `py`, `proto`, `bash`, `sh`…) ; tout autre nom = texte brut.
 * @param {string} [props.title] Titre de la barre supérieure (nom de fichier, description). Sans titre,
 *   pas de barre : le bouton de copie flotte en haut à droite.
 * @param {boolean} [props.lineNumbers=true]
 * @param {Array<number|[number, number]>} [props.highlightLines] Lignes (ou intervalles) mises en avant, à partir de 1.
 * @param {'accent'|'success'|'warning'|'danger'|'info'|'neutral'|string} [props.highlightTone='accent'] Couleur des lignes
 *   mises en avant : un ton, ou une couleur CSS (ex. `var(--proto-grpc)`).
 * @param {number} [props.startLine=1] Numéro de la première ligne.
 * @param {boolean} [props.wrap=false] Retour à la ligne automatique au lieu du défilement horizontal. La suite
 *   d'une ligne repliée reprend sous son indentation, décalée de `hangingIndent`.
 * @param {number} [props.hangingIndent=2] Retrait suspendu des lignes repliées, en caractères (avec `wrap`).
 * @param {number|string} [props.maxHeight] Hauteur maximale (défilement vertical au-delà).
 * @param {boolean} [props.copy=true] Bouton de copie.
 * @param {Node|Node[]} [props.actions] Contrôles supplémentaires dans la barre (requiert `title`).
 * @returns {HTMLElement & {update: (patch: Object) => void, destroy: () => void}}
 */
export function CodeBlock(props = {}) {
  const state = { code: '', language: 'text', lineNumbers: true, highlightLines: [], startLine: 1, wrap: false, hangingIndent: 2, copy: true, ...props };
  const bar = h('div.code__bar');
  const lines = h('code.code__lines');
  const scroller = h('div.code__scroll.scroll-x.scroll-y', h('pre.code__pre', lines));
  const floating = h('div.code__float');
  const el = h('div.code', bar, scroller, floating);

  function render() {
    const language = languageName(state.language);
    const source = String(state.code ?? '').replace(/\r\n?/g, '\n').replace(/\n$/, '');
    const marked = lineSet(state.highlightLines);
    const rows = highlight(source, language);
    const indents = state.wrap ? source.split('\n').map(indentOf) : [];
    const digits = String(state.startLine + rows.length - 1).length;
    const copy = state.copy ? CopyButton({ text: () => source, label: 'Copier le code' }) : null;
    const label = LANGUAGE_LABELS[language];

    bar.hidden = !state.title;
    clear(
      bar,
      state.title
        ? [h('span.code__title.truncate', state.title), h('div.code__tools', label ? h('span.code__language', label) : null, state.actions ?? null, copy)]
        : null,
    );
    clear(floating, state.title ? null : copy);
    el.dataset.wrap = String(Boolean(state.wrap));
    el.dataset.numbers = String(Boolean(state.lineNumbers));
    el.dataset.language = language;
    el.style.setProperty('--ln-width', `${digits}ch`);
    el.style.setProperty('--code-hang', `${Number(state.hangingIndent) || 0}ch`);
    const tone = state.highlightTone;
    if (!tone || tone === 'accent') {
      el.style.removeProperty('--code-hl');
      el.style.removeProperty('--code-hl-soft');
    } else {
      el.style.setProperty('--code-hl', TONES.has(tone) ? `var(--${tone})` : tone);
      el.style.setProperty('--code-hl-soft', TONES.has(tone) ? `var(--${tone}-soft)` : `color-mix(in srgb, ${tone} 14%, transparent)`);
    }
    scroller.style.maxHeight = cssSize(state.maxHeight);
    clear(
      lines,
      rows.map((tokens, index) => {
        const number = state.startLine + index;
        return h(
          'span.code__line',
          { class: { 'is-highlighted': marked.has(number) }, dataset: { line: number } },
          state.lineNumbers ? h('span.code__ln', { 'aria-hidden': 'true', textContent: String(number) }) : null,
          h('span.code__text', { style: indents[index] ? { '--indent': indents[index] } : null }, tokenNodes(tokens), tokens.length ? null : document.createTextNode(' ')),
        );
      }),
    );
  }

  el.update = (patch) => {
    Object.assign(state, patch);
    render();
  };
  el.destroy = () => {};
  render();
  return el;
}

/**
 * @typedef {Object} DiffAnnotation
 * @property {number} line Numéro de ligne (à partir de 1) dans le texte du côté concerné.
 * @property {'left'|'right'} [side='right']
 * @property {'danger'|'success'|'warning'|'info'|'accent'|'neutral'} [tone='danger']
 * @property {string} label Texte du badge (« BREAKING », « COMPATIBLE »).
 * @property {string} [text] Explication affichée sous la ligne.
 */

const FOLD_MIN = 4;

/**
 * Comparaison de deux textes : diff ligne à ligne (plus longue sous-séquence commune), lignes
 * ajoutées / supprimées / modifiées teintées, mots modifiés surlignés, annotations par ligne.
 *
 *     DiffView({ left: protoV1, right: protoV2, language: 'protobuf', leftTitle: 'service.proto · v1',
 *       rightTitle: 'service_v2.proto · v2', context: 2,
 *       annotations: [{ line: 81, side: 'right', tone: 'danger', label: 'BREAKING', text: 'N° 2 réutilisé avec un autre type.' }] })
 *
 * @param {Object} [props]
 * @param {string} [props.left] Ancienne version.
 * @param {string} [props.right] Nouvelle version.
 * @param {string} [props.language='text'] Voir `CodeBlock`.
 * @param {string} [props.leftTitle='Avant']
 * @param {string} [props.rightTitle='Après']
 * @param {'split'|'unified'} [props.mode='split'] Côte à côte ou unifié. Sous 720 px de large, l'affichage
 *   passe de lui-même en unifié.
 * @param {DiffAnnotation[]} [props.annotations]
 * @param {number|null} [props.context=null] Replie les zones identiques en ne gardant que ce nombre de lignes
 *   autour de chaque changement (`null` : tout afficher). Un clic déplie une zone.
 * @param {boolean} [props.wrap=true] Retour à la ligne automatique.
 * @param {number|string} [props.maxHeight]
 * @param {boolean} [props.toggle=true] Affiche le sélecteur « côte à côte / unifié ».
 * @param {Array<Object>} [props.rows] Diff déjà calculé (par le serveur) : remplace le calcul local, de sorte que
 *   lignes et compteurs soient ceux de la source. Formats acceptés : voir `alignRows` dans `code/diff.js`.
 * @param {(mode: 'split'|'unified') => void} [props.onModeChange]
 * @param {(el: HTMLElement) => void} [props.onRender] Appelé après chaque rendu des lignes : premier affichage,
 *   `update`, changement de mode (y compris le passage automatique en unifié), dépliage d'une zone.
 * @returns {HTMLElement & {update: (patch: Object) => void, setMode: (mode: 'split'|'unified') => void, highlightLine: (line: number|number[]|null, side?: 'left'|'right') => void, scrollToLine: (line: number, side?: 'left'|'right', options?: {behavior?: ScrollBehavior}) => HTMLElement|null, destroy: () => void}}
 *   `highlightLine(lignes, côté)` met en avant une ou plusieurs lignes d'un côté (`'right'` par défaut) et
 *   remplace la mise en avant précédente de ce côté ; `highlightLine(null)` efface les deux côtés. Les lignes
 *   mises en avant ne sont jamais repliées et le restent après un nouveau rendu.
 *   `scrollToLine(ligne, côté)` déplie la zone au besoin, amène la ligne dans la vue et renvoie sa rangée
 *   (`null` si la ligne n'existe pas). Chaque rangée porte `data-left` / `data-right` (numéros de ligne).
 */
export function DiffView(props = {}) {
  const state = { left: '', right: '', language: 'text', leftTitle: 'Avant', rightTitle: 'Après', mode: 'split', annotations: [], context: null, wrap: true, toggle: true, ...props };
  const bar = h('div.diff__bar');
  const body = h('tbody');
  const colgroup = h('colgroup');
  const table = h('table.diff__table', colgroup, body);
  const scroller = h('div.diff__scroll.scroll-x.scroll-y', table);
  const el = h('div.diff', bar, scroller);
  const expanded = new Set();
  /** Lignes mises en avant (`highlightLine`) et lignes à garder dépliées (`scrollToLine`), par côté. */
  const marks = { left: new Set(), right: new Set() };
  const pinned = { left: new Set(), right: new Set() };
  let narrow = false;

  const split = (text) => String(text ?? '').replace(/\r\n?/g, '\n').replace(/\n$/, '').split('\n');

  function notes(side, line) {
    return (state.annotations ?? []).filter((note) => (note.side ?? 'right') === side && note.line === line);
  }

  function codeCell(kind, tokens, ranges, side, line) {
    const badges = line === undefined ? [] : notes(side, line);
    return h(
      'td.diff__code',
      { class: `diff__code--${kind}` },
      badges.length ? h('span.diff__badges', badges.map((note) => Badge({ label: note.label, tone: note.tone ?? 'danger', size: 'sm', variant: 'solid' }))) : null,
      tokens ? tokenNodes(tokens, ranges) : null,
    );
  }

  const numberCell = (kind, line) => h('td.diff__ln', { class: `diff__ln--${kind}`, 'aria-hidden': 'true', textContent: line === undefined ? '' : String(line) });

  function noteRows(row, mode) {
    const items = [...(row.left !== undefined ? notes('left', row.left + 1) : []), ...(row.right !== undefined ? notes('right', row.right + 1) : [])].filter((note) => note.text);
    return items.map((note) => {
      const content = h('div.diff__note', { dataset: { tone: note.tone ?? 'danger' } }, h('span.diff__note-label', note.label), h('span.diff__note-text', note.text));
      if (mode === 'unified') return h('tr.diff__note-row', h('td', { colSpan: 3 }), h('td.diff__note-cell', content));
      const onLeft = (note.side ?? 'right') === 'left';
      return h(
        'tr.diff__note-row',
        h('td'),
        onLeft ? h('td.diff__note-cell', content) : h('td.diff__code.diff__code--void'),
        h('td'),
        onLeft ? h('td.diff__code.diff__code--void') : h('td.diff__note-cell', content),
      );
    });
  }

  /** Valeur de `data-mark` d'une rangée : côtés dont la ligne est mise en avant (`left`, `right`, `left right`). */
  function markOf(left, right) {
    const sides = [left !== undefined && marks.left.has(left + 1) ? 'left' : null, right !== undefined && marks.right.has(right + 1) ? 'right' : null].filter(Boolean);
    return sides.length ? sides.join(' ') : null;
  }

  function splitRow(row, leftTokens, rightTokens) {
    const leftKind = row.type === 'equal' ? 'equal' : row.left === undefined ? 'void' : 'remove';
    const rightKind = row.type === 'equal' ? 'equal' : row.right === undefined ? 'void' : 'add';
    return h(
      'tr.diff__row',
      { dataset: { type: row.type, left: row.left === undefined ? null : row.left + 1, right: row.right === undefined ? null : row.right + 1, mark: markOf(row.left, row.right) } },
      numberCell(leftKind, row.left === undefined ? undefined : row.left + 1),
      codeCell(leftKind, row.left === undefined ? null : leftTokens[row.left], row.leftRanges, 'left', row.left === undefined ? undefined : row.left + 1),
      numberCell(rightKind, row.right === undefined ? undefined : row.right + 1),
      codeCell(rightKind, row.right === undefined ? null : rightTokens[row.right], row.rightRanges, 'right', row.right === undefined ? undefined : row.right + 1),
    );
  }

  function unifiedRows(row, leftTokens, rightTokens) {
    const line = (kind, sign, left, right, tokens, ranges, side) =>
      h(
        'tr.diff__row',
        { dataset: { type: kind, left, right, mark: markOf(left === undefined ? undefined : left - 1, right === undefined ? undefined : right - 1) } },
        numberCell(kind, left),
        numberCell(kind, right),
        h('td.diff__sign', { class: `diff__code--${kind}`, 'aria-hidden': 'true', textContent: sign }),
        codeCell(kind, tokens, ranges, side, side === 'left' ? left : right),
      );
    if (row.type === 'equal') return [line('equal', '', row.left + 1, row.right + 1, rightTokens[row.right], undefined, 'right')];
    const out = [];
    if (row.left !== undefined) out.push(line('remove', '−', row.left + 1, undefined, leftTokens[row.left], row.leftRanges, 'left'));
    if (row.right !== undefined) out.push(line('add', '+', undefined, row.right + 1, rightTokens[row.right], row.rightRanges, 'right'));
    return out;
  }

  /** Découpe les lignes en blocs visibles et en zones identiques repliées. */
  function fold(rows) {
    if (state.context === null || state.context === undefined) return [{ rows }];
    const keep = new Array(rows.length).fill(false);
    const anchored = (side, line) => line !== undefined && (notes(side, line + 1).length > 0 || marks[side].has(line + 1) || pinned[side].has(line + 1));
    rows.forEach((row, index) => {
      if (row.type === 'equal' && !anchored('left', row.left) && !anchored('right', row.right)) return;
      for (let k = Math.max(0, index - state.context); k <= Math.min(rows.length - 1, index + state.context); k += 1) keep[k] = true;
    });
    const blocks = [];
    let index = 0;
    while (index < rows.length) {
      let end = index;
      while (end < rows.length && keep[end] === keep[index]) end += 1;
      const hidden = !keep[index] && end - index >= FOLD_MIN && !expanded.has(index);
      blocks.push(hidden ? { folded: end - index, key: index } : { rows: rows.slice(index, end) });
      index = end;
    }
    return blocks;
  }

  function render() {
    const mode = narrow ? 'unified' : state.mode;
    const leftLines = split(state.left);
    const rightLines = split(state.right);
    const leftTokens = highlight(leftLines.join('\n'), state.language);
    const rightTokens = highlight(rightLines.join('\n'), state.language);
    const { rows, added, removed } = state.rows ? alignRows(state.rows, leftLines, rightLines) : diffLines(leftLines, rightLines);
    const digits = String(Math.max(leftLines.length, rightLines.length)).length;

    el.dataset.mode = mode;
    el.dataset.wrap = String(Boolean(state.wrap));
    el.style.setProperty('--ln-width', `${digits}ch`);
    scroller.style.maxHeight = cssSize(state.maxHeight);
    clear(
      colgroup,
      mode === 'split'
        ? [h('col.diff__col-ln'), h('col'), h('col.diff__col-ln'), h('col')]
        : [h('col.diff__col-ln'), h('col.diff__col-ln'), h('col.diff__col-sign'), h('col')],
    );

    const counts = h(
      'span.diff__counts.num',
      h('span.diff__count.diff__count--add', { textContent: `+${added}` }),
      h('span.diff__count.diff__count--remove', { textContent: `−${removed}` }),
    );
    const switcher =
      state.toggle && !narrow
        ? Segmented({
            size: 'sm',
            value: state.mode,
            ariaLabel: 'Présentation du diff',
            options: [
              { value: 'split', icon: 'columns-2', title: 'Côte à côte' },
              { value: 'unified', icon: 'rows-3', title: 'Unifié' },
            ],
            onChange: (value) => el.setMode(value),
          })
        : null;
    clear(
      bar,
      mode === 'split'
        ? [h('span.diff__title.truncate', state.leftTitle), h('span.diff__title.diff__title--right.truncate', state.rightTitle)]
        : h('span.diff__title.truncate', state.leftTitle, h('span.diff__arrow', { 'aria-hidden': 'true' }, '→'), state.rightTitle),
      h('div.diff__tools', counts, switcher),
    );

    const out = [];
    for (const block of fold(rows)) {
      if (block.folded) {
        const button = h(
          'button.diff__fold',
          {
            type: 'button',
            onClick: () => {
              expanded.add(block.key);
              render();
            },
          },
          icon('chevrons-up-down', { size: 12 }),
          `${block.folded} lignes identiques`,
        );
        out.push(h('tr.diff__fold-row', h('td', { colSpan: 4 }, button)));
        continue;
      }
      for (const row of block.rows) {
        if (mode === 'split') out.push(splitRow(row, leftTokens, rightTokens));
        else out.push(...unifiedRows(row, leftTokens, rightTokens));
        out.push(...noteRows(row, mode));
      }
    }
    clear(body, added || removed ? null : h('tr', h('td.diff__empty', { colSpan: 4 }, 'Les deux versions sont identiques.')), out);
    state.onRender?.(el);
  }

  const rowOf = (line, side) => body.querySelector(`tr.diff__row[data-${side}="${Number(line)}"]`);

  const observer = new ResizeObserver((entries) => {
    const next = entries[entries.length - 1].contentRect.width < 720;
    if (next !== narrow) {
      narrow = next;
      render();
    }
  });
  observer.observe(el);

  el.update = (patch) => {
    if ('left' in patch || 'right' in patch || 'rows' in patch) {
      expanded.clear();
      pinned.left.clear();
      pinned.right.clear();
    }
    Object.assign(state, patch);
    render();
  };
  el.highlightLine = (line, side = 'right') => {
    const key = side === 'left' ? 'left' : 'right';
    if (line === null || line === undefined) {
      marks.left.clear();
      marks.right.clear();
    } else {
      marks[key] = new Set([].concat(line).map(Number));
    }
    render();
  };
  el.scrollToLine = (line, side = 'right', { behavior = 'smooth' } = {}) => {
    const key = side === 'left' ? 'left' : 'right';
    if (!rowOf(line, key)) {
      pinned[key].add(Number(line));
      render();
    }
    const row = rowOf(line, key);
    if (!row) return null;
    if (scroller.scrollHeight > scroller.clientHeight + 1) {
      const top = row.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
      scroller.scrollTo({ top: Math.max(0, top - scroller.clientHeight * 0.32), behavior });
    } else {
      row.scrollIntoView({ block: 'center', behavior });
    }
    return row;
  };
  el.setMode = (mode) => {
    state.mode = mode === 'unified' ? 'unified' : 'split';
    render();
    state.onModeChange?.(state.mode);
  };
  el.destroy = () => observer.disconnect();
  render();
  return el;
}
