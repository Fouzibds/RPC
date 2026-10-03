/**
 * LineChart : courbes multi-séries, aire facultative, réticule + info-bulle listant toutes les
 * séries, échelles linéaires ou logarithmiques, ajout de points en direct.
 */

import { svg, clear, uid } from '../../core/dom.js';
import { fmtNumber } from '../../core/format.js';
import {
  axisGutter,
  buildAxis,
  chartTooltip,
  clientPoint,
  createChart,
  dataTable,
  linearScale,
  logScale,
  logScaleTicks,
  measureText,
  prefersReducedMotion,
  seriesColor,
  svgRoot,
  ticks as niceTicks,
  tidyTickLabels,
  tipContent,
  valueAxis,
} from './core.js';

const f = (n) => Math.round(n * 100) / 100;

function linearPath(points) {
  return points.map(([x, y], index) => `${index ? 'L' : 'M'}${f(x)},${f(y)}`).join('');
}

/** Interpolation cubique monotone (Fritsch–Carlson) : lisse sans jamais dépasser les points. */
function monotonePath(points) {
  const n = points.length;
  if (n < 3) return linearPath(points);
  const slopes = [];
  for (let i = 0; i < n - 1; i += 1) slopes.push((points[i + 1][1] - points[i][1]) / (points[i + 1][0] - points[i][0] || 1));
  const tangents = [slopes[0]];
  for (let i = 1; i < n - 1; i += 1) tangents.push(slopes[i - 1] * slopes[i] <= 0 ? 0 : (slopes[i - 1] + slopes[i]) / 2);
  tangents.push(slopes[n - 2]);
  for (let i = 0; i < n - 1; i += 1) {
    if (slopes[i] === 0) {
      tangents[i] = 0;
      tangents[i + 1] = 0;
    } else {
      const a = tangents[i] / slopes[i];
      const b = tangents[i + 1] / slopes[i];
      const sum = a * a + b * b;
      if (sum > 9) {
        const tau = 3 / Math.sqrt(sum);
        tangents[i] = tau * a * slopes[i];
        tangents[i + 1] = tau * b * slopes[i];
      }
    }
  }
  let d = `M${f(points[0][0])},${f(points[0][1])}`;
  for (let i = 0; i < n - 1; i += 1) {
    const [x0, y0] = points[i];
    const [x1, y1] = points[i + 1];
    const third = (x1 - x0) / 3;
    d += `C${f(x0 + third)},${f(y0 + tangents[i] * third)} ${f(x1 - third)},${f(y1 - tangents[i + 1] * third)} ${f(x1)},${f(y1)}`;
  }
  return d;
}

function normalize(series) {
  return (series.points ?? [])
    .map((point) => (Array.isArray(point) ? [Number(point[0]), Number(point[1])] : [Number(point.x), Number(point.y)]))
    .filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y))
    .sort((a, b) => a[0] - b[0]);
}

/**
 * @typedef {Object} LineSeries
 * @property {string} id
 * @property {string} label
 * @property {Array<[number, number]|{x: number, y: number}>} points Points `(x, y)`.
 * @property {'local'|'custom'|'grpc'|'rest'} [protocol] Couleur du protocole.
 * @property {string} [color] Couleur CSS explicite.
 * @property {boolean} [area] Remplit l'aire sous cette courbe (sinon suit `props.area`).
 * @property {boolean} [dashed] Trait pointillé (référence, projection).
 */

/**
 * Courbes multi-séries sur un axe commun. Le réticule s'accroche à l'abscisse la plus proche
 * et l'info-bulle liste la valeur de chaque série ; les flèches du clavier le déplacent.
 *
 *     const chart = LineChart({ series: [{ id: 'grpc', label: 'gRPC', protocol: 'grpc', points: [[0, 0.3], [50, 51]] }],
 *       xFormat: fmtMs, yFormat: fmtMs, xLabel: 'Latence réseau ajoutée' });
 *     chart.push(Date.now(), { grpc: 0.31 });   // direct : ajoute un point et fait glisser la fenêtre
 *
 * @param {Object} [props]
 * @param {LineSeries[]} [props.series]
 * @param {'linear'|'log'} [props.xScale='linear']
 * @param {'linear'|'log'} [props.yScale='linear']
 * @param {(x: number) => string} [props.xFormat=fmtNumber] Format des abscisses (`fmtTime` pour un temps réel).
 * @param {(y: number) => string} [props.yFormat=fmtNumber] Format des valeurs (info-bulle).
 * @param {(y: number) => string} [props.yTickFormat] Format des graduations verticales (par défaut `yFormat`).
 * @param {10|1024} [props.yTickBase=10] Base des graduations verticales : 1024 avec `fmtBytes` (« 1 ko », « 10 ko », « 1 Mo »).
 * @param {number[]|'data'|'auto'} [props.xTicks='auto'] Graduations horizontales : liste, abscisses des
 *   données, ou `auto` (les abscisses des données si elles sont 8 ou moins, sinon des valeurs rondes).
 * @param {string} [props.xLabel] Titre de l'axe horizontal.
 * @param {string} [props.yLabel] Titre de l'axe vertical (écrit au-dessus de l'axe).
 * @param {boolean} [props.area=false] Aire dégradée sous chaque courbe.
 * @param {boolean|'auto'} [props.points='auto'] Marque chaque point (`auto` : jusqu'à 24 points par série).
 * @param {'linear'|'monotone'} [props.curve='linear'] `monotone` lisse la courbe sans dépasser les mesures.
 * @param {boolean} [props.yZero=true] Inclut zéro dans l'axe vertical (échelle linéaire).
 * @param {number} [props.yMin]
 * @param {number} [props.yMax]
 * @param {number} [props.height=260]
 * @param {number} [props.maxPoints=120] Taille de la fenêtre glissante alimentée par `push`.
 * @param {number} [props.slideMs=320] Durée du glissement après un `push` (à caler sur la cadence des points).
 * @param {string[]} [props.hidden] Identifiants des séries masquées (la légende les bascule).
 * @param {string} [props.ariaLabel]
 * @param {{icon?: string, title?: string, text?: string}} [props.empty]
 * @param {boolean} [props.loading]
 * @returns {HTMLElement & {update: (patch: Object) => void, push: (x: number, values: Record<string, number>) => void, setLoading: (on: boolean) => void, destroy: () => void}}
 *   `push(x, { idSérie: y })` ajoute un point à chaque série citée, retire les plus anciens au-delà de
 *   `maxPoints` et fait glisser les courbes.
 */
export function LineChart(props = {}) {
  const clipId = uid('chart-clip');
  let root = null;
  let layout = null;
  let sliding = false;
  let activeIndex = -1;
  let pointer = null;
  let layers = null;

  function visibleSeries(state) {
    return (state.series ?? [])
      .map((series, index) => ({ series, index, color: seriesColor(series, index), points: normalize(series) }))
      .filter((entry) => !state.hidden.includes(entry.series.id) && entry.points.length);
  }

  function deactivate() {
    activeIndex = -1;
    if (layers) layers.cross.setAttribute('visibility', 'hidden');
    chartTooltip.hide(el);
  }

  function activate(index, point) {
    if (!layout || !layout.xs.length) return;
    const state = el.state;
    activeIndex = Math.max(0, Math.min(index, layout.xs.length - 1));
    const x = layout.xs[activeIndex];
    const px = layout.x(x);
    const rows = [];
    let topY = layout.bottom;
    clear(layers.dots);
    for (const entry of layout.entries) {
      const hit = entry.points.find((candidate) => candidate[0] === x);
      if (!hit) continue;
      const py = layout.y(hit[1]);
      topY = Math.min(topY, py);
      layers.dots.append(svg('circle', { class: 'chart__dot chart__dot--active', cx: f(px), cy: f(py), r: 4, style: `--c:${entry.color}` }));
      rows.push({ label: entry.series.label, value: state.yFormat(hit[1]), color: entry.color, shape: 'line' });
    }
    layers.line.setAttribute('x1', String(Math.round(px) + 0.5));
    layers.line.setAttribute('x2', String(Math.round(px) + 0.5));
    layers.cross.setAttribute('visibility', 'visible');
    const at = point ?? clientPoint(root, px, topY);
    chartTooltip.show({
      owner: el,
      key: `${x}|${rows.length}`,
      x: at.x,
      y: at.y,
      content: () => tipContent({ title: state.xFormat(x), subtitle: state.xLabel, rows }),
    });
  }

  function nearestIndex(pixel) {
    const { xs, x } = layout;
    let low = 0;
    let high = xs.length - 1;
    while (high - low > 1) {
      const mid = (low + high) >> 1;
      if (x(xs[mid]) < pixel) low = mid;
      else high = mid;
    }
    return Math.abs(x(xs[low]) - pixel) <= Math.abs(x(xs[high]) - pixel) ? low : high;
  }

  function xAxisTicks(state, xs, min, max, innerWidth) {
    if (Array.isArray(state.xTicks)) return state.xTicks.filter((tick) => tick >= min && tick <= max);
    if (state.xTicks === 'data' || (state.xTicks === 'auto' && xs.length <= 8)) return xs;
    if (state.xScale === 'log') return logScaleTicks(min, max).ticks.filter((tick) => tick >= min && tick <= max);
    return niceTicks(min, max, Math.max(2, Math.round(innerWidth / 96)));
  }

  function draw({ plot, width, state }) {
    const entries = visibleSeries(state);
    const height = state.height;
    const yTickFormat = state.yTickFormat ?? state.yFormat;
    const xs = Array.from(new Set(entries.flatMap((entry) => entry.points.map((point) => point[0])))).sort((a, b) => a - b);
    const ys = entries.flatMap((entry) => entry.points.map((point) => point[1]));
    const logY = state.yScale === 'log';
    const logX = state.xScale === 'log';

    const top = state.yLabel ? 26 : 10;
    const bottom = state.xLabel ? 44 : 26;
    const plotBottom = height - bottom;
    const yAxis = buildAxis(ys, [plotBottom, top], { log: logY, zero: state.yZero, min: state.yMin, max: state.yMax, base: state.yTickBase });
    const left = axisGutter(yAxis.ticks, yTickFormat);
    let minX = xs.length ? xs[0] : 0;
    let maxX = xs.length ? xs[xs.length - 1] : 1;
    if (minX === maxX) {
      minX -= 1;
      maxX += 1;
    }
    const lastLabel = measureText(state.xFormat(maxX), { mono: true });
    const right = Math.max(12, Math.ceil(lastLabel / 2) + 2);
    const range = [left + 6, width - right];
    const x = logX ? logScale([Math.max(minX, Number.MIN_VALUE), maxX], range) : linearScale([minX, maxX], range);
    const y = yAxis.scale;

    const previous = layout;
    layout = { entries, xs, x, y, bottom: plotBottom, top, left, right: width - right };
    root = svgRoot(root, plot, width, height);

    // Deux découpes : le tracé reste dans la zone de données ; les libellés de l'axe horizontal s'arrêtent
    // au bord droit du graphique (en direct, la graduation qui entre glisse depuis l'extérieur).
    const defs = svg(
      'defs',
      svg('clipPath', { id: clipId }, svg('rect', { x: left, y: 0, width: Math.max(0, width - left), height })),
      svg('clipPath', { id: `${clipId}-axis` }, svg('rect', { x: -width, y: plotBottom, width: width * 2, height: height - plotBottom })),
    );
    const areas = svg('g');
    const lines = svg('g');
    const dots = svg('g');
    const withArea = (entry) => entry.series.area ?? state.area;
    const pathOf = state.curve === 'monotone' ? monotonePath : linearPath;
    const showPoints = (entry) => state.points === true || (state.points === 'auto' && entry.points.length <= 24);

    entries.forEach((entry, order) => {
      const scaled = entry.points.map(([px, py]) => [x(px), y(py)]);
      const d = pathOf(scaled);
      if (withArea(entry) && scaled.length > 1) {
        const gradientId = `${clipId}-g${entry.index}`;
        defs.append(
          svg(
            'linearGradient',
            { id: gradientId, x1: 0, y1: 0, x2: 0, y2: 1, style: `--c:${entry.color}` },
            svg('stop', { class: 'chart__area-from', offset: '0%' }),
            svg('stop', { class: 'chart__area-to', offset: '100%' }),
          ),
        );
        areas.append(
          svg('path', {
            class: 'chart__area',
            d: `${d}L${f(scaled[scaled.length - 1][0])},${plotBottom}L${f(scaled[0][0])},${plotBottom}Z`,
            fill: `url(#${gradientId})`,
            style: `--i:${order}`,
          }),
        );
      }
      lines.append(
        svg('path', {
          class: entry.series.dashed ? 'chart__line chart__line--dashed' : 'chart__line',
          d,
          pathLength: entry.series.dashed ? null : 1,
          style: `--c:${entry.color};--i:${order}`,
        }),
      );
      if (showPoints(entry)) {
        scaled.forEach(([px, py], index) => {
          dots.append(svg('circle', { class: 'chart__dot', cx: f(px), cy: f(py), r: 3, style: `--c:${entry.color};--i:${index}` }));
        });
      } else if (scaled.length) {
        const [px, py] = scaled[scaled.length - 1];
        dots.append(svg('circle', { class: 'chart__dot chart__dot--end', cx: f(px), cy: f(py), r: 3.5, style: `--c:${entry.color};--i:${order + 6}` }));
      }
    });

    const xTicks = xAxisTicks(state, xs, minX, maxX, range[1] - range[0]);
    const xAxis = svg('g', { class: 'chart__axis' });
    const xLabels = tidyTickLabels(xTicks.map((tick) => state.xFormat(tick)));
    let lastEdge = -Infinity;
    xTicks.forEach((tick, index) => {
      const label = xLabels[index];
      const labelWidth = measureText(label, { mono: true });
      const center = x(tick);
      if (center - labelWidth / 2 < lastEdge + 12) return;
      xAxis.append(svg('text', { class: 'chart__tick chart__tick--num', x: f(center), y: plotBottom + 18, 'text-anchor': 'middle' }, label));
      lastEdge = center + labelWidth / 2;
    });
    const titles = svg('g');
    if (state.xLabel) titles.append(svg('text', { class: 'chart__caption', x: (range[0] + range[1]) / 2, y: height - 4, 'text-anchor': 'middle' }, state.xLabel));
    if (state.yLabel) titles.append(svg('text', { class: 'chart__caption', x: 0, y: 10 }, state.yLabel));

    const crossLine = svg('line', { class: 'chart__cross', y1: top, y2: plotBottom });
    const crossDots = svg('g');
    const cross = svg('g', { visibility: 'hidden' }, crossLine, crossDots);
    layers = { cross, line: crossLine, dots: crossDots };

    const moving = svg('g', areas, lines, dots);
    const hit = svg('rect', { class: 'chart__hit', x: left, y: 0, width: Math.max(0, width - left), height: plotBottom + 4 });
    hit.addEventListener('pointermove', (event) => {
      pointer = { x: event.clientX, y: event.clientY };
      activate(nearestIndex(pointer.x - root.getBoundingClientRect().left), pointer);
    });
    hit.addEventListener('pointerleave', () => {
      pointer = null;
      deactivate();
    });

    clear(
      root,
      defs,
      valueAxis({ scale: y, ticks: yAxis.ticks, format: yTickFormat, side: 'left', span: [left, width - right], at: left - 10, baseline: logY ? yAxis.min : 0 }),
      svg('g', { 'clip-path': `url(#${clipId}-axis)` }, xAxis),
      titles,
      svg('g', { 'clip-path': `url(#${clipId})` }, moving),
      cross,
      hit,
    );

    if (sliding && previous && xs.length > 1 && !prefersReducedMotion()) {
      const shift = x(xs[xs.length - 1]) - x(xs[xs.length - 2]);
      const frames = [{ transform: `translateX(${f(shift)}px)` }, { transform: 'translateX(0)' }];
      const timing = { duration: state.slideMs ?? 320, easing: 'linear' };
      moving.animate(frames, timing);
      xAxis.animate(frames, timing);
    }
    sliding = false;
    if (pointer) activate(nearestIndex(pointer.x - root.getBoundingClientRect().left), pointer);
    else if (activeIndex >= 0 && plot.matches(':focus-visible')) activate(activeIndex);
    else deactivate();
  }

  const el = createChart(
    'line',
    {
      series: [],
      xScale: 'linear',
      yScale: 'linear',
      xFormat: fmtNumber,
      yFormat: fmtNumber,
      xTicks: 'auto',
      area: false,
      points: 'auto',
      curve: 'linear',
      yZero: true,
      height: 260,
      maxPoints: 120,
      ...props,
    },
    {
      isEmpty: (state) => !(state.series ?? []).some((series) => normalize(series).length),
      legend: (state) =>
        (state.series ?? []).length > 1 ? state.series.map((series, index) => ({ id: series.id, label: series.label, color: seriesColor(series, index) })) : null,
      draw,
      table: (state) => {
        const all = (state.series ?? []).map((series) => ({ label: series.label, points: new Map(normalize(series)) }));
        const xs = Array.from(new Set(all.flatMap((series) => Array.from(series.points.keys())))).sort((a, b) => a - b);
        return dataTable(
          state.ariaLabel ?? 'Courbes',
          [state.xLabel ?? 'x', ...all.map((series) => series.label)],
          xs.slice(-40).map((x) => [state.xFormat(x), ...all.map((series) => (series.points.has(x) ? state.yFormat(series.points.get(x)) : '—'))]),
        );
      },
      navigator: () => ({ count: layout?.xs.length ?? 0, activate: (index) => activate(index), clear: deactivate }),
    },
  );

  el.push = (x, values) => {
    const state = el.state;
    const series = (state.series ?? []).map((item) => {
      const y = values?.[item.id];
      if (y === undefined || y === null) return item;
      const points = [...(item.points ?? []), [x, y]];
      return { ...item, points: points.slice(Math.max(0, points.length - state.maxPoints)) };
    });
    sliding = true;
    el.update({ series });
  };
  return el;
}
