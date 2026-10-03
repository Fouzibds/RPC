/**
 * Petits formats : Sparkline et MiniBars (cellules de tableau, indicateurs), Donut (ratio avec
 * valeur centrée) et StackedBar (une barre découpée en segments étiquetés).
 */

import { h, svg, clear, uid } from '../../core/dom.js';
import { fmtNumber, fmtPercent, splitUnit } from '../../core/format.js';
import { chartTooltip, seriesColor, tipContent } from './core.js';

const f = (n) => Math.round(n * 100) / 100;

function colorOf(props, fallback = 'var(--accent)') {
  return props.color || props.protocol || props.tone ? seriesColor(props) : fallback;
}

/**
 * Mini-courbe sans axes, à glisser dans un indicateur (`Stat({ trailing })`) ou une cellule.
 *
 *     Sparkline({ values: [4, 6, 5, 9, 8, 12], type: 'area', protocol: 'grpc' })
 *
 * @param {Object} [props]
 * @param {number[]} [props.values]
 * @param {'line'|'area'|'bars'} [props.type='line']
 * @param {number} [props.width=96]
 * @param {number} [props.height=28]
 * @param {boolean} [props.responsive=false] Occupe toute la largeur de son conteneur et la suit (ResizeObserver) ;
 *   `width` n'est alors que la largeur du premier tracé. Se choisit à la création ; appeler `destroy()` au nettoyage.
 * @param {boolean} [props.dot=true] Point sur la dernière valeur (`line` et `area`).
 * @param {number} [props.min] Borne basse de l'échelle (par défaut le minimum, ou 0 pour `bars`).
 * @param {number} [props.max] Borne haute de l'échelle.
 * @param {'last'|'max'|'none'} [props.highlight='last'] Barre mise en avant (`bars`).
 * @param {(value: number) => string} [props.format] Active l'info-bulle au survol, avec ce format.
 * @param {string} [props.color] Couleur CSS (sinon `protocol`, `tone`, puis l'accent).
 * @param {'local'|'custom'|'grpc'|'rest'} [props.protocol]
 * @param {'accent'|'success'|'warning'|'danger'|'info'|'neutral'} [props.tone]
 * @param {string} [props.ariaLabel]
 * @returns {HTMLElement & {update: (next: number[]|Object) => void, push: (value: number, maxPoints?: number) => void, destroy: () => void}}
 *   `update` accepte un tableau de valeurs ou un objet de propriétés.
 */
export function Sparkline(props = {}) {
  const state = { values: [], type: 'line', width: 96, height: 28, dot: true, highlight: 'last', ...props };
  const gradientId = uid('spark');
  const root = svg('svg', { class: 'spark__svg', role: 'img' });
  const el = h('span.spark', { class: { 'spark--fill': Boolean(state.responsive) } }, root);
  let centers = [];
  let observer = null;

  function render() {
    const { width, height, type } = state;
    const values = (state.values ?? []).map(Number).filter(Number.isFinite);
    root.setAttribute('width', String(width));
    root.setAttribute('height', String(height));
    root.setAttribute('viewBox', `0 0 ${width} ${height}`);
    root.setAttribute('aria-label', state.ariaLabel ?? `Tendance sur ${values.length} points`);
    el.dataset.type = type;
    el.style.setProperty('--c', colorOf(state));
    centers = [];
    if (values.length === 0) {
      clear(root, svg('line', { class: 'spark__empty', x1: 0, x2: width, y1: height - 1.5, y2: height - 1.5 }));
      return;
    }
    const pad = 3;
    const low = state.min ?? (type === 'bars' ? Math.min(0, ...values) : Math.min(...values));
    const high = state.max ?? Math.max(...values);
    const span = high - low || 1;
    const y = (value) => height - pad - ((value - low) / span) * (height - pad * 2);

    if (type === 'bars') {
      const gap = values.length > 24 ? 1 : 2;
      const barWidth = Math.max(1, (width - gap * (values.length - 1)) / values.length);
      const peak = values.indexOf(Math.max(...values));
      const marked = state.highlight === 'last' ? values.length - 1 : state.highlight === 'max' ? peak : -1;
      clear(
        root,
        values.map((value, index) => {
          const top = Math.min(y(value), height - 1.5);
          const left = index * (barWidth + gap);
          centers.push(left + barWidth / 2);
          return svg('rect', {
            class: index === marked ? 'spark__bar spark__bar--on' : 'spark__bar',
            x: f(left),
            y: f(top),
            width: f(barWidth),
            height: f(height - top),
            rx: Math.min(1.5, barWidth / 2),
          });
        }),
      );
      return;
    }

    const step = values.length > 1 ? (width - pad * 2) / (values.length - 1) : 0;
    const points = values.map((value, index) => [values.length > 1 ? pad + index * step : width / 2, y(value)]);
    centers = points.map((point) => point[0]);
    const d = points.map(([px, py], index) => `${index ? 'L' : 'M'}${f(px)},${f(py)}`).join('');
    const [lastX, lastY] = points[points.length - 1];
    clear(
      root,
      type === 'area'
        ? [
            svg(
              'defs',
              svg(
                'linearGradient',
                { id: gradientId, x1: 0, y1: 0, x2: 0, y2: 1 },
                svg('stop', { class: 'chart__area-from', offset: '0%' }),
                svg('stop', { class: 'chart__area-to', offset: '100%' }),
              ),
            ),
            svg('path', { class: 'spark__area', d: `${d}L${f(lastX)},${height}L${f(points[0][0])},${height}Z`, fill: `url(#${gradientId})` }),
          ]
        : null,
      svg('path', { class: 'spark__line', d }),
      state.dot ? svg('circle', { class: 'spark__dot', cx: f(lastX), cy: f(lastY), r: 2.5 }) : null,
    );
  }

  el.addEventListener('pointermove', (event) => {
    if (!state.format || !centers.length) return;
    const px = event.clientX - root.getBoundingClientRect().left;
    let index = 0;
    centers.forEach((center, i) => {
      if (Math.abs(center - px) < Math.abs(centers[index] - px)) index = i;
    });
    const values = state.values.map(Number).filter(Number.isFinite);
    chartTooltip.show({
      owner: el,
      key: index,
      x: event.clientX,
      y: event.clientY,
      content: () => tipContent({ rows: [{ label: `Point ${index + 1} / ${values.length}`, value: state.format(values[index]), color: colorOf(state), shape: 'line' }] }),
    });
  });
  el.addEventListener('pointerleave', () => chartTooltip.hide(el));

  el.update = (next) => {
    Object.assign(state, Array.isArray(next) ? { values: next } : next);
    render();
  };
  el.push = (value, maxPoints = 40) => {
    state.values = [...state.values, value].slice(-maxPoints);
    render();
  };
  el.destroy = () => {
    observer?.disconnect();
    chartTooltip.hide(el);
  };
  render();
  if (state.responsive) {
    observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[entries.length - 1].contentRect.width);
      if (next > 0 && next !== state.width) {
        state.width = next;
        render();
      }
    });
    observer.observe(el);
  }
  return el;
}

/**
 * Mini-histogramme pour une cellule de tableau : `Sparkline` en barres, format compact.
 * @param {Object} [props] Mêmes propriétés que `Sparkline` ; par défaut 64 × 20 px, dernière barre mise en avant.
 * @returns {HTMLElement & {update: (next: number[]|Object) => void, push: (value: number, maxPoints?: number) => void, destroy: () => void}}
 */
export function MiniBars(props = {}) {
  return Sparkline({ width: 64, height: 20, ...props, type: 'bars' });
}

/**
 * @typedef {Object} DonutSegment
 * @property {string} label
 * @property {number} value
 * @property {string} [id] Identité stable (par défaut le rang) : garde l'animation quand les valeurs changent.
 * @property {'accent'|'success'|'warning'|'danger'|'info'|'neutral'} [tone]
 * @property {string} [color] Couleur CSS (sinon `protocol`, `tone`, puis une teinte catégorielle).
 * @property {'local'|'custom'|'grpc'|'rest'} [protocol]
 * @property {string} [hint] Précision affichée dans l'info-bulle.
 */

/**
 * Anneau (ou jauge en arc) pour un ratio, avec la valeur au centre : taux de succès, octets
 * économisés par Protobuf… Avec `segments`, l'anneau se découpe en plusieurs parts (issues d'une
 * série de scénarios, répartition des erreurs) : le total s'écrit au centre, une légende chiffrée
 * accompagne l'anneau, et survoler une part ou sa ligne de légende met l'autre en avant.
 *
 *     Donut({ value: 0.62, label: 'économisés', tone: 'success' })
 *     Donut({ label: 'scénarios joués', segments: [{ label: 'Compatibles', value: 5, tone: 'success' }, { label: 'Silencieux', value: 3, tone: 'danger' }] })
 *
 * @param {Object} [props]
 * @param {number} [props.value=0] Ratio entre 0 et 1 (anneau simple).
 * @param {DonutSegment[]} [props.segments] Parts de l'anneau. Leur présence à la création choisit le mode
 *   multi-segments ; l'élément renvoyé contient alors l'anneau et sa légende.
 * @param {number} [props.total] Total de référence (multi-segments) ; s'il dépasse la somme, le reste de l'anneau reste vide.
 * @param {boolean|'right'|'bottom'} [props.legend=true] Légende des segments : à droite de l'anneau (défaut), dessous, ou absente.
 * @param {string} [props.label] Légende sous la valeur.
 * @param {(value: number) => string} [props.format] Format de la valeur : pourcentage entier pour un ratio, nombre
 *   pour des segments (valeur de chaque part et total au centre).
 * @param {number} [props.size=120] Diamètre en pixels.
 * @param {number} [props.thickness=9] Épaisseur de l'anneau.
 * @param {'ring'|'gauge'} [props.variant='ring'] `gauge` : arc de 270° ouvert vers le bas.
 * @param {string} [props.color] Couleur CSS (sinon `protocol`, `tone`, puis l'accent).
 * @param {'local'|'custom'|'grpc'|'rest'} [props.protocol]
 * @param {'accent'|'success'|'warning'|'danger'|'info'|'neutral'} [props.tone]
 * @param {string} [props.ariaLabel]
 * @returns {HTMLElement & {update: (patch: Object) => void, set: (value: number|DonutSegment[]) => void, destroy: () => void}}
 *   `set` reçoit un ratio (anneau simple) ou une liste de segments (multi-segments).
 */
export function Donut(props = {}) {
  const multi = Array.isArray(props.segments);
  const state = { value: 0, size: 120, thickness: 9, variant: 'ring', legend: true, format: multi ? fmtNumber : (value) => fmtPercent(value, { decimals: 0 }), ...props };
  const track = svg('circle', { class: 'donut__track' });
  const arc = svg('circle', { class: 'donut__arc' });
  const parts = multi ? svg('g') : null;
  const root = svg('svg', { class: 'donut__svg', 'aria-hidden': 'true' }, track, parts ?? arc);
  const number = h('span.donut__number.num');
  const unit = h('span.donut__unit');
  const label = h('span.donut__label');
  const ring = h('div.donut', { role: 'img' }, root, h('div.donut__center', h('span.donut__value', number, unit), label));
  const legend = multi ? h('ul.donut__legend') : null;
  const el = multi ? h('div.donut-set', { role: 'group' }, ring, legend) : ring;
  /** Arcs conservés d'un rendu à l'autre (clé → cercle) : leur longueur s'anime quand les valeurs changent. */
  const arcs = new Map();
  /** Segments du dernier rendu (clé → segment), lus par les survols. */
  let live = new Map();
  /** Nœuds à mettre en avant ensemble (clé → arc et ligne de légende). */
  let nodes = new Map();
  let mounted = false;

  function highlight(key) {
    ring.dataset.hover = String(key !== null);
    for (const [id, list] of nodes) list.forEach((node) => node.toggleAttribute('data-active', id === key));
  }

  /** Survol d'une part ou de sa ligne de légende : mise en avant et info-bulle. */
  function bind(node, key) {
    node.addEventListener('pointerenter', () => highlight(key));
    node.addEventListener('pointerleave', () => {
      highlight(null);
      chartTooltip.hide(el);
    });
    node.addEventListener('pointermove', (event) => {
      const segment = live.get(key);
      if (!segment || segment.amount === 0) return;
      chartTooltip.show({
        owner: el,
        key: `${key}|${segment.amount}`,
        x: event.clientX,
        y: event.clientY,
        content: () =>
          tipContent({
            title: segment.label,
            rows: [
              { label: 'Valeur', value: state.format(segment.amount), color: segment.fill, shape: 'rect' },
              { label: 'Part', value: fmtPercent(segment.share, { decimals: segment.share < 0.1 ? 1 : 0 }), muted: true },
            ],
            note: segment.hint,
          }),
      });
    });
    return node;
  }

  /** Découpe l'anneau en parts et écrit la légende ; renvoie le texte du centre et le nom accessible. */
  function renderSegments(radius, circumference, sweep) {
    const amounts = (state.segments ?? []).map((segment) => Math.max(0, Number(segment.value) || 0));
    const sum = amounts.reduce((total, amount) => total + amount, 0);
    const whole = Math.max(sum, Number(state.total) || 0) || 1;
    const segments = (state.segments ?? []).map((segment, index) => ({
      ...segment,
      key: String(segment.id ?? index),
      amount: amounts[index],
      share: amounts[index] / whole,
      fill: seriesColor(segment, index),
    }));
    const drawn = segments.filter((segment) => segment.amount > 0);
    const gap = drawn.length > 1 || whole > sum ? 2 : 0;
    live = new Map(segments.map((segment) => [segment.key, segment]));
    nodes = new Map(segments.map((segment) => [segment.key, []]));

    let offset = 0;
    const kept = new Set();
    for (const segment of drawn) {
      const length = circumference * sweep * segment.share;
      let node = arcs.get(segment.key);
      if (!node) {
        node = bind(svg('circle', { class: 'donut__segment' }), segment.key);
        arcs.set(segment.key, node);
      }
      kept.add(segment.key);
      node.setAttribute('cx', String(state.size / 2));
      node.setAttribute('cy', String(state.size / 2));
      node.setAttribute('r', String(f(radius)));
      node.setAttribute('stroke-width', String(state.thickness));
      node.setAttribute('stroke-dasharray', `${f(mounted ? Math.max(0.5, length - gap) : 0)} ${f(circumference)}`);
      node.setAttribute('stroke-dashoffset', String(f(-offset)));
      node.style.setProperty('--c', segment.fill);
      parts.append(node);
      nodes.get(segment.key).push(node);
      offset += length;
    }
    for (const [key, node] of arcs) {
      if (!kept.has(key)) {
        node.remove();
        arcs.delete(key);
      }
    }

    legend.hidden = !state.legend;
    el.dataset.legend = state.legend === 'bottom' ? 'bottom' : 'right';
    clear(
      legend,
      state.legend
        ? segments.map((segment) => {
            const item = h(
              'li.donut__item',
              { dataset: { zero: String(segment.amount === 0) }, style: { '--c': segment.fill } },
              h('span.donut__key', { 'aria-hidden': 'true' }),
              h('span.donut__item-label.truncate', segment.label),
              h('span.donut__item-value.num', state.format(segment.amount)),
              h('span.donut__share.num', fmtPercent(segment.share, { decimals: 0 })),
            );
            nodes.get(segment.key).push(item);
            return bind(item, segment.key);
          })
        : null,
    );
    return { text: state.format(sum), summary: segments.map((segment) => `${segment.label} : ${state.format(segment.amount)}`).join(', ') };
  }

  function render() {
    const { size, thickness } = state;
    const ratio = Math.min(1, Math.max(0, Number(state.value) || 0));
    const radius = (size - thickness) / 2;
    const circumference = 2 * Math.PI * radius;
    const sweep = state.variant === 'gauge' ? 0.75 : 1;
    root.setAttribute('width', String(size));
    root.setAttribute('height', String(size));
    root.setAttribute('viewBox', `0 0 ${size} ${size}`);
    root.style.transform = `rotate(${state.variant === 'gauge' ? 135 : -90}deg)`;
    for (const circle of [track, arc]) {
      circle.setAttribute('cx', String(size / 2));
      circle.setAttribute('cy', String(size / 2));
      circle.setAttribute('r', String(f(radius)));
      circle.setAttribute('stroke-width', String(thickness));
    }
    track.setAttribute('stroke-dasharray', `${f(circumference * sweep)} ${f(circumference)}`);
    ring.style.width = `${size}px`;
    ring.style.height = `${size}px`;
    ring.dataset.size = size >= 104 ? 'md' : 'sm';

    let shown;
    let summary = '';
    if (multi) {
      const result = renderSegments(radius, circumference, sweep);
      shown = result.text;
      summary = ` — ${result.summary}`;
    } else {
      shown = state.format(ratio);
      arc.setAttribute('stroke-dasharray', `${f(circumference * sweep * (mounted ? ratio : 0))} ${f(circumference)}`);
      arc.style.opacity = ratio === 0 ? '0' : '1';
      ring.style.setProperty('--c', colorOf(state));
    }
    const text = splitUnit(shown);
    const name = `${shown}${state.label ? ` ${state.label}` : ''}`;
    number.textContent = text.value;
    unit.textContent = text.unit;
    clear(label, state.label ?? null);
    label.hidden = !state.label;
    ring.setAttribute('aria-label', multi ? name : (state.ariaLabel ?? name));
    if (multi) el.setAttribute('aria-label', state.ariaLabel ?? `${name}${summary}`);
  }

  el.update = (patch) => {
    Object.assign(state, patch);
    render();
  };
  el.set = (value) => el.update(Array.isArray(value) ? { segments: value } : { value });
  el.destroy = () => chartTooltip.hide(el);
  render();
  requestAnimationFrame(() => {
    mounted = true;
    render();
  });
  return el;
}

/**
 * @typedef {Object} StackSegment
 * @property {string} id
 * @property {string} label
 * @property {number} value
 * @property {string} [color] Couleur CSS (sinon `protocol`, `tone`, puis une teinte catégorielle).
 * @property {'local'|'custom'|'grpc'|'rest'} [protocol]
 * @property {'accent'|'success'|'warning'|'danger'|'info'|'neutral'} [tone]
 * @property {string} [hint] Précision affichée dans l'info-bulle.
 */

/**
 * Une seule barre horizontale découpée en segments, suivie de sa légende chiffrée : la
 * décomposition du temps d'un appel (sérialisation, réseau, exécution, désérialisation).
 * Survoler un segment ou une entrée de légende met l'autre en avant.
 *
 *     StackedBar({ format: fmtUs, segments: [{ id: 'net', label: 'Réseau', value: 211, color: LANE_COLORS.network }] })
 *
 * @param {Object} [props]
 * @param {StackSegment[]} [props.segments]
 * @param {(value: number) => string} [props.format=fmtNumber]
 * @param {number} [props.total] Total de référence ; s'il dépasse la somme, le reste apparaît vide.
 * @param {number} [props.thickness=12] Épaisseur de la barre en pixels.
 * @param {boolean} [props.legend=true] Affiche la légende chiffrée sous la barre.
 * @param {string} [props.ariaLabel]
 * @returns {HTMLElement & {update: (patch: Object) => void, destroy: () => void}}
 */
export function StackedBar(props = {}) {
  const state = { segments: [], format: fmtNumber, thickness: 12, legend: true, ...props };
  const bar = h('div.stacked__bar');
  const legend = h('div.stacked__legend');
  const el = h('div.stacked', { role: 'group' }, bar, legend);
  const parts = new Map();

  function highlight(id) {
    el.dataset.hover = String(id !== null);
    for (const [key, nodes] of parts) nodes.forEach((node) => node.toggleAttribute('data-active', key === id));
  }

  function render() {
    const segments = (state.segments ?? []).filter((segment) => Number(segment.value) > 0);
    const sum = segments.reduce((total, segment) => total + Number(segment.value), 0);
    const whole = Math.max(sum, Number(state.total) || 0) || 1;
    el.setAttribute('aria-label', state.ariaLabel ?? 'Répartition');
    bar.style.height = `${state.thickness}px`;
    parts.clear();

    const share = (segment) => fmtPercent(Number(segment.value) / whole, { decimals: Number(segment.value) / whole < 0.1 ? 1 : 0 });
    const bind = (node, segment) => {
      node.addEventListener('pointerenter', () => highlight(segment.id));
      node.addEventListener('pointerleave', () => {
        highlight(null);
        chartTooltip.hide(el);
      });
      node.addEventListener('pointermove', (event) => {
        chartTooltip.show({
          owner: el,
          key: segment.id,
          x: event.clientX,
          y: event.clientY,
          content: () =>
            tipContent({
              title: segment.label,
              rows: [
                { label: 'Valeur', value: state.format(segment.value), color: segment.fill, shape: 'rect' },
                { label: 'Part', value: share(segment), muted: true },
              ],
              note: segment.hint,
            }),
        });
      });
      return node;
    };

    const colored = segments.map((segment, index) => ({ ...segment, fill: seriesColor(segment, index) }));
    clear(
      bar,
      colored.map((segment, index) => {
        const node = bind(h('span.stacked__segment', { style: { '--c': segment.fill, '--i': index, flexGrow: String(segment.value) } }), segment);
        parts.set(segment.id, [node]);
        return node;
      }),
      whole > sum ? h('span.stacked__rest', { style: { flexGrow: String(whole - sum) } }) : null,
    );
    legend.hidden = !state.legend;
    clear(
      legend,
      state.legend
        ? colored.map((segment) => {
            const node = bind(
              h(
                'span.stacked__item',
                h('span.stacked__key', { style: { '--c': segment.fill } }),
                h('span.stacked__label', segment.label),
                h('span.stacked__value.num', state.format(segment.value)),
                h('span.stacked__share.num', share(segment)),
              ),
              segment,
            );
            parts.get(segment.id).push(node);
            return node;
          })
        : null,
    );
  }

  el.update = (patch) => {
    Object.assign(state, patch);
    render();
  };
  el.destroy = () => chartTooltip.hide(el);
  render();
  return el;
}
