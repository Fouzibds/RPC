/**
 * Socle commun des graphiques : échelles « rondes », mesure de texte, info-bulle partagée,
 * légende, tableau accessible et châssis (redimensionnement, états vide / chargement,
 * animation d'apparition, navigation au clavier).
 */

import { h, svg, clear, on } from '../../core/dom.js';
import { protocol } from '../../core/protocols.js';
import { Chip, EmptyState, Skeleton } from '../ui.js';

/* --- Couleurs ------------------------------------------------------------------ */

/**
 * Teintes catégorielles hors protocoles (champs Protobuf, séries diverses), dans un ordre fixe
 * validé pour les daltoniens. Ne jamais boucler au-delà : regrouper le reste dans « Autres ».
 */
export const CHART_COLORS = Object.freeze(['var(--viz-1)', 'var(--viz-2)', 'var(--viz-3)', 'var(--viz-4)', 'var(--viz-5)', 'var(--viz-6)']);

/** Couleurs des trois « couloirs » d'un appel distant : client, réseau, serveur. */
export const LANE_COLORS = Object.freeze({ client: 'var(--lane-client)', network: 'var(--lane-network)', server: 'var(--lane-server)' });

const TONES = new Set(['accent', 'success', 'warning', 'danger', 'info', 'neutral']);

/**
 * Couleur CSS d'une série ou d'une marque : `color` explicite, sinon couleur du `protocol`,
 * sinon `tone` (`success`, `danger`…), sinon teinte catégorielle de rang `index`.
 * @param {{color?: string, protocol?: string, tone?: string}} item
 * @param {number} [index=0]
 * @returns {string} Valeur prête pour une déclaration CSS (`var(--proto-grpc)`).
 */
export function seriesColor(item, index = 0) {
  if (item?.color) return item.color;
  if (item?.protocol) return protocol(item.protocol).color;
  if (item?.tone && TONES.has(item.tone)) return `var(--${item.tone})`;
  return CHART_COLORS[index % CHART_COLORS.length];
}

/* --- Échelles ------------------------------------------------------------------- */

function tidy(value) {
  return Number(value.toPrecision(12));
}

function niceStep(raw) {
  const power = 10 ** Math.floor(Math.log10(raw));
  const fraction = raw / power;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  return nice * power;
}

/**
 * Étend un intervalle à des bornes « rondes » et calcule ses graduations (pas de 1, 2, 2,5 ou 5 × 10ⁿ).
 * @param {number} min
 * @param {number} max
 * @param {number} [count=5] Nombre de graduations visé.
 * @returns {{min: number, max: number, step: number, ticks: number[]}}
 */
export function niceScale(min, max, count = 5) {
  let lo = Number.isFinite(min) ? min : 0;
  let hi = Number.isFinite(max) ? max : 1;
  if (lo > hi) [lo, hi] = [hi, lo];
  if (lo === hi) {
    if (lo === 0) hi = 1;
    else if (lo > 0) lo = 0;
    else hi = 0;
  }
  const step = niceStep((hi - lo) / Math.max(1, count));
  const first = Math.floor(lo / step + 1e-9) * step;
  const last = Math.ceil(hi / step - 1e-9) * step;
  const ticks = [];
  for (let i = 0; first + i * step <= last + step / 2; i += 1) ticks.push(tidy(first + i * step));
  return { min: tidy(first), max: tidy(last), step, ticks };
}

/**
 * Graduations « rondes » comprises dans un intervalle, sans l'étendre.
 * @param {number} min
 * @param {number} max
 * @param {number} [count=5]
 * @returns {number[]}
 */
export function ticks(min, max, count = 5) {
  const span = Math.abs(max - min) || 1;
  return niceScale(min, max, count).ticks.filter((tick) => tick >= min - span * 1e-9 && tick <= max + span * 1e-9);
}

/**
 * « Décade » de rang `exponent`. En base 1024 (axe en octets), les rangs suivent les unités
 * binaires : 1, 10, 100, 1 ko (1 024), 10 ko, 100 ko, 1 Mo…
 */
function decade(exponent, base) {
  if (base !== 1024) return tidy(10 ** exponent);
  const group = Math.floor(exponent / 3);
  return tidy(10 ** (exponent - group * 3) * 1024 ** group);
}

/**
 * Bornes et graduations d'une échelle logarithmique : décades entières, complétées par 2 et 5
 * quand les valeurs couvrent moins de deux décades et demie.
 * @param {number} min Plus petite valeur strictement positive.
 * @param {number} max
 * @param {10|1024} [base=10] 1024 pour un axe en octets : 100 o, 1 ko, 10 ko, 100 ko, 1 Mo…
 * @returns {{min: number, max: number, ticks: number[], minor: number[]}}
 */
export function logScaleTicks(min, max, base = 10) {
  const lo = min > 0 && Number.isFinite(min) ? min : 1;
  const hi = max > lo && Number.isFinite(max) ? max : lo * 10;
  let first = Math.floor(Math.log10(lo) + 1e-9);
  while (decade(first, base) > lo * (1 + 1e-9)) first -= 1;
  let last = Math.max(first + 1, Math.ceil(Math.log10(hi) - 1e-9));
  while (last - 1 > first && decade(last - 1, base) >= hi * (1 - 1e-9)) last -= 1;
  while (decade(last, base) < hi * (1 - 1e-9)) last += 1;
  const major = [];
  const minor = [];
  for (let exponent = first; exponent <= last; exponent += 1) {
    major.push(decade(exponent, base));
    if (exponent < last) for (const factor of [2, 5]) minor.push(tidy(factor * decade(exponent, base)));
  }
  const detailed = Math.log10(hi / lo) < 2.5;
  return {
    min: major[0],
    max: major[major.length - 1],
    ticks: detailed ? [...major, ...minor].sort((a, b) => a - b) : major,
    minor: detailed ? [] : minor,
  };
}

/**
 * @typedef {((value: number) => number) & {invert: (pixel: number) => number, domain: number[], range: number[]}} Scale
 */

/**
 * Échelle linéaire valeur → pixel.
 * @param {[number, number]} domain
 * @param {[number, number]} range
 * @returns {Scale}
 */
export function linearScale(domain, range) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  const scale = (value) => r0 + (value - d0) * k;
  scale.invert = (pixel) => (k === 0 ? d0 : d0 + (pixel - r0) / k);
  scale.domain = domain;
  scale.range = range;
  return scale;
}

/**
 * Échelle logarithmique (base 10) valeur → pixel. Les valeurs ≤ 0 sont ramenées à la borne basse.
 * @param {[number, number]} domain Bornes strictement positives.
 * @param {[number, number]} range
 * @returns {Scale}
 */
export function logScale(domain, range) {
  const l0 = Math.log10(domain[0]);
  const l1 = Math.log10(domain[1]);
  const [r0, r1] = range;
  const k = l1 === l0 ? 0 : (r1 - r0) / (l1 - l0);
  const scale = (value) => r0 + (Math.log10(Math.max(value, domain[0])) - l0) * k;
  scale.invert = (pixel) => (k === 0 ? domain[0] : 10 ** (l0 + (pixel - r0) / k));
  scale.domain = domain;
  scale.range = range;
  return scale;
}

/**
 * Construit l'axe numérique d'un graphique : bornes rondes, graduations, échelle.
 * @param {number[]} values Valeurs à couvrir.
 * @param {[number, number]} range Pixels de départ et d'arrivée.
 * @param {{log?: boolean, count?: number, zero?: boolean, min?: number, max?: number, base?: 10|1024}} [options]
 *   `zero` force l'origine à 0 (barres) ; `min` / `max` imposent une borne ; `base: 1024` gradue un
 *   axe en octets sur des multiples ronds de ko, Mo… (à associer à `fmtBytes`).
 * @returns {{scale: Scale, ticks: number[], min: number, max: number}}
 */
export function buildAxis(values, range, { log = false, count = 5, zero = true, min, max, base = 10 } = {}) {
  const finite = values.filter((value) => Number.isFinite(value) && (!log || value > 0));
  let lo = min ?? (finite.length ? Math.min(...finite) : 0);
  let hi = max ?? (finite.length ? Math.max(...finite) : 1);
  if (log) {
    const axis = logScaleTicks(lo, hi, base);
    return { scale: logScale([axis.min, axis.max], range), ticks: axis.ticks, min: axis.min, max: axis.max };
  }
  if (zero && min === undefined) lo = Math.min(0, lo);
  if (zero && max === undefined) hi = Math.max(0, hi);
  const span = Math.max(Math.abs(lo), Math.abs(hi));
  const unit = base === 1024 && span >= 1024 ? 1024 ** Math.floor(Math.log(span) / Math.log(1024) + 1e-9) : 1;
  const scaled = niceScale(lo / unit, hi / unit, count);
  const axis = { min: tidy(scaled.min * unit), max: tidy(scaled.max * unit), ticks: scaled.ticks.map((tick) => tidy(tick * unit)) };
  const bounded = { min: min ?? axis.min, max: max ?? axis.max };
  return {
    scale: linearScale([bounded.min, bounded.max], range),
    ticks: axis.ticks.filter((tick) => tick >= bounded.min && tick <= bounded.max),
    ...bounded,
  };
}

/* --- Mesure de texte --------------------------------------------------------------- */

let measureContext = null;
const families = {};
const widthCache = new Map();

function fontFamily(mono) {
  const key = mono ? 'mono' : 'sans';
  families[key] ??= getComputedStyle(document.documentElement).getPropertyValue(mono ? '--font-mono' : '--font-sans').trim() || 'sans-serif';
  return families[key];
}

/**
 * Largeur en pixels d'un texte, pour placer les libellés sans chevauchement.
 * @param {string} text
 * @param {{size?: number, mono?: boolean, weight?: number}} [options] Police de l'interface (ou à chasse fixe), 11 px par défaut.
 * @returns {number}
 */
export function measureText(text, { size = 11, mono = false, weight = 400 } = {}) {
  const key = `${size}|${mono}|${weight}|${text}`;
  let width = widthCache.get(key);
  if (width === undefined) {
    measureContext ??= document.createElement('canvas').getContext('2d');
    measureContext.font = `${weight} ${size}px ${fontFamily(mono)}`;
    width = measureContext.measureText(String(text)).width;
    if (widthCache.size > 4000) widthCache.clear();
    widthCache.set(key, width);
  }
  return width;
}

/**
 * Tronque un texte avec « … » pour qu'il tienne dans une largeur.
 * @param {string} text
 * @param {number} maxWidth Largeur disponible en pixels.
 * @param {{size?: number, mono?: boolean, weight?: number}} [options]
 * @returns {string}
 */
export function fitText(text, maxWidth, options) {
  const full = String(text);
  if (measureText(full, options) <= maxWidth) return full;
  let low = 0;
  let high = full.length;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (measureText(`${full.slice(0, mid).trimEnd()}…`, options) <= maxWidth) low = mid;
    else high = mid - 1;
  }
  return low > 0 ? `${full.slice(0, low).trimEnd()}…` : '';
}

/**
 * Place des libellés sur un axe sans qu'ils se chevauchent : chacun reste au plus près de sa
 * position idéale, les voisins se repoussent, l'ensemble reste dans `[min, max]`.
 * @param {Array<{center: number, width: number}>} labels Triés par position croissante.
 * @param {number} min
 * @param {number} max
 * @param {number} [gap=6] Espace minimal entre deux libellés.
 * @returns {number[]} Bord gauche de chaque libellé.
 */
export function spreadLabels(labels, min, max, gap = 6) {
  const lefts = [];
  let edge = min;
  for (const label of labels) {
    const left = Math.max(edge, label.center - label.width / 2);
    lefts.push(left);
    edge = left + label.width + gap;
  }
  let limit = max;
  for (let i = labels.length - 1; i >= 0; i -= 1) {
    lefts[i] = Math.min(lefts[i], limit - labels[i].width);
    limit = lefts[i] - gap;
  }
  return lefts;
}

/** Exécute `callback` quand les polices embarquées sont prêtes (au plus tard après 400 ms). */
function whenFontsReady(callback) {
  if (!document.fonts || document.fonts.status === 'loaded') {
    callback();
    return;
  }
  let done = false;
  const run = () => {
    if (done) return;
    done = true;
    widthCache.clear();
    callback();
  };
  document.fonts.ready.then(run);
  window.setTimeout(run, 400);
}

/**
 * L'utilisateur a-t-il demandé de réduire les animations ?
 * @returns {boolean}
 */
export function prefersReducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
}

/* --- Info-bulle partagée ------------------------------------------------------------- */

const TIP_OFFSET = 14;
const TIP_MARGIN = 8;

let tip = null;
let tipOwner = null;
let tipKey = null;

function placeTip(x, y) {
  const { offsetWidth: width, offsetHeight: height } = tip;
  const viewportW = document.documentElement.clientWidth;
  const viewportH = window.innerHeight;
  let left = x + TIP_OFFSET;
  let top = y + TIP_OFFSET;
  if (left + width > viewportW - TIP_MARGIN) left = x - TIP_OFFSET - width;
  if (top + height > viewportH - TIP_MARGIN) top = y - TIP_OFFSET - height;
  left = Math.max(TIP_MARGIN, Math.min(left, viewportW - width - TIP_MARGIN));
  top = Math.max(TIP_MARGIN, top);
  tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
}

/**
 * Info-bulle unique partagée par tous les graphiques, la vue hexadécimale et les chronologies.
 * Elle suit le pointeur, reste dans la fenêtre et ne capte jamais la souris.
 */
export const chartTooltip = Object.freeze({
  /**
   * Affiche (ou déplace) l'info-bulle.
   * @param {Object} options
   * @param {*} options.owner Identité de l'appelant (son élément racine) : sert à `hide(owner)`.
   * @param {*} options.key Identité de la donnée survolée ; le contenu n'est reconstruit que si elle change.
   * @param {() => Node|Node[]|string} options.content Fabrique du contenu (voir `tipContent`).
   * @param {number} options.x Abscisse du pointeur dans la fenêtre (`event.clientX`).
   * @param {number} options.y Ordonnée du pointeur (`event.clientY`).
   * @returns {void}
   */
  show({ owner, key, content, x, y }) {
    tip ??= h('div.chart-tip', { role: 'status' });
    const fresh = !tip.isConnected;
    if (fresh) document.body.appendChild(tip);
    if (fresh || tipOwner !== owner || tipKey !== key) {
      clear(tip, content());
      tipOwner = owner;
      tipKey = key;
    }
    tip.dataset.moving = String(!fresh);
    placeTip(x, y);
  },

  /**
   * Masque l'info-bulle. Avec `owner`, seulement si elle lui appartient.
   * @param {*} [owner]
   * @returns {void}
   */
  hide(owner) {
    if (!tip || (owner !== undefined && owner !== tipOwner)) return;
    tip.remove();
    tipOwner = null;
    tipKey = null;
  },
});

window.addEventListener('scroll', () => chartTooltip.hide(), true);
window.addEventListener('hashchange', () => chartTooltip.hide());

/**
 * @typedef {Object} TipRow
 * @property {string} label
 * @property {string|Node} value Valeur déjà formatée (affichée en chiffres tabulaires).
 * @property {string} [color] Couleur CSS de la clé de légende.
 * @property {'line'|'dot'|'rect'} [shape='line'] Forme de la clé.
 * @property {boolean} [muted] Ligne secondaire (texte atténué).
 */

/**
 * Contenu normalisé d'une info-bulle : titre, sous-titre, lignes « clé · libellé · valeur », note.
 * @param {{title?: string|Node, subtitle?: string|Node, rows?: TipRow[], note?: string|Node}} content
 * @returns {Array<HTMLElement|null>}
 */
export function tipContent({ title, subtitle, rows = [], note }) {
  return [
    title || subtitle
      ? h('div.chart-tip__head', title ? h('span.chart-tip__title', title) : null, subtitle ? h('span.chart-tip__subtitle', subtitle) : null)
      : null,
    rows.length
      ? h(
          'div.chart-tip__rows',
          rows.map((row) =>
            h(
              'div.chart-tip__row',
              { class: { 'chart-tip__row--muted': row.muted } },
              row.color ? h('span.chart-tip__key', { class: `chart-tip__key--${row.shape ?? 'line'}`, style: { '--c': row.color } }) : h('span'),
              h('span.chart-tip__label', row.label),
              h('span.chart-tip__value.num', row.value),
            ),
          ),
        )
      : null,
    note ? h('div.chart-tip__note', note) : null,
  ];
}

/* --- Légende ------------------------------------------------------------------------- */

/**
 * @typedef {Object} LegendItem
 * @property {string} id
 * @property {string} label
 * @property {string} [color] Couleur CSS de la pastille.
 * @property {'dot'|'line'|'rect'|'hatch'} [shape='dot'] Forme de la clé (légende non interactive).
 * @property {string} [value] Valeur affichée après le libellé.
 */

/**
 * Légende d'un graphique. Avec `onToggle`, chaque entrée est une puce cliquable qui masque ou
 * affiche sa série (la dernière série visible ne peut pas être masquée).
 * @param {Object} props
 * @param {LegendItem[]} props.items
 * @param {string[]} [props.hidden] Identifiants des séries masquées.
 * @param {(hidden: string[]) => void} [props.onToggle] Rend la légende interactive.
 * @param {(id: string|null) => void} [props.onHover] Survol d'une entrée (mise en avant de la série).
 * @returns {HTMLElement & {set: (items: LegendItem[], hidden?: string[]) => void}}
 */
export function Legend({ items = [], hidden = [], onToggle, onHover } = {}) {
  const el = h('div.chart-legend', { role: onToggle ? 'group' : 'list', 'aria-label': 'Légende' });
  let off = new Set(hidden);

  function entry(item) {
    let node;
    if (onToggle) {
      node = Chip({
        label: item.label,
        color: item.color,
        selected: !off.has(item.id),
        onToggle: (selected) => {
          if (selected) off.delete(item.id);
          else off.add(item.id);
          if (off.size >= current.length) {
            off.delete(item.id);
            node.setSelected(true);
            return;
          }
          onToggle(Array.from(off));
        },
      });
    } else {
      node = h(
        'span.chart-legend__item',
        { role: 'listitem' },
        h('span.chart-legend__key', { class: `chart-legend__key--${item.shape ?? 'dot'}`, style: { '--c': item.color } }),
        h('span.chart-legend__label', item.label),
        item.value ? h('span.chart-legend__value.num', item.value) : null,
      );
    }
    if (onHover) {
      node.addEventListener('pointerenter', () => onHover(item.id));
      node.addEventListener('pointerleave', () => onHover(null));
    }
    return node;
  }

  let current = items;
  el.set = (next, nextHidden) => {
    current = next;
    if (nextHidden) off = new Set(nextHidden);
    clear(el, current.map(entry));
    el.hidden = current.length === 0;
  };
  el.set(items);
  return el;
}

/* --- Tableau accessible ---------------------------------------------------------------- */

/**
 * Jumeau tabulaire d'un graphique, réservé aux lecteurs d'écran (classe `sr-only`).
 * @param {string} caption
 * @param {string[]} head En-têtes de colonnes.
 * @param {Array<Array<string|number>>} rows
 * @returns {HTMLTableElement}
 */
export function dataTable(caption, head, rows) {
  return h(
    'table.sr-only',
    h('caption', caption),
    h('thead', h('tr', head.map((cell) => h('th', { scope: 'col' }, cell)))),
    h('tbody', rows.map((row) => h('tr', row.map((cell, index) => (index === 0 ? h('th', { scope: 'row' }, String(cell)) : h('td', String(cell))))))),
  );
}

/* --- Briques SVG ----------------------------------------------------------------------- */

/**
 * Rectangle dont seule l'extrémité « donnée » est arrondie (le pied reste droit sur la ligne de base).
 * @param {number} x
 * @param {number} y
 * @param {number} width
 * @param {number} height
 * @param {number} radius
 * @param {'top'|'right'|'bottom'|'left'} [end='top'] Côté arrondi.
 * @returns {string} Attribut `d` d'un `<path>`.
 */
export function roundedBar(x, y, width, height, radius, end = 'top') {
  const w = Math.max(0, width);
  const hgt = Math.max(0, height);
  const r = Math.max(0, Math.min(radius, end === 'top' || end === 'bottom' ? w / 2 : hgt / 2, end === 'top' || end === 'bottom' ? hgt : w));
  const f = (n) => Math.round(n * 100) / 100;
  if (end === 'top') return `M${f(x)},${f(y + hgt)}V${f(y + r)}Q${f(x)},${f(y)} ${f(x + r)},${f(y)}H${f(x + w - r)}Q${f(x + w)},${f(y)} ${f(x + w)},${f(y + r)}V${f(y + hgt)}Z`;
  if (end === 'bottom') return `M${f(x)},${f(y)}V${f(y + hgt - r)}Q${f(x)},${f(y + hgt)} ${f(x + r)},${f(y + hgt)}H${f(x + w - r)}Q${f(x + w)},${f(y + hgt)} ${f(x + w)},${f(y + hgt - r)}V${f(y)}Z`;
  if (end === 'right') return `M${f(x)},${f(y)}H${f(x + w - r)}Q${f(x + w)},${f(y)} ${f(x + w)},${f(y + r)}V${f(y + hgt - r)}Q${f(x + w)},${f(y + hgt)} ${f(x + w - r)},${f(y + hgt)}H${f(x)}Z`;
  return `M${f(x + w)},${f(y)}H${f(x + r)}Q${f(x)},${f(y)} ${f(x)},${f(y + r)}V${f(y + hgt - r)}Q${f(x)},${f(y + hgt)} ${f(x + r)},${f(y + hgt)}H${f(x + w)}Z`;
}

/**
 * Réserve d'éléments SVG réutilisés d'un rendu à l'autre (clé → élément), pour que les
 * transitions CSS s'appliquent quand une valeur change.
 * @returns {{take: (key: string, create: () => SVGElement) => SVGElement, commit: () => void}}
 */
export function elementPool() {
  let previous = new Map();
  let next = new Map();
  return {
    take(key, create) {
      const element = previous.get(key) ?? create();
      next.set(key, element);
      return element;
    },
    commit() {
      previous = next;
      next = new Map();
    },
  };
}

/**
 * Harmonise les libellés d'un axe : retire les zéros décimaux que tous partagent
 * (« 1,00 s », « 2,00 s » → « 1 s », « 2 s » ; « 0,10 ms », « 1,00 ms » → « 0,1 ms », « 1,0 ms »).
 * Les valeurs écrites sur les marques gardent, elles, leur précision complète.
 * @param {string[]} labels Libellés déjà formatés (virgule décimale).
 * @returns {string[]}
 */
export function tidyTickLabels(labels) {
  const decimals = labels.map((label) => /,(\d+)(?!.*\d)/.exec(label)?.[1] ?? null);
  const present = decimals.filter((part) => part !== null);
  if (!present.length) return labels;
  const spare = Math.min(...present.map((part) => part.length - part.replace(/0+$/, '').length));
  if (spare === 0) return labels;
  return labels.map((label, index) => {
    const part = decimals[index];
    if (part === null) return label;
    const kept = part.slice(0, part.length - spare);
    return label.replace(/,\d+(?!.*\d)/, kept ? `,${kept}` : '');
  });
}

/**
 * Trace l'axe des valeurs : filets horizontaux (ou verticaux) et libellés de graduation.
 * @param {Object} options
 * @param {Scale} options.scale
 * @param {number[]} options.ticks
 * @param {(value: number) => string} options.format
 * @param {'left'|'bottom'} options.side Côté où s'affichent les libellés.
 * @param {[number, number]} options.span Étendue des filets sur l'axe opposé, en pixels.
 * @param {number} options.at Position des libellés sur l'axe opposé, en pixels.
 * @param {number} [options.baseline] Valeur dont le filet est renforcé (0 pour des barres).
 * @returns {SVGGElement}
 */
export function valueAxis({ scale, ticks: values, format, side, span, at, baseline }) {
  const group = svg('g', { class: 'chart__axis' });
  const labels = tidyTickLabels(values.map((value) => format(value)));
  let lastEdge = -Infinity;
  values.forEach((value, index) => {
    const position = Math.round(scale(value)) + 0.5;
    const strong = baseline !== undefined && value === baseline;
    const label = labels[index];
    if (side === 'left') {
      group.append(
        svg('line', { class: strong ? 'chart__baseline' : 'chart__grid', x1: span[0], x2: span[1], y1: position, y2: position }),
        svg('text', { class: 'chart__tick chart__tick--num', x: at, y: position, dy: '0.34em', 'text-anchor': 'end' }, label),
      );
    } else {
      group.append(svg('line', { class: strong ? 'chart__baseline' : 'chart__grid', x1: position, x2: position, y1: span[0], y2: span[1] }));
      const width = measureText(label, { mono: true });
      const left = position - width / 2;
      if (left >= lastEdge + 10) {
        group.append(svg('text', { class: 'chart__tick chart__tick--num', x: position, y: at, 'text-anchor': 'middle' }, label));
        lastEdge = left + width;
      }
    }
  });
  return group;
}

/**
 * Largeur de la marge nécessaire aux libellés d'un axe vertical.
 * @param {number[]} values
 * @param {(value: number) => string} format
 * @returns {number}
 */
export function axisGutter(values, format) {
  const labels = tidyTickLabels(values.map((value) => format(value)));
  return Math.ceil(Math.max(0, ...labels.map((label) => measureText(label, { mono: true })))) + 10;
}

/* --- Châssis --------------------------------------------------------------------------- */

/**
 * @typedef {Object} ChartContext
 * @property {HTMLElement} el Racine du graphique.
 * @property {HTMLElement} plot Conteneur du tracé (le SVG y est inséré).
 * @property {number} width Largeur disponible en pixels.
 * @property {Object} state Propriétés courantes (fusion des `update`).
 * @property {'mount'|'update'|'resize'} reason Ce qui a provoqué le rendu (`mount` : premier tracé, animé).
 */

/**
 * @typedef {Object} ChartHooks
 * @property {(context: ChartContext) => void} draw Trace le graphique dans `context.plot`.
 * @property {(state: Object) => boolean} isEmpty Vrai quand il n'y a rien à tracer.
 * @property {(state: Object) => LegendItem[]|null} [legend] Entrées de la légende (séries masquables via `state.hidden`).
 * @property {boolean} [staticLegend] Légende purement descriptive : aucune série à masquer.
 * @property {(state: Object) => HTMLTableElement|null} [table] Jumeau tabulaire pour les lecteurs d'écran.
 * @property {(state: Object) => {count: number, activate: (index: number) => void, clear: () => void}|null} [navigator]
 *   Navigation au clavier : flèches pour parcourir les données, Échap pour quitter.
 * @property {() => void} [dispose] Nettoyage propre au graphique.
 */

/**
 * Châssis commun : crée la racine `figure.chart`, suit la largeur disponible (ResizeObserver),
 * gère les états vide et chargement, la légende, l'animation d'apparition et le clavier.
 * @param {string} kind Suffixe de classe (`chart--bar`).
 * @param {Object} props Propriétés initiales (dont `height`, `ariaLabel`, `empty`, `loading`, `hidden`).
 * @param {ChartHooks} hooks
 * @returns {HTMLElement & {update: (patch: Object) => void, setLoading: (on: boolean) => void, destroy: () => void, state: Object}}
 */
export function createChart(kind, props, hooks) {
  const state = { loading: false, hidden: [], ...props };
  const legendSlot = h('div.chart__legend');
  const plot = h('div.chart__plot');
  const tableSlot = h('div');
  const el = h('figure.chart', { class: `chart--${kind}`, role: 'group', 'aria-label': state.ariaLabel ?? null }, legendSlot, plot, tableSlot);

  let width = 0;
  let frame = 0;
  let pending = null;
  let drawn = false;
  let ready = false;
  let destroyed = false;
  let animateTimer = 0;
  let legend = null;
  let legendSignature = '';
  let navIndex = -1;

  /** La légende est reconstruite quand ses entrées changent, ou quand la page impose `hidden`. */
  const legendKey = (items) => `${JSON.stringify(items)}|${state.hidden.join(',')}`;

  function syncLegend() {
    const items = hooks.legend?.(state) ?? null;
    const signature = items ? legendKey(items) : '';
    if (signature !== legendSignature) {
      legendSignature = signature;
      if (!items) {
        legend = null;
        clear(legendSlot);
      } else if (!legend) {
        legend = Legend({
          items,
          hidden: state.hidden,
          onToggle: hooks.staticLegend
            ? undefined
            : (hidden) => {
                state.hidden = hidden;
                legendSignature = legendKey(hooks.legend(state));
                schedule('update');
              },
        });
        clear(legendSlot, legend);
      } else {
        legend.set(items, state.hidden);
      }
    }
    legendSlot.hidden = !items || !items.length;
  }

  function placeholder() {
    const height = typeof state.height === 'number' ? state.height : 200;
    return h(
      'div.chart__placeholder',
      { style: { minHeight: `${height}px` } },
      state.loading
        ? Skeleton({ variant: 'block', height })
        : EmptyState({
            size: 'sm',
            icon: state.empty?.icon ?? 'chart-column',
            title: state.empty?.title ?? 'Aucune donnée',
            text: state.empty?.text ?? 'Les mesures apparaîtront ici.',
          }),
    );
  }

  function render() {
    frame = 0;
    if (destroyed || !ready || !width) return;
    const reason = drawn ? (pending ?? 'update') : 'mount';
    pending = null;
    const empty = hooks.isEmpty(state);
    el.dataset.busy = String(Boolean(state.loading) && !empty);
    if (empty) {
      drawn = false;
      legendSlot.hidden = true;
      clear(tableSlot);
      clear(plot, placeholder());
      return;
    }
    if (plot.firstElementChild?.classList.contains('chart__placeholder')) clear(plot);
    syncLegend();
    if (reason === 'mount') {
      el.dataset.animate = 'in';
      window.clearTimeout(animateTimer);
      animateTimer = window.setTimeout(() => delete el.dataset.animate, 1400);
    } else if (reason === 'resize') {
      el.dataset.instant = '';
      requestAnimationFrame(() => delete el.dataset.instant);
    }
    hooks.draw({ el, plot, width, state, reason });
    if (hooks.table) clear(tableSlot, hooks.table(state));
    drawn = true;
  }

  /** Une mise à jour de données l'emporte sur un simple redimensionnement en attente. */
  function schedule(why) {
    if (why === 'update' || pending === null) pending = why;
    if (!frame) frame = requestAnimationFrame(render);
  }

  const observer = new ResizeObserver((entries) => {
    const next = Math.floor(entries[entries.length - 1].contentRect.width);
    if (next === width) return;
    width = next;
    schedule('resize');
  });
  observer.observe(plot);
  whenFontsReady(() => {
    ready = true;
    schedule('resize');
  });

  /* Clavier : le tracé est un seul arrêt de tabulation ; les flèches parcourent les données. */
  const offs = [];
  if (hooks.navigator) {
    plot.tabIndex = 0;
    offs.push(
      on(plot, 'keydown', (event) => {
        const nav = hooks.navigator(state);
        if (!nav || !nav.count) return;
        const moves = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
        if (event.key in moves) navIndex = (Math.max(navIndex, moves[event.key] > 0 ? -1 : 0) + moves[event.key] + nav.count) % nav.count;
        else if (event.key === 'Home') navIndex = 0;
        else if (event.key === 'End') navIndex = nav.count - 1;
        else if (event.key === 'Escape') {
          navIndex = -1;
          nav.clear();
          return;
        } else return;
        event.preventDefault();
        nav.activate(navIndex);
      }),
      on(plot, 'blur', () => {
        navIndex = -1;
        hooks.navigator(state)?.clear();
      }),
    );
  }

  el.state = state;
  el.update = (patch) => {
    if (destroyed) return;
    Object.assign(state, patch);
    if (patch && 'ariaLabel' in patch) el.setAttribute('aria-label', state.ariaLabel ?? '');
    schedule('update');
  };
  el.setLoading = (on_) => el.update({ loading: Boolean(on_) });
  el.destroy = () => {
    if (destroyed) return;
    destroyed = true;
    observer.disconnect();
    cancelAnimationFrame(frame);
    window.clearTimeout(animateTimer);
    offs.forEach((off) => off());
    chartTooltip.hide(el);
    hooks.dispose?.();
  };
  return el;
}

/**
 * Prépare le `<svg>` persistant d'un graphique : dimensions, insertion dans le conteneur.
 * @param {SVGSVGElement|null} existing SVG du rendu précédent, s'il existe.
 * @param {HTMLElement} plot
 * @param {number} width
 * @param {number} height
 * @returns {SVGSVGElement}
 */
export function svgRoot(existing, plot, width, height) {
  const root = existing ?? svg('svg', { class: 'chart__svg', 'aria-hidden': 'true', focusable: 'false' });
  root.setAttribute('width', String(width));
  root.setAttribute('height', String(height));
  root.setAttribute('viewBox', `0 0 ${width} ${height}`);
  if (root.parentNode !== plot) clear(plot, root);
  return root;
}

/**
 * Position, dans la fenêtre, d'un point exprimé en coordonnées du SVG (pour l'info-bulle au clavier).
 * @param {SVGSVGElement} root
 * @param {number} x
 * @param {number} y
 * @returns {{x: number, y: number}}
 */
export function clientPoint(root, x, y) {
  const rect = root.getBoundingClientRect();
  return { x: rect.left + x, y: rect.top + y };
}
