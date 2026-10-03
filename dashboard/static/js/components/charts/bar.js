/**
 * Graphiques en barres : BarChart (une série, une couleur par barre) et GroupedBarChart
 * (catégories × séries, légende cliquable).
 */

import { svg, clear } from '../../core/dom.js';
import { fmtNumber } from '../../core/format.js';
import {
  axisGutter,
  buildAxis,
  chartTooltip,
  clientPoint,
  createChart,
  dataTable,
  elementPool,
  fitText,
  measureText,
  roundedBar,
  seriesColor,
  svgRoot,
  tipContent,
  valueAxis,
} from './core.js';

const RADIUS = 4;
const MIN_BAR = 2;

function colorOf(item, fallback) {
  return item.color || item.protocol || item.tone ? seriesColor(item) : fallback;
}

function setBar(path, d, color, index, horizontal) {
  path.setAttribute('d', d);
  path.setAttribute('class', horizontal ? 'chart__bar chart__bar--h' : 'chart__bar');
  path.style.setProperty('--c', color);
  path.style.setProperty('--i', String(index));
  path.removeAttribute('data-active');
  return path;
}

/**
 * @typedef {Object} BarDatum
 * @property {string} label Nom de la catégorie.
 * @property {number} value
 * @property {string} [id] Identité stable (par défaut le libellé) : garde la barre d'un rendu à l'autre.
 * @property {string} [short] Libellé abrégé, utilisé quand la place manque.
 * @property {'local'|'custom'|'grpc'|'rest'} [protocol] Donne à la barre la couleur du protocole.
 * @property {string} [color] Couleur CSS explicite (prioritaire).
 * @property {string} [hint] Précision affichée dans l'info-bulle.
 */

/**
 * Barres verticales ou horizontales, une seule série. Sommet arrondi, valeur écrite au bout de
 * chaque barre, info-bulle au survol, flèches du clavier pour parcourir les barres.
 *
 *     BarChart({ data: [{ label: 'gRPC / Protobuf', protocol: 'grpc', value: 121 }], format: fmtBytes })
 *
 * @param {Object} [props]
 * @param {BarDatum[]} [props.data]
 * @param {'vertical'|'horizontal'} [props.orientation='vertical'] `horizontal` : une ligne par catégorie
 *   (la hauteur se déduit du nombre de lignes), idéal pour un classement.
 * @param {(value: number) => string} [props.format=fmtNumber] Format des valeurs (`fmtMs`, `fmtBytes`…).
 * @param {(value: number) => string} [props.tickFormat] Format des graduations (par défaut `format`).
 * @param {boolean} [props.log=false] Échelle logarithmique (valeurs strictement positives).
 * @param {10|1024} [props.tickBase=10] Base des graduations : 1024 avec `fmtBytes`, pour des repères ronds (« 1 ko », « 2 ko »).
 * @param {boolean|'asc'|'desc'} [props.sorted=false] Trie les barres par valeur (`true` = décroissant).
 * @param {number} [props.height=240] Hauteur totale en pixels (orientation verticale).
 * @param {number} [props.rowHeight=34] Hauteur d'une ligne (orientation horizontale).
 * @param {number} [props.barSize] Épaisseur maximale d'une barre (24 px en vertical, 12 px en horizontal).
 * @param {boolean} [props.valueLabels=true] Écrit la valeur au bout de chaque barre.
 * @param {boolean} [props.axis] Affiche les graduations (par défaut : oui en vertical, non en horizontal).
 *   En horizontal sans axe, chaque barre repose sur une piste et sa valeur s'aligne à droite.
 * @param {string} [props.color='var(--accent)'] Couleur des barres sans `protocol` ni `color`.
 * @param {string} [props.seriesLabel='Valeur'] Nom de la grandeur, dans l'info-bulle.
 * @param {string} [props.ariaLabel] Nom accessible du graphique.
 * @param {{icon?: string, title?: string, text?: string}} [props.empty] Contenu de l'état vide.
 * @param {boolean} [props.loading] Squelette (ou voile sur le tracé précédent).
 * @returns {HTMLElement & {update: (patch: Object) => void, setLoading: (on: boolean) => void, destroy: () => void}}
 *   `update({ data })` anime les barres vers leurs nouvelles valeurs.
 */
export function BarChart(props = {}) {
  let root = null;
  let marks = [];
  const pool = elementPool();

  function prepare(state) {
    const fallback = state.color ?? 'var(--accent)';
    const rows = (state.data ?? [])
      .filter((item) => Number.isFinite(Number(item.value)))
      .map((item) => ({ ...item, value: Number(item.value), key: String(item.id ?? item.label), fill: colorOf(item, fallback) }));
    if (state.sorted) rows.sort((a, b) => (state.sorted === 'asc' ? a.value - b.value : b.value - a.value));
    return rows;
  }

  function deactivate() {
    if (!root) return;
    root.dataset.hover = 'false';
    for (const mark of marks) {
      mark.bar.removeAttribute('data-active');
      mark.band.removeAttribute('data-active');
    }
    chartTooltip.hide(el);
  }

  function activate(index, point) {
    const mark = marks[index];
    if (!mark) return;
    const state = el.state;
    root.dataset.hover = 'true';
    for (const other of marks) {
      other.bar.toggleAttribute('data-active', false);
      other.band.toggleAttribute('data-active', false);
    }
    mark.bar.setAttribute('data-active', 'true');
    mark.band.setAttribute('data-active', 'true');
    const at = point ?? clientPoint(root, mark.tipX, mark.tipY);
    chartTooltip.show({
      owner: el,
      key: mark.item.key,
      x: at.x,
      y: at.y,
      content: () =>
        tipContent({
          title: mark.item.label,
          rows: [{ label: state.seriesLabel ?? 'Valeur', value: state.format(mark.item.value), color: mark.item.fill, shape: 'rect' }],
          note: mark.item.hint,
        }),
    });
  }

  function hit(index, x, y, width, height) {
    const rect = svg('rect', { class: 'chart__hit', x, y, width: Math.max(0, width), height: Math.max(0, height) });
    rect.addEventListener('pointermove', (event) => activate(index, { x: event.clientX, y: event.clientY }));
    rect.addEventListener('pointerleave', deactivate);
    return rect;
  }

  function drawVertical({ plot, width, state }, rows) {
    const format = state.format;
    const tickFormat = state.tickFormat ?? format;
    const showAxis = state.axis ?? true;
    const height = state.height;
    const top = state.valueLabels ? 24 : 10;
    const bottom = 30;
    const values = rows.map((row) => row.value);
    const probe = buildAxis(values, [0, 1], { log: state.log, base: state.tickBase });
    const left = showAxis ? axisGutter(probe.ticks, tickFormat) : 0;
    const right = 2;
    const plotBottom = height - bottom;
    const axis = buildAxis(values, [plotBottom, top], { log: state.log, base: state.tickBase });
    const band = (width - left - right) / rows.length;
    const barWidth = Math.max(4, Math.min(state.barSize ?? 24, band * 0.6));
    const base = axis.scale(state.log ? axis.min : 0);
    const labelRoom = band - 6;
    const labelsFit = state.valueLabels && rows.every((row) => measureText(format(row.value), { mono: true }) <= labelRoom);

    root = svgRoot(root, plot, width, height);
    const bands = svg('g');
    const bars = svg('g');
    const labels = svg('g');
    const hits = svg('g');
    marks = rows.map((row, index) => {
      const cx = left + band * (index + 0.5);
      const raw = axis.scale(row.value);
      const up = row.value >= 0 || state.log;
      const size = Math.max(row.value === 0 ? 0 : MIN_BAR, Math.abs(base - raw));
      const y = up ? base - size : base;
      const bar = setBar(
        pool.take(row.key, () => svg('path')),
        roundedBar(cx - barWidth / 2, y, barWidth, size, RADIUS, up ? 'top' : 'bottom'),
        row.fill,
        index,
        false,
      );
      bars.append(bar);
      const bandRect = svg('rect', { class: 'chart__band', x: cx - band / 2 + 3, y: top - 14, width: band - 6, height: plotBottom - top + 14, rx: 6 });
      bands.append(bandRect);
      if (labelsFit) {
        labels.append(svg('text', { class: 'chart__value', x: cx, y: up ? y - 7 : y + size + 15, 'text-anchor': 'middle', style: `--i:${index}` }, format(row.value)));
      }
      const full = measureText(row.label, { size: 12 }) <= labelRoom;
      const name = full ? row.label : fitText(row.short ?? row.label, labelRoom, { size: 12 });
      labels.append(svg('text', { class: 'chart__category', x: cx, y: plotBottom + 19, 'text-anchor': 'middle' }, name));
      hits.append(hit(index, cx - band / 2, 0, band, height));
      return { item: row, bar, band: bandRect, tipX: cx + barWidth / 2, tipY: y };
    });
    pool.commit();

    clear(
      root,
      bands,
      showAxis
        ? valueAxis({ scale: axis.scale, ticks: axis.ticks, format: tickFormat, side: 'left', span: [left, width - right], at: left - 10, baseline: state.log ? axis.min : 0 })
        : svg('line', { class: 'chart__baseline', x1: left, x2: width - right, y1: Math.round(base) + 0.5, y2: Math.round(base) + 0.5 }),
      bars,
      labels,
      hits,
    );
  }

  function drawHorizontal({ plot, width, state }, rows) {
    const format = state.format;
    const tickFormat = state.tickFormat ?? format;
    const showAxis = state.axis ?? false;
    const rowHeight = state.rowHeight ?? 34;
    const thickness = Math.min(state.barSize ?? 12, rowHeight - 12);
    const top = 2;
    const bottom = showAxis ? 24 : 2;
    const height = top + rows.length * rowHeight + bottom;
    const labelMax = Math.min(width * 0.38, Math.max(...rows.map((row) => measureText(row.label, { size: 12 }))));
    const left = Math.ceil(labelMax) + 14;
    const valueMax = state.valueLabels ? Math.max(...rows.map((row) => measureText(format(row.value), { mono: true }))) : 0;
    const right = Math.ceil(valueMax) + (state.valueLabels ? 12 : 2);
    const values = rows.map((row) => row.value);
    const axis = buildAxis(values, [left, width - right], { log: state.log, base: state.tickBase, count: Math.max(2, Math.round((width - left - right) / 90)) });
    const base = axis.scale(state.log ? axis.min : 0);
    const plotBottom = top + rows.length * rowHeight;

    root = svgRoot(root, plot, width, height);
    const bands = svg('g');
    const tracks = svg('g');
    const bars = svg('g');
    const labels = svg('g');
    const hits = svg('g');
    marks = rows.map((row, index) => {
      const cy = top + rowHeight * (index + 0.5);
      const size = Math.max(row.value === 0 ? 0 : MIN_BAR, axis.scale(row.value) - base);
      const bar = setBar(
        pool.take(row.key, () => svg('path')),
        roundedBar(base, cy - thickness / 2, size, thickness, RADIUS, 'right'),
        row.fill,
        index,
        true,
      );
      bars.append(bar);
      const bandRect = svg('rect', { class: 'chart__band', x: 0, y: cy - rowHeight / 2 + 2, width, height: rowHeight - 4, rx: 6 });
      bands.append(bandRect);
      if (!showAxis) {
        tracks.append(svg('path', { class: 'chart__track', d: roundedBar(base, cy - thickness / 2, width - right - base, thickness, RADIUS, 'right') }));
      }
      const name = measureText(row.label, { size: 12 }) <= labelMax ? row.label : fitText(row.short ?? row.label, labelMax, { size: 12 });
      labels.append(svg('text', { class: 'chart__category', x: left - 14, y: cy, dy: '0.34em', 'text-anchor': 'end' }, name));
      if (state.valueLabels) {
        /* Avec un axe, la valeur suit le bout de la barre ; sans axe, elle s'aligne à droite de la piste. */
        const anchor = showAxis ? { x: base + size + 8 } : { x: width, 'text-anchor': 'end' };
        labels.append(svg('text', { class: 'chart__value', ...anchor, y: cy, dy: '0.34em', style: `--i:${index}` }, format(row.value)));
      }
      hits.append(hit(index, 0, cy - rowHeight / 2, width, rowHeight));
      return { item: row, bar, band: bandRect, tipX: base + size, tipY: cy + thickness / 2 };
    });
    pool.commit();

    clear(
      root,
      bands,
      showAxis ? valueAxis({ scale: axis.scale, ticks: axis.ticks, format: tickFormat, side: 'bottom', span: [top, plotBottom], at: plotBottom + 17, baseline: state.log ? axis.min : 0 }) : tracks,
      bars,
      labels,
      hits,
    );
  }

  const el = createChart(
    'bar',
    { data: [], orientation: 'vertical', height: 240, format: fmtNumber, log: false, sorted: false, valueLabels: true, ...props },
    {
      isEmpty: (state) => prepare(state).length === 0,
      draw(context) {
        const rows = prepare(context.state);
        if (context.state.orientation === 'horizontal') drawHorizontal(context, rows);
        else drawVertical(context, rows);
      },
      table: (state) =>
        dataTable(
          state.ariaLabel ?? 'Graphique en barres',
          ['Catégorie', state.seriesLabel ?? 'Valeur'],
          prepare(state).map((row) => [row.label, state.format(row.value)]),
        ),
      navigator: () => ({ count: marks.length, activate: (index) => activate(index), clear: deactivate }),
    },
  );
  return el;
}

/**
 * @typedef {Object} BarCategory
 * @property {string} id
 * @property {string} label
 * @property {string} [short] Libellé abrégé quand la place manque.
 * @property {'local'|'custom'|'grpc'|'rest'} [protocol] Couleur du groupe quand `colorBy: 'category'`.
 * @property {string} [color]
 */

/**
 * @typedef {Object} BarSeries
 * @property {string} id
 * @property {string} label
 * @property {number[]} values Une valeur par catégorie, dans le même ordre (`null` = absente).
 * @property {'local'|'custom'|'grpc'|'rest'} [protocol] Couleur de la série quand `colorBy: 'series'`.
 * @property {string} [color]
 */

const SHADES = [1, 0.5, 0.28, 0.16];

/**
 * Barres groupées : une grappe par catégorie, une barre par série. La légende est faite de
 * puces cliquables qui masquent ou affichent une série ; le survol d'une grappe liste toutes
 * ses valeurs.
 *
 *     GroupedBarChart({
 *       categories: [{ id: 'grpc', label: 'gRPC', protocol: 'grpc' }, …],
 *       series: [{ id: 'request', label: 'Requête', values: [17, …] }, { id: 'response', label: 'Réponse', values: [121, …] }],
 *       colorBy: 'category', format: fmtBytes,
 *     })
 *
 * @param {Object} [props]
 * @param {Array<BarCategory|string>} [props.categories]
 * @param {BarSeries[]} [props.series]
 * @param {'series'|'category'} [props.colorBy='series'] `series` : une couleur par série ;
 *   `category` : chaque grappe prend la couleur de sa catégorie (protocole) et les séries se
 *   distinguent par l'intensité (pleine, puis de plus en plus claire).
 * @param {(value: number) => string} [props.format=fmtNumber]
 * @param {(value: number) => string} [props.tickFormat]
 * @param {boolean} [props.log=false] Échelle logarithmique.
 * @param {10|1024} [props.tickBase=10] Base des graduations : 1024 avec `fmtBytes`.
 * @param {number} [props.height=260]
 * @param {number} [props.barSize=20] Épaisseur maximale d'une barre.
 * @param {boolean|'auto'} [props.valueLabels='auto'] Valeurs au-dessus des barres ; `auto` ne les écrit que si toutes tiennent.
 * @param {boolean} [props.monoLabels=false] Noms de catégories en chasse fixe (noms de procédures).
 * @param {string[]} [props.hidden] Identifiants des séries masquées.
 * @param {string} [props.ariaLabel]
 * @param {{icon?: string, title?: string, text?: string}} [props.empty]
 * @param {boolean} [props.loading]
 * @returns {HTMLElement & {update: (patch: Object) => void, setLoading: (on: boolean) => void, destroy: () => void}}
 */
export function GroupedBarChart(props = {}) {
  let root = null;
  let groups = [];
  const pool = elementPool();

  const categoriesOf = (state) => (state.categories ?? []).map((category) => (typeof category === 'string' ? { id: category, label: category } : category));

  function fillOf(state, category, series, seriesIndex) {
    if (state.colorBy === 'category') {
      const share = Math.round(SHADES[Math.min(seriesIndex, SHADES.length - 1)] * 100);
      const base = colorOf(category, 'var(--accent)');
      return share === 100 ? base : `color-mix(in srgb, ${base} ${share}%, transparent)`;
    }
    return seriesColor(series, seriesIndex);
  }

  function legendColor(state, series, index) {
    if (state.colorBy !== 'category') return seriesColor(series, index);
    const share = Math.round(SHADES[Math.min(index, SHADES.length - 1)] * 100);
    return `color-mix(in srgb, var(--fg-0) ${share}%, transparent)`;
  }

  function deactivate() {
    if (!root) return;
    root.dataset.hover = 'false';
    for (const group of groups) {
      group.band.removeAttribute('data-active');
      group.bars.forEach((bar) => bar.removeAttribute('data-active'));
    }
    chartTooltip.hide(el);
  }

  function activate(index, point) {
    const group = groups[index];
    if (!group) return;
    const state = el.state;
    root.dataset.hover = 'true';
    for (const other of groups) {
      const on = other === group;
      other.band.toggleAttribute('data-active', false);
      other.bars.forEach((bar) => bar.toggleAttribute('data-active', false));
      if (on) {
        other.band.setAttribute('data-active', 'true');
        other.bars.forEach((bar) => bar.setAttribute('data-active', 'true'));
      }
    }
    const at = point ?? clientPoint(root, group.tipX, group.tipY);
    chartTooltip.show({
      owner: el,
      key: group.category.id,
      x: at.x,
      y: at.y,
      content: () =>
        tipContent({
          title: group.category.label,
          rows: group.entries.map((entry) => ({ label: entry.series.label, value: state.format(entry.value), color: entry.fill, shape: 'rect' })),
        }),
    });
  }

  function draw({ plot, width, state }) {
    const categories = categoriesOf(state);
    const all = state.series ?? [];
    const visible = all.map((series, index) => ({ series, index })).filter(({ series }) => !state.hidden.includes(series.id));
    const format = state.format;
    const tickFormat = state.tickFormat ?? format;
    const height = state.height;
    const values = visible.flatMap(({ series }) => series.values.filter((value) => value !== null && Number.isFinite(Number(value))).map(Number));
    const probe = buildAxis(values, [0, 1], { log: state.log, base: state.tickBase });
    const left = axisGutter(probe.ticks, tickFormat);
    const right = 2;
    const gap = 2;
    const band = (width - left - right) / Math.max(1, categories.length);
    const count = Math.max(1, visible.length);
    const barWidth = Math.max(3, Math.min(state.barSize ?? 20, (band * 0.72 - gap * (count - 1)) / count));
    const groupWidth = barWidth * count + gap * (count - 1);
    const slot = barWidth + gap;
    const fits = values.every((value) => measureText(format(value), { mono: true }) <= slot - 2);
    const withLabels = state.valueLabels === true || (state.valueLabels === 'auto' && fits);
    const top = withLabels ? 24 : 10;
    const bottom = 30;
    const plotBottom = height - bottom;
    const axis = buildAxis(values, [plotBottom, top], { log: state.log, base: state.tickBase });
    const base = axis.scale(state.log ? axis.min : 0);
    const labelOptions = { size: state.monoLabels ? 11 : 12, mono: state.monoLabels };

    root = svgRoot(root, plot, width, height);
    const bands = svg('g');
    const bars = svg('g');
    const labels = svg('g');
    const hits = svg('g');
    groups = categories.map((category, categoryIndex) => {
      const cx = left + band * (categoryIndex + 0.5);
      const start = cx - groupWidth / 2;
      const bandRect = svg('rect', { class: 'chart__band', x: cx - band / 2 + 3, y: top - 14, width: band - 6, height: plotBottom - top + 14, rx: 6 });
      bands.append(bandRect);
      const entries = [];
      const groupBars = [];
      let tipY = base;
      visible.forEach(({ series, index: seriesIndex }, position) => {
        const raw = series.values[categoryIndex];
        const value = raw === null || raw === undefined ? NaN : Number(raw);
        if (!Number.isFinite(value)) return;
        const fill = fillOf(state, category, series, seriesIndex);
        const size = Math.max(value === 0 ? 0 : MIN_BAR, Math.abs(base - axis.scale(value)));
        const x = start + position * slot;
        const y = base - size;
        tipY = Math.min(tipY, y);
        const bar = setBar(
          pool.take(`${category.id}|${series.id}`, () => svg('path')),
          roundedBar(x, y, barWidth, size, Math.min(RADIUS, barWidth / 2), 'top'),
          fill,
          categoryIndex * count + position,
          false,
        );
        bars.append(bar);
        groupBars.push(bar);
        entries.push({ series, value, fill });
        if (withLabels) {
          labels.append(svg('text', { class: 'chart__value', x: x + barWidth / 2, y: y - 7, 'text-anchor': 'middle', style: `--i:${categoryIndex * count + position}` }, format(value)));
        }
      });
      const room = band - 8;
      const name = measureText(category.label, labelOptions) <= room ? category.label : fitText(category.short ?? category.label, room, labelOptions);
      labels.append(svg('text', { class: state.monoLabels ? 'chart__category chart__tick--num' : 'chart__category', x: cx, y: plotBottom + 19, 'text-anchor': 'middle' }, name));
      const rect = svg('rect', { class: 'chart__hit', x: cx - band / 2, y: 0, width: band, height });
      rect.addEventListener('pointermove', (event) => activate(categoryIndex, { x: event.clientX, y: event.clientY }));
      rect.addEventListener('pointerleave', deactivate);
      hits.append(rect);
      return { category, band: bandRect, bars: groupBars, entries, tipX: cx + groupWidth / 2, tipY };
    });
    pool.commit();

    clear(
      root,
      bands,
      valueAxis({ scale: axis.scale, ticks: axis.ticks, format: tickFormat, side: 'left', span: [left, width - right], at: left - 10, baseline: state.log ? axis.min : 0 }),
      bars,
      labels,
      hits,
    );
  }

  const el = createChart(
    'grouped',
    { categories: [], series: [], colorBy: 'series', height: 260, format: fmtNumber, log: false, valueLabels: 'auto', monoLabels: false, ...props },
    {
      isEmpty: (state) => !categoriesOf(state).length || !(state.series ?? []).some((series) => series.values?.some((value) => Number.isFinite(value))),
      legend: (state) => (state.series ?? []).map((series, index) => ({ id: series.id, label: series.label, color: legendColor(state, series, index) })),
      draw,
      table: (state) =>
        dataTable(
          state.ariaLabel ?? 'Barres groupées',
          ['Catégorie', ...(state.series ?? []).map((series) => series.label)],
          categoriesOf(state).map((category, index) => [category.label, ...(state.series ?? []).map((series) => state.format(series.values[index]))]),
        ),
      navigator: () => ({ count: groups.length, activate: (index) => activate(index), clear: deactivate }),
    },
  );
  return el;
}
