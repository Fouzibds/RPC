/**
 * Table : colonnes typées, en-tête collant, tri, survol, état vide, densité compacte.
 */

import { h, append } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { EmptyState, Skeleton } from './feedback.js';

/**
 * @typedef {Object} TableColumn
 * @property {string} key Clé de la valeur dans chaque ligne.
 * @property {string} label En-tête.
 * @property {'left'|'right'|'center'} [align='left'] `right` pour les nombres.
 * @property {number|string} [width] Largeur fixe (px ou valeur CSS). En `layout: 'fixed'`, les colonnes sans largeur se partagent le reste.
 * @property {boolean} [mono] Valeur en police à chasse fixe, chiffres tabulaires.
 * @property {boolean} [wrap] Autorise le retour à la ligne dans la cellule (texte long).
 * @property {boolean} [sortable]
 * @property {(value: *, row: Object) => string|number|Node|null} [format] Rendu de la cellule (texte ou nœud).
 * @property {(row: Object) => number|string} [sortValue] Valeur utilisée pour trier (par défaut `row[key]`).
 * @property {string} [title] Info-bulle native de l'en-tête.
 */

function compare(a, b) {
  const aMissing = a === null || a === undefined || a === '';
  const bMissing = b === null || b === undefined || b === '';
  if (aMissing || bMissing) return aMissing === bMissing ? 0 : aMissing ? 1 : -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), 'fr', { numeric: true, sensitivity: 'base' });
}

/**
 * Tableau de données.
 * @param {Object} props
 * @param {TableColumn[]} props.columns
 * @param {Object[]} [props.rows]
 * @param {(row: Object, index: number) => string|number} [props.rowKey] Identité d'une ligne (posée dans `data-key`).
 * @param {{key: string, dir: 'asc'|'desc'}} [props.sort] Tri initial.
 * @param {(sort: {key: string, dir: 'asc'|'desc'}) => void} [props.onSort] Si fourni, le tri est délégué à l'appelant.
 * @param {(row: Object, event: Event) => void} [props.onRowClick] Rend les lignes cliquables (et focalisables).
 * @param {(row: Object) => string|null} [props.rowTone] `success|warning|danger|info|accent` : fine barre colorée à gauche.
 * @param {'normal'|'compact'} [props.density='normal'] Hauteur de ligne 40 ou 32 px.
 * @param {'auto'|'fixed'} [props.layout='auto'] `fixed` : le tableau tient toujours dans son conteneur ; les
 *   largeurs viennent des colonnes (`width`) et un contenu trop long est coupé par « … » au lieu d'élargir
 *   le tableau (le texte complet reste accessible par l'info-bulle native des cellules de texte).
 * @param {boolean} [props.stickyHeader=true]
 * @param {number|string} [props.maxHeight] Hauteur maximale du corps défilant.
 * @param {{icon?: string, title?: string, text?: string}} [props.empty] Contenu de l'état vide.
 * @param {string} [props.caption] Légende accessible.
 * @returns {HTMLElement & {setRows: (rows: Object[]) => void, setColumns: (columns: TableColumn[]) => void, setSort: (sort: {key: string, dir: 'asc'|'desc'}|null) => void, setLoading: (on: boolean, rows?: number) => void, rows: Object[]}}
 *   `setColumns` remplace les colonnes en gardant les lignes ; le tri courant est conservé si sa colonne existe encore.
 */
export function Table({
  columns = [],
  rows = [],
  rowKey,
  sort = null,
  onSort,
  onRowClick,
  rowTone,
  density = 'normal',
  layout = 'auto',
  stickyHeader = true,
  maxHeight,
  empty = {},
  caption,
} = {}) {
  let data = rows;
  let currentSort = sort;
  let loading = 0;
  let headCells = [];

  function headCell(column) {
    const label = h('span.table__th-label', column.label);
    return h(
      'th.table__th',
      {
        scope: 'col',
        class: [`table__cell--${column.align ?? 'left'}`],
        style: column.width !== undefined ? { width: typeof column.width === 'number' ? `${column.width}px` : column.width } : null,
        title: column.title,
      },
      column.sortable
        ? h(
            'button.table__sort',
            { type: 'button', onClick: () => toggleSort(column.key) },
            label,
            h('span.table__sort-icon', { 'aria-hidden': 'true' }, icon('arrow-up', { size: 12, stroke: 2 })),
          )
        : label,
    );
  }

  const headRow = h('tr');
  const body = h('tbody.table__body');
  const table = h(
    'table.table',
    { class: [`table--${density}`, { 'table--fixed': layout === 'fixed', 'table--sticky': stickyHeader, 'table--clickable': Boolean(onRowClick) }] },
    caption ? h('caption.sr-only', caption) : null,
    h('thead.table__head', headRow),
    body,
  );

  function buildHead() {
    headCells = columns.map(headCell);
    headRow.replaceChildren(...headCells);
  }
  const el = h(
    'div.table-wrap.scroll-x',
    { style: maxHeight !== undefined ? { maxHeight: typeof maxHeight === 'number' ? `${maxHeight}px` : maxHeight, overflowY: 'auto' } : null },
    table,
  );

  function toggleSort(key) {
    const dir = currentSort?.key === key && currentSort.dir === 'asc' ? 'desc' : 'asc';
    currentSort = { key, dir };
    if (onSort) onSort(currentSort);
    render();
  }

  function sorted() {
    if (!currentSort || onSort) return data;
    const column = columns.find((item) => item.key === currentSort.key);
    if (!column) return data;
    const valueOf = column.sortValue ?? ((row) => row[column.key]);
    const factor = currentSort.dir === 'desc' ? -1 : 1;
    return data
      .map((row, index) => ({ row, index }))
      .sort((a, b) => factor * compare(valueOf(a.row), valueOf(b.row)) || a.index - b.index)
      .map((item) => item.row);
  }

  function renderHead() {
    columns.forEach((column, index) => {
      const active = currentSort?.key === column.key;
      const cell = headCells[index];
      if (column.sortable) cell.setAttribute('aria-sort', active ? (currentSort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
      cell.dataset.sorted = active ? currentSort.dir : '';
    });
  }

  function renderRow(row, index) {
    const tone = rowTone?.(row);
    const tr = h(
      'tr.table__row',
      {
        dataset: { key: rowKey ? rowKey(row, index) : null, tone },
        tabIndex: onRowClick ? 0 : null,
        onClick: onRowClick ? (event) => onRowClick(row, event) : null,
        onKeydown: onRowClick
          ? (event) => {
              if (event.key === 'Enter' && event.target === tr) onRowClick(row, event);
            }
          : null,
      },
      columns.map((column) => {
        const raw = row[column.key];
        const content = column.format ? column.format(raw, row) : raw;
        const missing = content === null || content === undefined || content === '';
        // En largeur fixe, un texte coupé par « … » reste lisible en entier par l'info-bulle native.
        const clipped = layout === 'fixed' && !column.wrap && !missing && !(content instanceof Node);
        return append(
          h('td.table__td', {
            class: [`table__cell--${column.align ?? 'left'}`, { num: column.mono, 'table__cell--wrap': column.wrap, 'table__cell--missing': missing }],
            title: clipped ? String(content) : null,
          }),
          missing ? '—' : content,
        );
      }),
    );
    return tr;
  }

  function render() {
    renderHead();
    body.replaceChildren();
    if (loading) {
      for (let r = 0; r < loading; r += 1) {
        body.appendChild(
          h(
            'tr.table__row.table__row--loading',
            columns.map((column, c) =>
              h('td.table__td', { class: `table__cell--${column.align ?? 'left'}` }, Skeleton({ width: `${45 + ((r * 7 + c * 13) % 40)}%` })),
            ),
          ),
        );
      }
      return;
    }
    const list = sorted();
    if (!list.length) {
      body.appendChild(
        h(
          'tr.table__row.table__row--empty',
          h(
            'td.table__td',
            { colspan: columns.length },
            EmptyState({ size: 'sm', icon: empty.icon ?? 'inbox', title: empty.title ?? 'Aucune donnée', text: empty.text ?? '' }),
          ),
        ),
      );
      return;
    }
    list.forEach((row, index) => body.appendChild(renderRow(row, index)));
  }

  Object.defineProperty(el, 'rows', { get: () => data });
  el.setRows = (next) => {
    data = next ?? [];
    loading = 0;
    render();
  };
  el.setColumns = (next) => {
    columns = next ?? [];
    if (currentSort && !columns.some((column) => column.key === currentSort.key)) currentSort = null;
    buildHead();
    render();
  };
  el.setSort = (next) => {
    currentSort = next;
    render();
  };
  el.setLoading = (on, count = 5) => {
    loading = on ? count : 0;
    render();
  };
  buildHead();
  render();
  return el;
}
