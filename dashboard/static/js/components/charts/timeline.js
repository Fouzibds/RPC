/**
 * Timeline : couloirs de marques positionnées dans le temps (tentatives d'appel, attentes,
 * ouverture du disjoncteur), avec axe du temps, info-bulle, ajout en direct et suivi automatique.
 * C'est la chronologie du laboratoire de chaos.
 */

import { svg, clear, uid } from '../../core/dom.js';
import { fmtMs } from '../../core/format.js';
import { chartTooltip, clientPoint, createChart, fitText, linearScale, measureText, seriesColor, svgRoot, ticks as niceTicks, tipContent, valueAxis } from './core.js';

/**
 * @typedef {Object} TimelineKind
 * @property {string} label Nom affiché dans la légende et l'info-bulle.
 * @property {'success'|'warning'|'danger'|'info'|'accent'|'neutral'} tone
 * @property {'solid'|'hatch'|'thin'} [style='solid'] `hatch` : hachures (état subi) ; `thin` : trait fin (attente).
 */

/** Types de marques reconnus par défaut. @type {Readonly<Record<string, TimelineKind>>} */
export const TIMELINE_KINDS = Object.freeze({
  ok: { label: 'Succès', tone: 'success', style: 'solid' },
  retry: { label: 'Nouvelle tentative', tone: 'info', style: 'solid' },
  timeout: { label: 'Délai dépassé', tone: 'warning', style: 'solid' },
  error: { label: 'Erreur', tone: 'danger', style: 'solid' },
  breaker: { label: 'Disjoncteur ouvert', tone: 'danger', style: 'hatch' },
  backoff: { label: 'Attente avant nouvel essai', tone: 'neutral', style: 'thin' },
  pending: { label: 'En cours', tone: 'accent', style: 'hatch' },
  info: { label: 'Évènement', tone: 'neutral', style: 'solid' },
});

/**
 * @typedef {Object} TimelineLane
 * @property {string} id
 * @property {string} label
 * @property {string} [sublabel] Seconde ligne, plus discrète.
 * @property {'local'|'custom'|'grpc'|'rest'} [protocol] Pastille à la couleur du protocole.
 * @property {string} [color]
 */

/**
 * @typedef {Object} TimelineItem
 * @property {string} lane Identifiant du couloir.
 * @property {number} start Instant de début, dans l'unité de `format` (millisecondes par défaut).
 * @property {number} [end] Instant de fin : avec, c'est une plage ; sans, un évènement ponctuel (losange).
 * @property {string} kind Clé de `TIMELINE_KINDS` (ou de `props.kinds`) : `ok`, `retry`, `timeout`, `error`, `breaker`, `backoff`…
 * @property {string} [id] Identité stable : `push` remplace la marque qui porte le même identifiant.
 * @property {string} [label] Texte court écrit dans la plage si elle est assez large (« 1 », « 2 »…).
 * @property {string} [title] Titre de l'info-bulle (par défaut le nom du type).
 * @property {string} [detail] Texte libre de l'info-bulle (code d'erreur, message).
 */

const SPAN_HEIGHT = 16;
const THIN_HEIGHT = 6;

/**
 * Chronologie en couloirs. Par défaut, toute la durée tient dans la largeur ; avec `window`,
 * seules les dernières millisecondes sont visibles et la vue suit les nouvelles marques.
 *
 *     const timeline = Timeline({ lanes: [{ id: 'naive', label: 'Client naïf' }], items: [] });
 *     timeline.push({ lane: 'naive', start: 0, end: 5000, kind: 'timeout', label: '1' });
 *
 * @param {Object} [props]
 * @param {TimelineLane[]} [props.lanes]
 * @param {TimelineItem[]} [props.items]
 * @param {Record<string, TimelineKind>} [props.kinds] Types supplémentaires ou redéfinis.
 * @param {(value: number) => string} [props.format=fmtMs] Format des instants et des durées.
 * @param {number} [props.window] Largeur de la fenêtre glissante ; sans elle, tout est visible.
 * @param {number} [props.minSpan=1000] Durée minimale représentée (évite un axe dilaté au démarrage).
 * @param {number} [props.laneHeight=40]
 * @param {number|null} [props.now] Position du curseur « maintenant ».
 * @param {boolean} [props.legend=true] Légende des types de marques présents.
 * @param {(item: TimelineItem) => void} [props.onSelect] Clic sur une marque.
 * @param {string} [props.ariaLabel]
 * @param {{icon?: string, title?: string, text?: string}} [props.empty]
 * @param {boolean} [props.loading]
 * @returns {HTMLElement & {update: (patch: Object) => void, push: (items: TimelineItem|TimelineItem[]) => void, setNow: (now: number|null) => void, clear: () => void, setLoading: (on: boolean) => void, destroy: () => void}}
 */
export function Timeline(props = {}) {
  const patternBase = uid('timeline-hatch');
  let root = null;
  let frame = null;
  let layout = null;
  let activeKey = null;
  const elements = new Map();

  const kindOf = (state, item) => state.kinds?.[item.kind] ?? TIMELINE_KINDS[item.kind] ?? TIMELINE_KINDS.info;
  const keyOf = (item, index) => String(item.id ?? `#${index}`);

  function ensureFrame(plot, width, height) {
    const fresh = !root || root.parentNode !== plot;
    root = svgRoot(root, plot, width, height);
    if (fresh || !frame) {
      elements.clear();
      frame = { defs: svg('defs'), back: svg('g'), spans: svg('g'), marks: svg('g'), front: svg('g'), hits: svg('g') };
      clear(root, frame.defs, frame.back, frame.spans, frame.marks, frame.front, frame.hits);
    }
  }

  function deactivate() {
    if (activeKey !== null) elements.get(activeKey)?.removeAttribute('data-active');
    activeKey = null;
    chartTooltip.hide(el);
  }

  function activate(entry, point) {
    const state = el.state;
    if (activeKey !== entry.key) {
      if (activeKey !== null) elements.get(activeKey)?.removeAttribute('data-active');
      activeKey = entry.key;
      elements.get(entry.key)?.setAttribute('data-active', 'true');
    }
    const { item, kind, lane } = entry;
    const at = point ?? clientPoint(root, entry.x1, entry.cy + 10);
    chartTooltip.show({
      owner: el,
      key: entry.key,
      x: at.x,
      y: at.y,
      content: () =>
        tipContent({
          title: item.title ?? kind.label,
          subtitle: lane.label,
          rows: [
            { label: item.end === undefined ? 'Instant' : 'Début', value: state.format(item.start), color: `var(--${kind.tone})`, shape: 'rect' },
            ...(item.end === undefined ? [] : [{ label: 'Durée', value: state.format(item.end - item.start) }]),
          ],
          note: item.detail,
        }),
    });
  }

  /** Marque la plus proche du pointeur dans un couloir : celle qui le contient (la plus étroite), sinon à moins de 10 px. */
  function pick(laneId, pixel) {
    let best = null;
    let bestScore = Infinity;
    for (const entry of layout.entries) {
      if (entry.item.lane !== laneId) continue;
      const inside = pixel >= entry.x0 - 2 && pixel <= entry.x1 + 2;
      const distance = inside ? 0 : Math.min(Math.abs(pixel - entry.x0), Math.abs(pixel - entry.x1));
      if (distance > 10) continue;
      const score = distance * 1000 + (entry.x1 - entry.x0);
      if (score < bestScore) {
        best = entry;
        bestScore = score;
      }
    }
    return best;
  }

  function draw({ plot, width, state }) {
    const lanes = state.lanes ?? [];
    const items = (state.items ?? []).filter((item) => Number.isFinite(Number(item.start)));
    const laneHeight = state.laneHeight ?? 40;
    const hasSub = lanes.some((lane) => lane.sublabel);
    const left = Math.ceil(Math.max(0, ...lanes.map((lane) => Math.max(measureText(lane.label, { size: 12 }), measureText(lane.sublabel ?? '', { size: 11 }))))) + 30;
    const right = 14;
    const top = 4;
    const plotBottom = top + lanes.length * laneHeight;
    const height = plotBottom + 26;

    const now = Number.isFinite(state.now) ? state.now : null;
    const latest = Math.max(0, now ?? 0, ...items.map((item) => item.end ?? item.start));
    const earliest = Math.min(0, ...items.map((item) => item.start));
    let from = earliest;
    let to = Math.max(latest, earliest + (state.minSpan ?? 1000));
    if (state.window && to - from > state.window) from = to - state.window;
    const pad = (to - from) * 0.02;
    const x = linearScale([from, to + pad], [left, width - right]);

    ensureFrame(plot, width, height);

    const usedHatch = new Set();
    const back = [];
    lanes.forEach((lane, index) => {
      const y = top + index * laneHeight;
      const cy = y + laneHeight / 2;
      back.push(
        svg('rect', { class: 'timeline__lane', x: left - 8, y: y + 5, width: Math.max(0, width - left + 8), height: laneHeight - 10, rx: 6 }),
        lane.protocol || lane.color ? svg('circle', { class: 'chart__key', cx: 4, cy: hasSub && lane.sublabel ? cy - 7 : cy, r: 3.5, style: `--c:${seriesColor(lane)}` }) : null,
        svg('text', { class: 'chart__category', x: lane.protocol || lane.color ? 14 : 0, y: lane.sublabel ? cy - 7 : cy, dy: '0.34em' }, lane.label),
        lane.sublabel ? svg('text', { class: 'chart__caption', x: lane.protocol || lane.color ? 14 : 0, y: cy + 8, dy: '0.34em' }, lane.sublabel) : null,
      );
    });

    const laneIndex = new Map(lanes.map((lane, index) => [lane.id, index]));
    const seen = new Set();
    const entries = [];
    items.forEach((item, index) => {
      const row = laneIndex.get(item.lane);
      if (row === undefined) return;
      const kind = kindOf(state, item);
      const key = keyOf(item, index);
      const cy = top + row * laneHeight + laneHeight / 2;
      const isSpan = item.end !== undefined;
      const x0 = x(item.start);
      const x1 = isSpan ? Math.max(x0 + 3, x(item.end)) : x0;
      if (x1 < left - 4) return;
      seen.add(key);
      let node = elements.get(key);
      const shape = isSpan ? 'span' : 'mark';
      if (!node || node.dataset.shape !== shape) {
        node?.remove();
        node = isSpan ? svg('g', { class: 'timeline__item timeline__span' }, svg('rect'), svg('text', { class: 'timeline__text' })) : svg('g', { class: 'timeline__item timeline__mark' }, svg('rect'));
        node.dataset.shape = shape;
        elements.set(key, node);
        (isSpan ? frame.spans : frame.marks).append(node);
      }
      node.dataset.tone = kind.tone;
      node.dataset.style = kind.style ?? 'solid';
      const rect = node.firstElementChild;
      if (isSpan) {
        const tall = kind.style === 'thin' ? THIN_HEIGHT : SPAN_HEIGHT;
        const start = Math.max(left - 8, x0);
        rect.setAttribute('x', String(Math.round(start * 10) / 10));
        rect.setAttribute('y', String(cy - tall / 2));
        rect.setAttribute('width', String(Math.round(Math.max(3, x1 - start) * 10) / 10));
        rect.setAttribute('height', String(tall));
        rect.setAttribute('rx', String(kind.style === 'thin' ? 3 : 4));
        if (kind.style === 'hatch') {
          usedHatch.add(kind.tone);
          rect.style.fill = `url(#${patternBase}-${kind.tone})`;
        } else {
          rect.style.fill = '';
        }
        const text = node.lastElementChild;
        const room = x1 - start - 8;
        const label = item.label && kind.style !== 'thin' && room > 6 ? fitText(String(item.label), room, { size: 10, weight: 600 }) : '';
        text.textContent = label;
        text.setAttribute('x', String(Math.round(start + 5)));
        text.setAttribute('y', String(cy));
        text.setAttribute('dy', '0.36em');
      } else {
        rect.setAttribute('x', String(Math.round(x0 - 4.5)));
        rect.setAttribute('y', String(cy - 4.5));
        rect.setAttribute('width', '9');
        rect.setAttribute('height', '9');
        rect.setAttribute('rx', '1.5');
        rect.setAttribute('transform', `rotate(45 ${Math.round(x0)} ${cy})`);
      }
      entries.push({ key, item, kind, lane: lanes[row], x0: isSpan ? x0 : x0 - 5, x1: isSpan ? x1 : x0 + 5, cy });
    });
    for (const [key, node] of elements) {
      if (!seen.has(key)) {
        node.remove();
        elements.delete(key);
      }
    }

    clear(
      frame.defs,
      Array.from(usedHatch, (tone) =>
        svg(
          'pattern',
          { id: `${patternBase}-${tone}`, class: 'timeline__hatch', 'data-tone': tone, width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
          svg('rect', { class: 'timeline__hatch-bg', width: 6, height: 6 }),
          svg('line', { class: 'timeline__hatch-line', x1: 0, x2: 0, y1: 0, y2: 6 }),
        ),
      ),
    );
    clear(
      frame.back,
      valueAxis({ scale: x, ticks: niceTicks(from, to, Math.max(2, Math.round((width - left - right) / 110))), format: state.format, side: 'bottom', span: [top + 5, plotBottom - 5], at: plotBottom + 17 }),
      back,
    );
    const nowX = now === null ? null : Math.round(x(now)) + 0.5;
    clear(
      frame.front,
      nowX !== null && nowX >= left
        ? [svg('line', { class: 'timeline__now', x1: nowX, x2: nowX, y1: top, y2: plotBottom }), svg('path', { class: 'timeline__now-cap', d: `M${nowX - 4},${top - 1}h8l-4,5z` })]
        : null,
    );
    clear(
      frame.hits,
      lanes.map((lane, index) => {
        const hit = svg('rect', { class: 'chart__hit', x: left - 8, y: top + index * laneHeight, width: Math.max(0, width - left + 8), height: laneHeight });
        const entryAt = (event) => pick(lane.id, event.clientX - root.getBoundingClientRect().left);
        hit.addEventListener('pointermove', (event) => {
          const entry = entryAt(event);
          hit.style.cursor = entry && state.onSelect ? 'pointer' : '';
          if (entry) activate(entry, { x: event.clientX, y: event.clientY });
          else deactivate();
        });
        hit.addEventListener('pointerleave', deactivate);
        hit.addEventListener('click', (event) => {
          const entry = entryAt(event);
          if (entry) state.onSelect?.(entry.item);
        });
        return hit;
      }),
    );
    layout = { entries: entries.sort((a, b) => a.item.start - b.item.start) };
  }

  const el = createChart(
    'timeline',
    { lanes: [], items: [], format: fmtMs, minSpan: 1000, laneHeight: 40, legend: true, now: null, ...props },
    {
      isEmpty: (state) => !(state.lanes ?? []).length,
      staticLegend: true,
      legend: (state) => {
        if (!state.legend) return null;
        const present = new Set((state.items ?? []).map((item) => item.kind));
        const all = { ...TIMELINE_KINDS, ...(state.kinds ?? {}) };
        return Object.entries(all)
          .filter(([id]) => present.has(id))
          .map(([id, kind]) => ({ id, label: kind.label, color: `var(--${kind.tone})`, shape: kind.style === 'hatch' ? 'hatch' : kind.style === 'thin' ? 'line' : 'rect' }));
      },
      draw,
      navigator: () => ({
        count: layout?.entries.length ?? 0,
        activate: (index) => activate(layout.entries[index]),
        clear: deactivate,
      }),
    },
  );

  el.push = (incoming) => {
    const items = [...(el.state.items ?? [])];
    for (const item of Array.isArray(incoming) ? incoming : [incoming]) {
      const index = item.id === undefined ? -1 : items.findIndex((existing) => existing.id === item.id);
      if (index >= 0) items[index] = item;
      else items.push(item);
    }
    el.update({ items });
  };
  el.setNow = (now) => el.update({ now });
  el.clear = () => el.update({ items: [], now: null });
  return el;
}
