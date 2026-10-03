/**
 * Vue hexadécimale annotée : décalage, octets, gouttière ASCII. Les segments décrits par les
 * évènements de trace (`detail.segments` : trame, en-têtes, tag / longueur / valeur Protobuf)
 * teintent leurs octets ; survoler un octet met tout son segment en avant dans les deux
 * colonnes. `SegmentList` présente les mêmes segments sous forme de tableau, champ par champ.
 */

import { h, clear } from '../core/dom.js';
import { fmtBytes, fmtNumber } from '../core/format.js';
import { CHART_COLORS, chartTooltip, measureText, tipContent } from './charts/core.js';
import { CopyButton, Segmented } from './ui.js';

/**
 * @typedef {Object} Segment
 * @property {string} label Description (« Longueur du message », « Tag — champ 1 « product_id »… »).
 * @property {number} start Premier octet (inclus).
 * @property {number} end Dernier octet (exclu).
 * @property {'frame'|'header'|'tag'|'len'|'value'|'body'} kind
 * @property {string} [field] Nom du champ Protobuf.
 * @property {number} [number] Numéro du champ Protobuf.
 * @property {number} [wire_type] Type de fil (0 VARINT, 1 I64, 2 LEN, 5 I32).
 * @property {string} [wire_type_name]
 * @property {string} [type] Type déclaré dans le contrat (`string`, `sint32`…).
 * @property {*} [value] Valeur décodée.
 * @property {number} [depth] Niveau d'imbrication (sous-messages).
 * @property {boolean} [mismatch] Le contenu contredit le contrat.
 */

const KIND_LABELS = { frame: 'Trame', header: 'En-tête', tag: 'Tag', len: 'Longueur', value: 'Valeur', body: 'Corps' };
const HARD_LIMIT = 8192;
/** Octets montrés par segment dans `SegmentList` avant « … ». */
const CHUNK_BYTES = 6;

/**
 * Convertit une chaîne hexadécimale (espaces tolérés) ou une liste d'octets en `Uint8Array`.
 * @param {string|Uint8Array|number[]|null|undefined} input
 * @returns {Uint8Array}
 */
export function toBytes(input) {
  if (!input) return new Uint8Array(0);
  if (typeof input !== 'string') return Uint8Array.from(input);
  const clean = input.replace(/[^0-9a-fA-F]/g, '');
  const bytes = new Uint8Array(Math.floor(clean.length / 2));
  for (let i = 0; i < bytes.length; i += 1) bytes[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
  return bytes;
}

/**
 * Largeur, en pixels, qu'il faut à la vue pour afficher `perRow` octets par ligne sans défilement :
 * décalage (4 car.), octets (2 car. + 8 px chacun, 8 px de plus entre deux groupes de huit), gouttière
 * ASCII facultative (1 car. + 1 px par octet), deux gouttières de 16 px et 24 px de marges. Ces
 * dimensions sont celles de `.hexview__*` dans css/charts.css.
 */
function rowWidth(perRow, ascii) {
  const ch = measureText('0', { size: 12, mono: true });
  return 4 * ch + 16 + perRow * (2 * ch + 8) + (perRow / 8 - 1) * 8 + (ascii ? 16 + perRow * (ch + 1) : 0) + 24;
}

const hex2 = (byte) => byte.toString(16).padStart(2, '0');
const printable = (byte) => (byte >= 0x20 && byte <= 0x7e ? String.fromCharCode(byte) : '·');

function formatValue(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'string') return `"${value.length > 80 ? `${value.slice(0, 80)}…` : value}"`;
  if (typeof value === 'object') {
    const text = JSON.stringify(value);
    return text.length > 80 ? `${text.slice(0, 80)}…` : text;
  }
  return String(value);
}

function rangeLabel(start, end) {
  const count = end - start;
  return count <= 1 ? `octet ${start}` : `octets ${start}–${end - 1}`;
}

/**
 * Regroupe les segments par identité visuelle : chaque champ Protobuf (tag + longueur + valeur)
 * forme un groupe et reçoit une teinte ; les segments de trame restent neutres.
 * @param {Segment[]} segments
 * @returns {{groups: Array<{label: string, color: string|null, indices: number[], start: number, end: number}>, groupOf: number[]}}
 */
function groupSegments(segments) {
  const groups = [];
  const byKey = new Map();
  const groupOf = [];
  let hue = 0;
  segments.forEach((segment, index) => {
    const isField = segment.field !== undefined || segment.number !== undefined;
    const key = isField ? `field|${segment.depth ?? 0}|${segment.number ?? segment.field}|${segment.field ?? ''}` : `segment|${index}`;
    let position = byKey.get(key);
    if (position === undefined) {
      position = groups.length;
      byKey.set(key, position);
      const color = segment.kind === 'frame' ? null : CHART_COLORS[hue % CHART_COLORS.length];
      if (segment.kind !== 'frame') hue += 1;
      const label = isField ? `${segment.field ?? `#${segment.number}`}${segment.number !== undefined ? ` · champ ${segment.number}` : ''}` : segment.label;
      groups.push({ label, color, indices: [], start: segment.start, end: segment.end });
    }
    const group = groups[position];
    group.indices.push(index);
    group.start = Math.min(group.start, segment.start);
    group.end = Math.max(group.end, segment.end);
    groupOf.push(position);
  });
  return { groups, groupOf };
}

/**
 * Vue hexadécimale colorée d'une charge utile.
 *
 *     const view = HexView({ hex: event.payload_hex, segments: event.detail.segments,
 *       onSegmentHover: (segment, index) => list.highlight(index) });
 *
 * @param {Object} [props]
 * @param {string} [props.hex] Octets en hexadécimal (`payload_hex` d'un évènement de trace).
 * @param {Uint8Array|number[]} [props.bytes] Octets bruts (prioritaires sur `hex`).
 * @param {Segment[]} [props.segments] Découpage coloré ; sans lui, simple vidage.
 * @param {Array<{id: string, label: string, segments: Segment[]}>} [props.readings] Plusieurs lectures des mêmes octets
 *   (« vu par l'émetteur », « vu par le lecteur v2 »…) : un sélecteur dans la barre recolore le vidage avec la
 *   lecture choisie. Prioritaire sur `segments`.
 * @param {string} [props.reading] Identifiant de la lecture active (la première par défaut).
 * @param {(id: string) => void} [props.onReadingChange] Appelé quand l'utilisateur change de lecture.
 * @param {8|16|'auto'} [props.bytesPerRow='auto'] `auto` : 16 octets par ligne dès que la largeur du conteneur le
 *   permet (gouttière ASCII comprise), 8 sinon.
 * @param {number} [props.maxBytes=256] Octets affichés avant le bouton « afficher la suite » (un reste
 *   inférieur au quart de cette limite est affiché d'emblée).
 * @param {string} [props.title] Titre de la barre supérieure.
 * @param {boolean} [props.legend=true] Liste des segments sous le vidage (survol = mise en avant ; défile
 *   au-delà de quatre lignes).
 * @param {boolean} [props.ascii=true] Gouttière ASCII.
 * @param {boolean} [props.copy=true] Bouton « copier en hexadécimal ».
 * @param {number|string} [props.maxHeight] Hauteur maximale du vidage (défilement au-delà).
 * @param {(segment: Segment|null, index: number) => void} [props.onSegmentHover] Segment survolé (`null`, `-1` en sortie).
 * @returns {HTMLElement & {update: (patch: Object) => void, highlight: (indices: number|number[]|null) => void, setSegments: (segments: Segment[]) => void, setReading: (id: string) => void, segments: Segment[], destroy: () => void}}
 *   `highlight` met en avant un ou plusieurs segments (par leur rang dans la lecture affichée) ;
 *   `setSegments` recolore les mêmes octets avec un autre découpage, sans toucher au défilement ni à
 *   « afficher la suite » ; `setReading` affiche une des `readings` ; `segments` est le découpage affiché.
 */
export function HexView(props = {}) {
  const state = { bytesPerRow: 'auto', maxBytes: 256, legend: true, ascii: true, copy: true, segments: [], ...props };
  const count = h('span.hexview__count.num');
  const title = h('span.hexview__title');
  const readingSlot = h('div.hexview__readings');
  const tools = h('div.hexview__tools');
  const grid = h('div.hexview__grid', { role: 'presentation' });
  const scroller = h('div.hexview__scroll.scroll-x.scroll-y', grid);
  const more = h('button.hexview__more', { type: 'button' });
  const legend = h('div.hexview__legend');
  const el = h('div.hexview', h('div.hexview__bar', h('div.hexview__heading', title, count), readingSlot, tools), scroller, more, legend);

  let bytes = new Uint8Array(0);
  let shown = 0;
  let perRow = 16;
  let width = 0;
  let segments = [];
  let segmentOf = [];
  let grouping = { groups: [], groupOf: [] };
  let cells = [];
  let legendNodes = [];
  let hovered = -1;
  let readingSwitch = null;
  let readingSignature = '';

  function mapSegments() {
    const reading = state.readings?.find((item) => item.id === state.reading) ?? state.readings?.[0];
    segments = (reading ? reading.segments : state.segments) ?? [];
    segmentOf = new Array(bytes.length).fill(-1);
    segments.forEach((segment, index) => {
      const end = Math.min(bytes.length, segment.end);
      for (let i = Math.max(0, segment.start); i < end; i += 1) segmentOf[i] = index;
    });
    grouping = groupSegments(segments);
  }

  /** 16 octets par ligne dès que la largeur mesurée le permet ; avant la première mesure, 16. */
  function autoPerRow() {
    return width > 0 && width < Math.floor(rowWidth(16, state.ascii)) ? 8 : 16;
  }

  function setActive(indices) {
    const active = new Set(indices);
    el.dataset.hover = String(active.size > 0);
    for (let i = 0; i < cells.length; i += 1) {
      const on = active.has(segmentOf[i]);
      cells[i][0].classList.toggle('is-active', on);
      cells[i][1]?.classList.toggle('is-active', on);
    }
    const groups = new Set(indices.map((index) => grouping.groupOf[index]));
    legendNodes.forEach((node, index) => node.toggleAttribute('data-active', groups.has(index)));
  }

  function cellClass(index) {
    const segmentIndex = segmentOf[index];
    if (segmentIndex < 0) {
      const byte = bytes[index];
      return byte === 0 ? 'hexview__byte is-zero' : byte < 0x20 || byte > 0x7e ? 'hexview__byte is-binary' : 'hexview__byte';
    }
    const segment = segments[segmentIndex];
    return [
      'hexview__byte',
      'is-tinted',
      `hexview__byte--${segment.kind}`,
      index === segment.start ? 'is-start' : '',
      index === segment.end - 1 ? 'is-end' : '',
      segment.mismatch ? 'is-mismatch' : '',
    ].join(' ');
  }

  function renderRows() {
    const rows = [];
    cells = [];
    for (let offset = 0; offset < shown; offset += perRow) {
      const hexCells = [];
      const asciiCells = [];
      for (let i = offset; i < Math.min(shown, offset + perRow); i += 1) {
        const segmentIndex = segmentOf[i];
        const color = segmentIndex >= 0 ? grouping.groups[grouping.groupOf[segmentIndex]].color : null;
        const style = color ? { '--seg': color } : null;
        const className = cellClass(i);
        const mid = (i - offset) % 8 === 0 && i !== offset ? ' is-mid' : '';
        const hexCell = h('span', { class: className + mid, style, dataset: { i }, textContent: hex2(bytes[i]) });
        hexCells.push(hexCell);
        let asciiCell = null;
        if (state.ascii) {
          asciiCell = h('span', { class: className.replace('hexview__byte', 'hexview__char'), style, dataset: { i }, textContent: printable(bytes[i]) });
          asciiCells.push(asciiCell);
        }
        cells.push([hexCell, asciiCell]);
      }
      rows.push(
        h(
          'div.hexview__row',
          h('span.hexview__offset', { textContent: offset.toString(16).padStart(4, '0') }),
          h('span.hexview__hex', hexCells),
          state.ascii ? h('span.hexview__ascii', asciiCells) : null,
        ),
      );
    }
    clear(grid, rows);
    grid.style.setProperty('--per-row', String(perRow));
    el.dataset.segmented = String(segments.length > 0);
  }

  /** Sélecteur de lecture : recréé seulement quand la liste des lectures change, pour garder le focus clavier. */
  function renderReadings() {
    const list = state.readings?.length > 1 ? state.readings : [];
    const signature = list.map((item) => `${item.id}|${item.label}`).join('\n');
    if (signature !== readingSignature) {
      readingSignature = signature;
      readingSwitch = list.length
        ? Segmented({
            size: 'sm',
            ariaLabel: 'Lecture des octets',
            options: list.map((item) => ({ value: item.id, label: item.label })),
            onChange: (id) => {
              el.setReading(id);
              state.onReadingChange?.(id);
            },
          })
        : null;
      clear(readingSlot, readingSwitch);
    }
    readingSwitch?.setValue((list.find((item) => item.id === state.reading) ?? list[0]).id);
  }

  function renderLegend() {
    const { groups } = grouping;
    legend.hidden = !state.legend || groups.length === 0;
    legendNodes = groups.map((group) => {
      const node = h(
        'span.hexview__entry',
        { style: group.color ? { '--seg': group.color } : null, tabIndex: 0 },
        h('span.hexview__swatch'),
        h('span.hexview__entry-label.truncate', group.label),
        h('span.hexview__entry-range.num', group.end - group.start <= 1 ? String(group.start) : `${group.start}–${group.end - 1}`),
      );
      const on = () => {
        setActive(group.indices);
        state.onSegmentHover?.(segments[group.indices[0]], group.indices[0]);
      };
      const off = () => {
        setActive([]);
        state.onSegmentHover?.(null, -1);
      };
      node.addEventListener('pointerenter', on);
      node.addEventListener('pointerleave', off);
      node.addEventListener('focus', on);
      node.addEventListener('blur', off);
      return node;
    });
    clear(legend, legendNodes);
  }

  function render() {
    bytes = state.bytes ? toBytes(state.bytes) : toBytes(state.hex);
    perRow = state.bytesPerRow === 'auto' ? autoPerRow() : state.bytesPerRow;
    shown = Math.min(bytes.length, Math.max(shown, state.maxBytes));
    if (bytes.length - shown <= Math.max(16, state.maxBytes / 4)) shown = bytes.length;
    mapSegments();
    clear(title, state.title ?? 'Octets sur le fil');
    count.textContent = fmtBytes(bytes.length, { exact: true });
    renderReadings();
    clear(tools, state.copy && bytes.length ? CopyButton({ text: () => Array.from(bytes, hex2).join(' '), label: 'Copier en hexadécimal' }) : null);
    scroller.style.maxHeight = state.maxHeight === undefined ? '' : typeof state.maxHeight === 'number' ? `${state.maxHeight}px` : state.maxHeight;
    renderRows();
    renderLegend();
    const rest = bytes.length - shown;
    more.hidden = rest <= 0;
    more.textContent = rest > HARD_LIMIT ? `Afficher ${fmtNumber(HARD_LIMIT)} octets de plus (${fmtNumber(rest)} restants)` : `Afficher la suite · ${fmtNumber(rest)} octet${rest > 1 ? 's' : ''}`;
    el.dataset.empty = String(bytes.length === 0);
    if (bytes.length === 0) clear(grid, h('p.hexview__none', 'Aucun octet : cette étape ne transporte pas de message.'));
  }

  more.addEventListener('click', () => {
    shown = Math.min(bytes.length, shown + HARD_LIMIT);
    render();
  });

  grid.addEventListener('pointermove', (event) => {
    const cell = event.target.closest?.('[data-i]');
    if (!cell) return;
    const index = Number(cell.dataset.i);
    const segmentIndex = segmentOf[index];
    if (segmentIndex !== hovered) {
      hovered = segmentIndex;
      setActive(segmentIndex >= 0 ? [segmentIndex] : []);
      state.onSegmentHover?.(segmentIndex >= 0 ? segments[segmentIndex] : null, segmentIndex);
    }
    const byte = bytes[index];
    const segment = segmentIndex >= 0 ? segments[segmentIndex] : null;
    chartTooltip.show({
      owner: el,
      key: `${segmentIndex}|${index}`,
      x: event.clientX,
      y: event.clientY,
      content: () => {
        const value = segment ? formatValue(segment.value) : null;
        return tipContent({
          title: segment ? segment.label : `Octet ${index}`,
          rows: [
            ...(segment
              ? [
                  { label: KIND_LABELS[segment.kind] ?? segment.kind, value: `${rangeLabel(segment.start, segment.end)} · ${fmtBytes(segment.end - segment.start)}`, color: grouping.groups[grouping.groupOf[segmentIndex]].color ?? 'var(--fg-2)', shape: 'rect' },
                  ...(value !== null ? [{ label: 'Valeur décodée', value }] : []),
                ]
              : []),
            { label: `Octet 0x${index.toString(16).padStart(4, '0')}`, value: `0x${hex2(byte)} · ${byte} · ${byte.toString(2).padStart(8, '0')}`, muted: true },
          ],
        });
      },
    });
  });
  grid.addEventListener('pointerleave', () => {
    hovered = -1;
    setActive([]);
    chartTooltip.hide(el);
    state.onSegmentHover?.(null, -1);
  });

  const observer = new ResizeObserver((entries) => {
    const next = Math.floor(entries[entries.length - 1].contentRect.width);
    width = next;
    if (state.bytesPerRow === 'auto' && autoPerRow() !== perRow) render();
  });
  observer.observe(el);

  el.update = (patch) => {
    if ('hex' in patch || 'bytes' in patch) shown = 0;
    if ('hex' in patch && !('bytes' in patch)) state.bytes = undefined;
    Object.assign(state, patch);
    render();
  };
  el.highlight = (indices) => setActive(indices === null || indices === undefined || indices === -1 ? [] : [].concat(indices));
  el.setSegments = (next) => el.update({ segments: next ?? [], readings: undefined });
  el.setReading = (id) => el.update({ reading: id });
  Object.defineProperty(el, 'segments', { get: () => segments });
  el.destroy = () => {
    observer.disconnect();
    chartTooltip.hide(el);
  };
  render();
  return el;
}

/** Fusionne les segments d'un même champ (tag, longueur, valeur) en une ligne de tableau. */
function fieldRows(segments) {
  const rows = [];
  let current = null;
  segments.forEach((segment, index) => {
    const isField = segment.kind === 'tag' || segment.kind === 'len' || segment.kind === 'value';
    if (isField && segment.kind !== 'tag' && current && current.field && current.number === segment.number && current.depth === (segment.depth ?? 0)) {
      current.parts.push({ segment, index });
      current.end = segment.end;
      if (segment.kind === 'value') current.value = segment.value;
      return;
    }
    current = {
      field: isField,
      number: segment.number,
      depth: segment.depth ?? 0,
      name: isField ? (segment.field ?? '—') : segment.label,
      kind: segment.kind,
      type: segment.type,
      wire: isField ? `${segment.wire_type_name ?? ''}${segment.wire_type !== undefined ? ` (${segment.wire_type})` : ''}` : '',
      mismatch: Boolean(segment.mismatch),
      start: segment.start,
      end: segment.end,
      value: isField ? undefined : segment.value,
      parts: [{ segment, index }],
    };
    rows.push(current);
  });
  return rows;
}

/**
 * Tableau compact des segments d'un message, une ligne par champ Protobuf : numéro, nom, type
 * de fil, octets (tag · longueur · valeur) et valeur décodée. Les lignes de trame et d'en-tête
 * y figurent aussi. Le tableau s'adapte à sa largeur : sous 640 px la colonne des octets ne garde
 * que la taille, sous 420 px le type de fil disparaît ; la valeur se tronque (texte complet au survol).
 * Un libellé de trame ou d'en-tête trop long passe sur deux lignes au lieu d'être coupé ; au-delà,
 * le texte complet reste dans l'info-bulle.
 * À relier à une `HexView` par les survols :
 *
 *     const list = SegmentList({ hex, segments, onHover: (indices) => view.highlight(indices) });
 *     const view = HexView({ hex, segments, onSegmentHover: (_, index) => list.highlight(index) });
 *
 * @param {Object} [props]
 * @param {Segment[]} [props.segments]
 * @param {string} [props.hex] Octets du message : affiche les octets de chaque champ.
 * @param {Uint8Array|number[]} [props.bytes]
 * @param {(indices: number[]|null) => void} [props.onHover] Rangs des segments de la ligne survolée (`null` en sortie).
 * @param {number|string} [props.maxHeight]
 * @returns {HTMLElement & {update: (patch: Object) => void, highlight: (index: number|null) => void, destroy: () => void}}
 *   `highlight(index)` met en avant la ligne qui contient le segment de rang `index`.
 */
export function SegmentList(props = {}) {
  const state = { segments: [], ...props };
  const body = h('tbody');
  const table = h(
    'table.seglist__table',
    h(
      'thead',
      h('tr', h('th.seglist__num', { scope: 'col' }, 'N°'), h('th', { scope: 'col' }, 'Champ'), h('th', { scope: 'col' }, 'Type de fil'), h('th', { scope: 'col' }, 'Octets'), h('th', { scope: 'col' }, 'Valeur')),
    ),
    body,
  );
  const el = h('div.seglist.scroll-x.scroll-y', table);
  let rowNodes = [];
  let rowOfSegment = [];

  function bytesCell(row, bytes, colors) {
    if (!bytes.length) return h('span.seglist__range.num', rangeLabel(row.start, row.end));
    const chunks = row.parts.map(({ segment, index }) => {
      const slice = Array.from(bytes.subarray(segment.start, Math.min(segment.end, segment.start + CHUNK_BYTES)), hex2).join(' ');
      const more = segment.end - segment.start > CHUNK_BYTES ? ' …' : '';
      return h('span', { class: `seglist__bytes seglist__bytes--${segment.kind}`, style: colors[index] ? { '--seg': colors[index] } : null, textContent: slice + more });
    });
    return h('span.seglist__chunks.mono', chunks);
  }

  function render() {
    const segments = state.segments ?? [];
    const bytes = state.bytes ? toBytes(state.bytes) : toBytes(state.hex);
    const { groups, groupOf } = groupSegments(segments);
    const colors = segments.map((_, index) => groups[groupOf[index]].color);
    const rows = fieldRows(segments);
    rowOfSegment = [];
    rowNodes = rows.map((row, rowIndex) => {
      row.parts.forEach(({ index }) => {
        rowOfSegment[index] = rowIndex;
      });
      const value = formatValue(row.value);
      const node = h(
        'tr.seglist__row',
        { class: { 'is-mismatch': row.mismatch }, style: colors[row.parts[0].index] ? { '--seg': colors[row.parts[0].index] } : null },
        h('td.seglist__num.num', row.field && row.number !== undefined ? String(row.number) : '—'),
        h(
          'td',
          h(
            'span.seglist__name',
            { style: { paddingLeft: `${row.depth * 14}px` } },
            h('span.seglist__swatch'),
            h('span.seglist__label', { class: { mono: row.field, truncate: row.field }, title: row.field ? null : row.name }, row.name),
            row.type ? h('span.seglist__type.mono', row.type) : null,
          ),
        ),
        h('td.seglist__wire.num', row.wire || (KIND_LABELS[row.kind] ?? row.kind)),
        h('td', bytesCell(row, bytes, colors), h('span.seglist__size.num', fmtBytes(row.end - row.start))),
        h(
          'td.seglist__value',
          value === null
            ? h('span.fg-3', row.type === 'message' ? 'sous-message' : '—')
            : h('span.seglist__lit.mono.truncate', { class: `seglist__lit--${typeof row.value}`, title: value }, value),
        ),
      );
      node.addEventListener('pointerenter', () => state.onHover?.(row.parts.map((part) => part.index)));
      node.addEventListener('pointerleave', () => state.onHover?.(null));
      return node;
    });
    clear(body, rowNodes.length ? rowNodes : h('tr', h('td.seglist__empty', { colSpan: 5 }, 'Aucun segment décrit pour ce message.')));
    el.style.maxHeight = state.maxHeight === undefined ? '' : typeof state.maxHeight === 'number' ? `${state.maxHeight}px` : state.maxHeight;
  }

  el.update = (patch) => {
    if ('hex' in patch && !('bytes' in patch)) state.bytes = undefined;
    Object.assign(state, patch);
    render();
  };
  el.highlight = (index) => {
    const target = index === null || index === undefined || index < 0 ? -1 : rowOfSegment[index];
    rowNodes.forEach((node, rowIndex) => node.toggleAttribute('data-active', rowIndex === target));
  };
  el.destroy = () => {};
  render();
  return el;
}
