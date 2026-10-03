/**
 * Distributions : Histogram (effectifs par intervalle, marqueurs de percentiles) et RangeChart
 * (min · p50 · p95 · p99 · max par ligne, sur un axe partagé).
 */

import { svg, clear } from '../../core/dom.js';
import { fmtNumber, fmtPercent } from '../../core/format.js';
import {
  chartTooltip,
  clientPoint,
  createChart,
  dataTable,
  elementPool,
  linearScale,
  logScale,
  logScaleTicks,
  measureText,
  roundedBar,
  seriesColor,
  spreadLabels,
  svgRoot,
  ticks as niceTicks,
  tipContent,
  valueAxis,
} from './core.js';

const f = (n) => Math.round(n * 100) / 100;
const capitalize = (text) => text.charAt(0).toUpperCase() + text.slice(1);

/** Histogramme superposé : hauteur d'un rang de libellés de repères, et nombre maximal de rangs. */
const LABEL_LANE = 13;
const OVERLAY_LANES = 3;

function axisTicks(log, min, max, room) {
  if (log) return logScaleTicks(min, max).ticks.filter((tick) => tick >= min && tick <= max);
  return niceTicks(min, max, Math.max(2, Math.round(room / 90)));
}

function rowLabels(group, rows, centerOf, sublabelOf) {
  rows.forEach((row, index) => {
    const cy = centerOf(index);
    const sub = sublabelOf?.(row);
    group.append(
      svg('circle', { class: 'chart__key', cx: 4, cy: sub ? cy - 8 : cy, r: 3.5, style: `--c:${row.color}` }),
      svg('text', { class: 'chart__category', x: 14, y: sub ? cy - 8 : cy, dy: '0.34em' }, row.label),
      sub ? svg('text', { class: 'chart__tick chart__tick--num', x: 14, y: cy + 9, dy: '0.34em' }, sub) : null,
    );
  });
}

/**
 * @typedef {Object} HistogramSeries
 * @property {string} id
 * @property {string} label
 * @property {number[]} edges Bornes des intervalles, croissantes (une de plus que `counts`).
 * @property {number[]} counts Effectif de chaque intervalle.
 * @property {Record<string, number>} [markers] Repères verticaux nommés : `{ p50: 0.14, p95: 0.21, p99: 0.48 }`.
 * @property {'local'|'custom'|'grpc'|'rest'} [protocol]
 * @property {string} [color]
 */

/**
 * Distribution des latences : une ligne par série sur un axe commun (`multiples`, par défaut)
 * ou séries superposées en marches d'escalier (`overlay`). Les hauteurs sont normalisées par
 * série — on compare des formes et des positions, pas des effectifs absolus.
 *
 *     Histogram({ format: fmtMs, xScale: 'log', series: [{ id: 'grpc', label: 'gRPC', protocol: 'grpc',
 *       edges: h.edges_ms, counts: h.counts, markers: { p50: r.median_ms, p95: r.p95_ms, p99: r.p99_ms } }] })
 *
 * @param {Object} [props]
 * @param {HistogramSeries[]} [props.series]
 * @param {'multiples'|'overlay'} [props.layout='multiples']
 * @param {'linear'|'log'} [props.xScale='linear'] `log` étale la longue traîne des latences.
 * @param {(value: number) => string} [props.format=fmtNumber] Format des valeurs de l'axe.
 * @param {[number, number]} [props.domain] Force l'étendue de l'axe (par défaut celle des intervalles).
 * @param {string[]} [props.markers] Noms des repères à tracer (tous en `multiples`, `['p50']` en `overlay`). En
 *   `overlay`, les libellés de même nom trop proches fusionnent et les autres s'étagent : ils ne se chevauchent jamais.
 * @param {number} [props.rowHeight=92] Hauteur d'une ligne (`multiples`).
 * @param {number} [props.height=220] Hauteur totale (`overlay`).
 * @param {string} [props.countLabel='appels'] Unité des effectifs (« 1 000 appels »).
 * @param {string[]} [props.hidden] Séries masquées (`overlay` : la légende les bascule).
 * @param {string} [props.ariaLabel]
 * @param {{icon?: string, title?: string, text?: string}} [props.empty]
 * @param {boolean} [props.loading]
 * @returns {HTMLElement & {update: (patch: Object) => void, setLoading: (on: boolean) => void, destroy: () => void}}
 */
export function Histogram(props = {}) {
  let root = null;
  let active = null;
  const pool = elementPool();

  function prepare(state) {
    return (state.series ?? [])
      .map((series, index) => {
        const counts = (series.counts ?? []).map((count) => Number(count) || 0);
        return {
          ...series,
          counts,
          edges: (series.edges ?? []).slice(0, counts.length + 1),
          color: seriesColor(series, index),
          total: counts.reduce((sum, count) => sum + count, 0),
          peak: Math.max(0, ...counts),
        };
      })
      .filter((series) => series.edges.length === series.counts.length + 1 && series.total > 0);
  }

  function markersOf(series, state, fallback) {
    const names = state.markers ?? fallback ?? Object.keys(series.markers ?? {});
    return names
      .filter((name) => Number.isFinite(series.markers?.[name]))
      .map((name) => ({ name, value: series.markers[name] }))
      .sort((a, b) => a.value - b.value);
  }

  function scaleOf(state, rows, range) {
    const log = state.xScale === 'log';
    const firsts = rows.map((row) => (log ? row.edges.find((edge) => edge > 0) : row.edges[0]));
    const min = state.domain?.[0] ?? Math.min(...firsts);
    const max = state.domain?.[1] ?? Math.max(...rows.map((row) => row.edges[row.edges.length - 1]));
    return { x: log ? logScale([min, max], range) : linearScale([min, max], range), min, max, log };
  }

  function binAt(row, value) {
    for (let i = 0; i < row.counts.length; i += 1) if (value >= row.edges[i] && value < row.edges[i + 1]) return i;
    return -1;
  }

  function binRows(row, index, state) {
    return [
      { label: 'Intervalle', value: `${state.format(row.edges[index])} – ${state.format(row.edges[index + 1])}` },
      { label: capitalize(state.countLabel), value: fmtNumber(row.counts[index]), color: row.color, shape: 'rect' },
      { label: 'Part', value: fmtPercent(row.counts[index] / row.total), muted: true },
    ];
  }

  function percentileNote(row, state) {
    const entries = markersOf(row, { ...state, markers: undefined });
    return entries.length ? entries.map((entry) => `${entry.name} ${state.format(entry.value)}`).join(' · ') : undefined;
  }

  function clearActive() {
    active?.removeAttribute('data-active');
    active = null;
    chartTooltip.hide(el);
  }

  function markerLayer(entries, x, top, bottom, bounds) {
    const group = svg('g', { class: 'chart__fade' });
    const labels = entries.map((entry) => ({ ...entry, center: x(entry.value), width: measureText(entry.name, { weight: 500 }) }));
    const lefts = spreadLabels(labels, bounds[0], bounds[1], 5);
    labels.forEach((label, index) => {
      const px = Math.round(label.center) + 0.5;
      group.append(
        svg('line', {
          class: index === 0 ? 'chart__marker chart__marker--strong' : 'chart__marker',
          x1: px,
          x2: px,
          y1: top + 15,
          y2: bottom,
        }),
        svg('text', { class: index === 0 ? 'chart__marker-label chart__marker-label--strong' : 'chart__marker-label', x: f(lefts[index]), y: top + 10 }, label.name),
      );
    });
    return group;
  }

  /**
   * Repères de plusieurs séries superposées : les libellés de même nom trop proches pour tenir côte à
   * côte fusionnent en un seul, centré sur leurs traits ; les autres s'étagent sur plusieurs rangs.
   * @returns {{labels: Array<{name: string, left: number, lane: number, strong: boolean, members: Array<{px: number, color: string}>}>, lanes: number}}
   */
  function overlayLabels(rows, state, x, bounds) {
    const gap = 6;
    const entries = rows
      .flatMap((row) => markersOf(row, state, ['p50']).map((marker, index) => ({ name: marker.name, center: x(marker.value), color: row.color, strong: index === 0 })))
      .sort((a, b) => a.center - b.center);
    const merged = [];
    for (const entry of entries) {
      const width = measureText(entry.name, { weight: 500 });
      const near = merged.findLast((label) => label.name === entry.name);
      if (near && entry.center - near.to < width + gap) {
        near.to = entry.center;
        near.strong ||= entry.strong;
        near.members.push(entry);
      } else {
        merged.push({ name: entry.name, width, from: entry.center, to: entry.center, strong: entry.strong, members: [entry] });
      }
    }
    const edges = [];
    const labels = merged.map((label) => {
      const ideal = Math.max(bounds[0], Math.min((label.from + label.to) / 2 - label.width / 2, bounds[1] - label.width));
      let lane = edges.findIndex((edge) => ideal >= edge + gap);
      if (lane < 0) lane = edges.length < OVERLAY_LANES ? edges.length : edges.indexOf(Math.min(...edges));
      const left = Math.max(ideal, (edges[lane] ?? -Infinity) + gap);
      edges[lane] = left + label.width;
      return { name: label.name, left, lane, strong: label.strong, members: label.members.map((member) => ({ px: Math.round(member.center) + 0.5, color: member.color })) };
    });
    return { labels, lanes: Math.max(1, edges.length) };
  }

  function drawMultiples({ plot, width, state }, rows) {
    const rowHeight = state.rowHeight ?? 92;
    const subOf = (row) => `${fmtNumber(row.total)} ${state.countLabel}`;
    const left = Math.ceil(Math.max(...rows.map((row) => Math.max(measureText(row.label, { size: 12 }), measureText(subOf(row), { mono: true }))))) + 30;
    const probe = scaleOf(state, rows, [0, 1]);
    const right = Math.max(8, Math.ceil(measureText(state.format(probe.max), { mono: true }) / 2) + 2);
    const { x, min, max, log } = scaleOf(state, rows, [left, width - right]);
    const height = rows.length * rowHeight + 24;
    const plotBottom = rows.length * rowHeight - 10;

    root = svgRoot(root, plot, width, height);
    const bars = svg('g');
    const overlays = svg('g');
    const labels = svg('g');
    const hits = svg('g');
    rowLabels(labels, rows, (index) => index * rowHeight + 18 + (rowHeight - 28) / 2, subOf);

    rows.forEach((row, rowIndex) => {
      const top = rowIndex * rowHeight;
      const barsTop = top + 22;
      const bottom = top + rowHeight - 10;
      const y = linearScale([0, row.peak], [bottom, barsTop]);
      const rowBars = row.counts.map((count, index) => {
        const x0 = Math.max(left, x(row.edges[index]));
        const x1 = Math.min(width - right, x(row.edges[index + 1]));
        const span = Math.max(0, x1 - x0);
        const gap = span >= 6 ? 2 : span >= 3 ? 1 : 0;
        const size = count > 0 ? Math.max(1.5, bottom - y(count)) : 0;
        const bar = pool.take(`${row.id}|${index}`, () => svg('path'));
        bar.setAttribute('class', 'chart__bar chart__bar--bin');
        bar.setAttribute('d', roundedBar(x0 + gap / 2, bottom - size, Math.max(0, span - gap), size, 2, 'top'));
        bar.style.setProperty('--c', row.color);
        bar.style.setProperty('--i', String(Math.round((index / row.counts.length) * 10) + rowIndex * 2));
        bar.removeAttribute('data-active');
        bars.append(bar);
        return bar;
      });
      overlays.append(
        svg('line', { class: 'chart__baseline', x1: left, x2: width - right, y1: bottom + 0.5, y2: bottom + 0.5 }),
        markerLayer(markersOf(row, state), x, top, bottom, [left, width - right]),
      );
      const hit = svg('rect', { class: 'chart__hit', x: left, y: top, width: Math.max(0, width - left), height: rowHeight });
      hit.addEventListener('pointermove', (event) => {
        const index = binAt(row, x.invert(event.clientX - root.getBoundingClientRect().left));
        if (index < 0) {
          clearActive();
          return;
        }
        if (active !== rowBars[index]) {
          active?.removeAttribute('data-active');
          active = rowBars[index];
          active.setAttribute('data-active', 'true');
        }
        chartTooltip.show({
          owner: el,
          key: `${row.id}|${index}`,
          x: event.clientX,
          y: event.clientY,
          content: () => tipContent({ title: row.label, rows: binRows(row, index, state), note: percentileNote(row, state) }),
        });
      });
      hit.addEventListener('pointerleave', clearActive);
      hits.append(hit);
    });
    pool.commit();

    clear(
      root,
      valueAxis({ scale: x, ticks: axisTicks(log, min, max, width - left - right), format: state.format, side: 'bottom', span: [0, plotBottom], at: plotBottom + 18 }),
      bars,
      overlays,
      labels,
      hits,
    );
  }

  function drawOverlay({ plot, width, state }, rows) {
    const height = state.height ?? 220;
    const top = 4;
    const bottom = height - 26;
    const probe = scaleOf(state, rows, [0, 1]);
    const right = Math.max(8, Math.ceil(measureText(state.format(probe.max), { mono: true }) / 2) + 2);
    const left = Math.max(8, Math.ceil(measureText(state.format(probe.min), { mono: true }) / 2) + 2);
    const { x, min, max, log } = scaleOf(state, rows, [left, width - right]);
    const marks = overlayLabels(rows, state, x, [left, width - right]);
    // Chaque rang de libellés supplémentaire abaisse d'autant le haut du tracé.
    const head = top + (marks.lanes - 1) * LABEL_LANE;

    root = svgRoot(root, plot, width, height);
    const shapes = svg('g');
    const markers = svg('g', { class: 'chart__fade' });
    rows.forEach((row, order) => {
      const y = linearScale([0, row.peak], [bottom, head + 22]);
      let d = `M${f(x(row.edges[0]))},${bottom}`;
      row.counts.forEach((count, index) => {
        d += `V${f(y(count))}H${f(x(row.edges[index + 1]))}`;
      });
      d += `V${bottom}`;
      shapes.append(
        svg('path', { class: 'chart__step-area', d: `${d}Z`, style: `--c:${row.color};--i:${order}` }),
        svg('path', { class: 'chart__step', d, style: `--c:${row.color};--i:${order}` }),
      );
    });
    for (const label of marks.labels) {
      const labelTop = top + label.lane * LABEL_LANE;
      for (const member of label.members) {
        markers.append(svg('line', { class: label.strong ? 'chart__marker chart__marker--strong' : 'chart__marker', x1: member.px, x2: member.px, y1: labelTop + 15, y2: bottom, style: `--c:${member.color}` }));
      }
      // Un libellé étagé peut croiser le trait d'un repère voisin : son halo le garde lisible.
      const classes = ['chart__marker-label', label.strong ? 'chart__marker-label--strong' : '', label.lane > 0 ? 'chart__marker-label--halo' : ''];
      markers.append(svg('text', { class: classes.filter(Boolean).join(' '), x: f(label.left), y: labelTop + 10 }, label.name));
    }

    const cross = svg('line', { class: 'chart__cross', y1: head + 16, y2: bottom, visibility: 'hidden' });
    const hit = svg('rect', { class: 'chart__hit', x: left, y: 0, width: Math.max(0, width - left - right), height: bottom });
    hit.addEventListener('pointermove', (event) => {
      const px = event.clientX - root.getBoundingClientRect().left;
      const value = x.invert(px);
      cross.setAttribute('x1', String(Math.round(px) + 0.5));
      cross.setAttribute('x2', String(Math.round(px) + 0.5));
      cross.setAttribute('visibility', 'visible');
      chartTooltip.show({
        owner: el,
        key: rows.map((row) => binAt(row, value)).join('|'),
        x: event.clientX,
        y: event.clientY,
        content: () =>
          tipContent({
            title: state.format(value),
            rows: rows.map((row) => {
              const index = binAt(row, value);
              return { label: row.label, value: index < 0 ? '—' : fmtPercent(row.counts[index] / row.total), color: row.color, shape: 'rect' };
            }),
            note: 'Part des appels dans l’intervalle survolé',
          }),
      });
    });
    hit.addEventListener('pointerleave', () => {
      cross.setAttribute('visibility', 'hidden');
      chartTooltip.hide(el);
    });

    clear(
      root,
      valueAxis({ scale: x, ticks: axisTicks(log, min, max, width - left - right), format: state.format, side: 'bottom', span: [head + 16, bottom], at: bottom + 18 }),
      svg('line', { class: 'chart__baseline', x1: left, x2: width - right, y1: bottom + 0.5, y2: bottom + 0.5 }),
      shapes,
      markers,
      cross,
      hit,
    );
  }

  const el = createChart(
    'histogram',
    { series: [], layout: 'multiples', xScale: 'linear', format: fmtNumber, countLabel: 'appels', ...props },
    {
      isEmpty: (state) => prepare(state).length === 0,
      legend: (state) => (state.layout === 'overlay' && prepare(state).length > 1 ? prepare(state).map((row) => ({ id: row.id, label: row.label, color: row.color })) : null),
      draw(context) {
        const { state } = context;
        active = null;
        if (state.layout === 'overlay') {
          const rows = prepare(state).filter((row) => !state.hidden.includes(row.id));
          drawOverlay(context, rows);
        } else {
          drawMultiples(context, prepare(state));
        }
      },
      table: (state) =>
        dataTable(
          state.ariaLabel ?? 'Distribution',
          ['Série', 'Effectif', ...Object.keys(prepare(state)[0]?.markers ?? {})],
          prepare(state).map((row) => [row.label, fmtNumber(row.total), ...Object.values(row.markers ?? {}).map((value) => state.format(value))]),
        ),
    },
  );
  return el;
}

/**
 * @typedef {Object} RangeRow
 * @property {string} id
 * @property {string} label
 * @property {number} min
 * @property {number} p50 Médiane.
 * @property {number} p95
 * @property {number} p99
 * @property {number} max
 * @property {number} [mean] Moyenne (info-bulle seulement).
 * @property {'local'|'custom'|'grpc'|'rest'} [protocol]
 * @property {string} [color]
 */

const RANGE_KEYS = [
  ['min', 'Minimum'],
  ['p50', 'Médiane (p50)'],
  ['p95', 'p95'],
  ['p99', 'p99'],
  ['max', 'Maximum'],
];

/**
 * Étendue des latences, une ligne par protocole sur un axe partagé : trait fin du minimum au
 * maximum, bande pleine de la médiane au p95, bande claire jusqu'au p99, point sur la médiane.
 * Une alternative lisible à la boîte à moustaches.
 *
 *     RangeChart({ format: fmtMs, scale: 'log', rows: [{ id: 'grpc', label: 'gRPC', protocol: 'grpc',
 *       min: r.min_ms, p50: r.median_ms, p95: r.p95_ms, p99: r.p99_ms, max: r.max_ms }] })
 *
 * @param {Object} [props]
 * @param {RangeRow[]} [props.rows]
 * @param {'linear'|'log'} [props.scale='linear']
 * @param {(value: number) => string} [props.format=fmtNumber]
 * @param {number} [props.rowHeight=46]
 * @param {boolean} [props.valueLabels=true] Écrit la médiane et le p99 au-dessus de chaque ligne.
 * @param {string} [props.ariaLabel]
 * @param {{icon?: string, title?: string, text?: string}} [props.empty]
 * @param {boolean} [props.loading]
 * @returns {HTMLElement & {update: (patch: Object) => void, setLoading: (on: boolean) => void, destroy: () => void}}
 */
export function RangeChart(props = {}) {
  let root = null;
  let marks = [];

  function prepare(state) {
    return (state.rows ?? [])
      .filter((row) => RANGE_KEYS.every(([key]) => Number.isFinite(Number(row[key]))))
      .map((row, index) => ({ ...row, color: seriesColor(row, index) }));
  }

  function deactivate() {
    marks.forEach((mark) => mark.band.removeAttribute('data-active'));
    chartTooltip.hide(el);
  }

  function activate(index, point) {
    const mark = marks[index];
    if (!mark) return;
    const state = el.state;
    marks.forEach((other) => other.band.removeAttribute('data-active'));
    mark.band.setAttribute('data-active', 'true');
    const at = point ?? clientPoint(root, mark.tipX, mark.tipY);
    chartTooltip.show({
      owner: el,
      key: mark.row.id,
      x: at.x,
      y: at.y,
      content: () =>
        tipContent({
          title: mark.row.label,
          rows: [
            ...RANGE_KEYS.map(([key, label]) => ({ label, value: state.format(mark.row[key]), color: key === 'p50' ? mark.row.color : undefined, shape: 'dot' })),
            ...(Number.isFinite(mark.row.mean) ? [{ label: 'Moyenne', value: state.format(mark.row.mean), muted: true }] : []),
          ],
        }),
    });
  }

  function draw({ plot, width, state }) {
    const rows = prepare(state);
    const log = state.scale === 'log';
    const rowHeight = state.rowHeight ?? 46;
    const left = Math.ceil(Math.max(...rows.map((row) => measureText(row.label, { size: 12 })))) + 30;
    const values = rows.flatMap((row) => RANGE_KEYS.map(([key]) => Number(row[key])));
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const bounds = log ? { min: lo / 1.7, max: hi * 1.7 } : { min: Math.min(0, lo), max: hi + (hi - Math.min(0, lo)) * 0.04 };
    const right = Math.max(12, Math.ceil(measureText(state.format(bounds.max), { mono: true }) / 2) + 2);
    const range = [left, width - right];
    const x = log ? logScale([bounds.min, bounds.max], range) : linearScale([bounds.min, bounds.max], range);
    const top = 6;
    const plotBottom = top + rows.length * rowHeight;
    const height = plotBottom + 24;

    root = svgRoot(root, plot, width, height);
    const bands = svg('g');
    const glyphs = svg('g');
    const labels = svg('g');
    const hits = svg('g');
    rowLabels(labels, rows, (index) => top + rowHeight * (index + 0.5) + 5);

    marks = rows.map((row, index) => {
      const cy = top + rowHeight * (index + 0.5) + 5;
      const px = Object.fromEntries(RANGE_KEYS.map(([key]) => [key, x(Number(row[key]))]));
      const band = svg('rect', { class: 'chart__band', x: 0, y: top + rowHeight * index + 2, width, height: rowHeight - 4, rx: 6 });
      bands.append(band);
      glyphs.append(
        svg(
          'g',
          { class: 'chart__range', style: `--c:${row.color};--i:${index}` },
          svg('line', { class: 'chart__whisker', x1: f(px.min), x2: f(px.max), y1: cy + 0.5, y2: cy + 0.5 }),
          svg('line', { class: 'chart__whisker', x1: Math.round(px.min) + 0.5, x2: Math.round(px.min) + 0.5, y1: cy - 4, y2: cy + 5 }),
          svg('line', { class: 'chart__whisker', x1: Math.round(px.max) + 0.5, x2: Math.round(px.max) + 0.5, y1: cy - 4, y2: cy + 5 }),
          svg('rect', { class: 'chart__range-tail', x: f(px.p95), y: cy - 4, width: f(Math.max(0, px.p99 - px.p95)), height: 9, rx: 2 }),
          svg('rect', { class: 'chart__range-core', x: f(px.p50), y: cy - 4, width: f(Math.max(2, px.p95 - px.p50)), height: 9, rx: 2 }),
          svg('circle', { class: 'chart__dot chart__dot--median', cx: f(px.p50), cy: cy + 0.5, r: 5 }),
        ),
      );
      if (state.valueLabels) {
        const texts = [
          { key: 'p50', text: state.format(row.p50), center: px.p50 },
          { key: 'p99', text: `p99 ${state.format(row.p99)}`, center: px.p99 },
        ].map((label) => ({ ...label, width: measureText(label.text, { mono: true }) }));
        const overlap = texts[1].center - texts[1].width / 2 < texts[0].center + texts[0].width / 2 + 8;
        const shown = overlap ? [texts[0]] : texts;
        const lefts = spreadLabels(shown, left - 4, width - 2, 8);
        shown.forEach((label, order) => {
          labels.append(
            svg('text', { class: label.key === 'p50' ? 'chart__value' : 'chart__tick chart__tick--num', x: f(lefts[order]), y: cy - 12, style: `--i:${index}` }, label.text),
          );
        });
      }
      const hit = svg('rect', { class: 'chart__hit', x: 0, y: top + rowHeight * index, width, height: rowHeight });
      hit.addEventListener('pointermove', (event) => activate(index, { x: event.clientX, y: event.clientY }));
      hit.addEventListener('pointerleave', deactivate);
      hits.append(hit);
      return { row, band, tipX: px.p95, tipY: cy + 8 };
    });

    clear(
      root,
      bands,
      valueAxis({
        scale: x,
        ticks: axisTicks(log, bounds.min, bounds.max, range[1] - range[0]),
        format: state.format,
        side: 'bottom',
        span: [top, plotBottom],
        at: plotBottom + 18,
      }),
      glyphs,
      labels,
      hits,
    );
  }

  const el = createChart(
    'range',
    { rows: [], scale: 'linear', format: fmtNumber, valueLabels: true, ...props },
    {
      isEmpty: (state) => prepare(state).length === 0,
      staticLegend: true,
      legend: () => [
        { id: 'span', label: 'min – max', shape: 'line', color: 'var(--chart-axis)' },
        { id: 'p50', label: 'médiane', shape: 'dot', color: 'var(--fg-1)' },
        { id: 'core', label: 'p50 → p95', shape: 'rect', color: 'color-mix(in srgb, var(--fg-1) 70%, transparent)' },
        { id: 'tail', label: 'p95 → p99', shape: 'rect', color: 'color-mix(in srgb, var(--fg-1) 28%, transparent)' },
      ],
      draw,
      table: (state) =>
        dataTable(
          state.ariaLabel ?? 'Étendue des latences',
          ['Série', ...RANGE_KEYS.map(([, label]) => label)],
          prepare(state).map((row) => [row.label, ...RANGE_KEYS.map(([key]) => state.format(row[key]))]),
        ),
      navigator: () => ({ count: marks.length, activate: (index) => activate(index), clear: deactivate }),
    },
  );
  return el;
}
