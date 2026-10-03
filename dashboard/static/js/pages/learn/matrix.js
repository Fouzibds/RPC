/**
 * Bilan — « Comparatif » : la matrice qualitative du bilan, critères en lignes, les quatre façons
 * d'appeler en colonnes. Valeurs courtes et pastilles pour les critères, barres à l'échelle pour
 * les lignes mesurées.
 */

import { h, svg } from '../../core/dom.js';
import { PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { Card, EmptyState, Skeleton } from '../../components/ui.js';
import { MARK_LABELS, RATINGS } from './content.js';
import { measuredSeries } from './data.js';

/**
 * Pastille de lecture : disque plein, demi-disque, anneau, point ou tiret.
 * @param {import('./content.js').Mark} mark
 * @returns {SVGElement}
 */
function Glyph(mark) {
  const shapes = {
    full: [svg('circle', { cx: 7, cy: 7, r: 5.25, fill: 'currentColor' })],
    half: [svg('circle', { cx: 7, cy: 7, r: 4.75, stroke: 'currentColor', 'stroke-width': 1.5 }), svg('path', { d: 'M7 2.25a4.75 4.75 0 0 0 0 9.5z', fill: 'currentColor' })],
    empty: [svg('circle', { cx: 7, cy: 7, r: 4.75, stroke: 'currentColor', 'stroke-width': 1.5 })],
    dot: [svg('circle', { cx: 7, cy: 7, r: 1.75, fill: 'currentColor' })],
    na: [svg('path', { d: 'M4 7h6', stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round' })],
  };
  return svg('svg', { class: `learn-glyph learn-glyph--${mark}`, viewBox: '0 0 14 14', width: 14, height: 14, fill: 'none', role: 'img', 'aria-label': MARK_LABELS[mark] }, shapes[mark]);
}

/** Coupe une valeur en « l'essentiel » et « la précision » : `Non : socket TCP brute` → `['Non', 'socket TCP brute']`. */
function splitValue(text) {
  const colon = text.indexOf(' : ');
  if (colon > 0) return [text.slice(0, colon), text.slice(colon + 3)];
  const trailing = text.match(/^(.+?)\s*\(([^()]+)\)$/);
  if (trailing) return [trailing[1], trailing[2]];
  const leading = text.match(/^(\S+ \([^()]+\)) (.+)$/);
  if (leading) return [leading[1], leading[2]];
  const comma = text.indexOf(', ');
  if (comma > 0) return [text.slice(0, comma), text.slice(comma + 2)];
  return [text, ''];
}

function isBlank(text) {
  return !text || text === '—' || /^sans objet$/i.test(text);
}

function qualitativeCell(text, mark, id) {
  const info = protocol(id);
  if (isBlank(text)) return h('td.learn-matrix__cell', Glyph('na'), h('span.sr-only', text || 'Sans objet'));
  const [head, detail] = splitValue(text);
  return h(
    'td.learn-matrix__cell',
    { style: { '--proto': info.color, '--proto-fg': info.fg } },
    h('div.learn-matrix__value', Glyph(mark ?? 'dot'), h('div.learn-matrix__words', h('span.learn-matrix__head', head), detail ? h('span.learn-matrix__detail', detail) : null)),
  );
}

function measuredCell(text, id, series) {
  const info = protocol(id);
  if (isBlank(text)) return h('td.learn-matrix__cell', Glyph('na'), h('span.sr-only', 'Non mesuré'));
  const [, number, unit] = text.match(/^(.*\d)\s+(\D+)$/) ?? [null, text, ''];
  const value = series?.values[id];
  const max = series ? Math.max(...Object.values(series.values)) : 0;
  const remote = series ? Object.entries(series.values).filter(([name]) => name !== 'local') : [];
  const lowest = remote.length > 1 ? remote.reduce((best, entry) => (entry[1] < best[1] ? entry : best))[0] : null;
  return h(
    'td.learn-matrix__cell',
    { style: { '--proto': info.color, '--proto-fg': info.fg }, class: { 'is-best': lowest === id } },
    h('span.learn-matrix__number', h('span.num', number), unit ? h('span.learn-figure__unit', unit) : null),
    Number.isFinite(value) && max > 0 ? h('span.learn-matrix__bar', { 'aria-hidden': 'true' }, h('span.learn-matrix__fill', { style: { '--ratio': String(Math.max(0.012, value / max)) } })) : null,
    lowest === id ? h('span.learn-matrix__best', series.best) : null,
  );
}

function header(columns) {
  return h(
    'thead',
    h(
      'tr',
      h('th.learn-matrix__corner', { scope: 'col' }, 'Critère'),
      columns.map(({ id, label }) => {
        const info = protocol(id);
        return h(
          'th.learn-matrix__proto',
          { scope: 'col', style: { '--proto': info.color, '--proto-fg': info.fg } },
          h('span.learn-matrix__proto-name', h('span.learn-matrix__proto-dot', { 'aria-hidden': 'true' }), label),
        );
      }),
    ),
  );
}

function legend(hasMeasured) {
  return h(
    'footer.learn-matrix__legend',
    h('ul.learn-matrix__marks', ['full', 'half', 'empty', 'dot', 'na'].map((mark) => h('li', Glyph(mark), MARK_LABELS[mark]))),
    h(
      'p.learn-matrix__note',
      hasMeasured
        ? 'Les pastilles sont une lecture qualitative ; les trois dernières lignes sont mesurées par le dernier banc d’essai, barres à l’échelle de la ligne.'
        : 'Les pastilles sont une lecture qualitative. Lancez le banc d’essai : trois lignes mesurées s’ajouteront au tableau.',
    ),
  );
}

/**
 * Matrice comparative.
 * @param {Object} props
 * @param {Object} props.summary `GET /api/summary` (`comparison`, `protocols`).
 * @param {Object|null} [props.report] Dernier rapport de banc d'essai, pour l'échelle des barres.
 * @returns {HTMLElement}
 */
export function Matrix({ summary, report = null }) {
  const rows = summary.comparison ?? [];
  if (!rows.length) {
    return Card({ padding: 'none' }, EmptyState({ size: 'sm', icon: 'table', title: 'Aucun critère à comparer', text: 'Le laboratoire n’a renvoyé aucune ligne pour le comparatif.' }));
  }
  const known = new Map((summary.protocols ?? []).map((entry) => [entry.id, entry.label]));
  const columns = PROTOCOL_IDS.filter((id) => rows.some((row) => id in row)).map((id) => ({ id, label: known.get(id) ?? protocol(id).label }));
  const qualitative = rows.filter((row) => !row.measured);
  const measured = rows.filter((row) => row.measured);

  const body = h(
    'tbody',
    qualitative.map((row) => {
      const marks = RATINGS[row.criterion];
      return h(
        'tr',
        h('th.learn-matrix__criterion', { scope: 'row' }, row.criterion),
        columns.map(({ id }) => qualitativeCell(String(row[id] ?? ''), marks?.[PROTOCOL_IDS.indexOf(id)], id)),
      );
    }),
    measured.length
      ? h('tr.learn-matrix__divider', h('th', { scope: 'colgroup', colspan: columns.length + 1 }, h('span.t-label', 'Mesuré par le dernier banc d’essai')))
      : null,
    measured.map((row) => {
      const series = measuredSeries(row.criterion, report);
      return h(
        'tr.learn-matrix__measured',
        h('th.learn-matrix__criterion', { scope: 'row' }, row.criterion.replace(/\s*\(mesurée?s?\)\s*$/i, '')),
        columns.map(({ id }) => measuredCell(String(row[id] ?? ''), id, series)),
      );
    }),
  );

  return Card(
    { padding: 'none', class: 'learn-matrix' },
    h('div.learn-matrix__scroll', h('table.learn-matrix__table', h('caption.sr-only', 'Comparatif qualitatif des quatre façons d’appeler une procédure'), header(columns), body)),
    legend(measured.length > 0),
  );
}

/**
 * Squelette de chargement de la matrice.
 * @returns {HTMLElement}
 */
export function MatrixSkeleton() {
  return Card(
    { padding: 'md', class: 'learn-matrix' },
    h('div.learn-matrix__skeleton', { 'aria-hidden': 'true' }, Array.from({ length: 6 }, () => h('div.learn-matrix__skeleton-row', Array.from({ length: 5 }, () => Skeleton({ height: 14 }))))),
  );
}
