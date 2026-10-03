/**
 * Affichage de données : CountUp, Stat, KeyValue, Stepper.
 */

import { h, clear } from '../../core/dom.js';
import { fmtNumber, splitUnit } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { CopyButton } from './buttons.js';

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

/**
 * Nombre animé : glisse de l'ancienne valeur à la nouvelle.
 * @param {Object} [props]
 * @param {number} [props.value=0]
 * @param {(value: number) => string} [props.format] Formatage (par défaut `fmtNumber`).
 * @param {number} [props.duration=600] Durée de l'animation, en millisecondes.
 * @param {(text: string) => void} [props.onFrame] Reçoit le texte à chaque image (au lieu d'écrire dans l'élément).
 * @returns {HTMLSpanElement & {value: number, set: (value: number, options?: {animate?: boolean}) => void}}
 */
export function CountUp({ value = 0, format = (v) => fmtNumber(v), duration = 600, onFrame } = {}) {
  const el = h('span.countup.num');
  let shown = Number(value) || 0;
  let target = shown;
  let frame = 0;

  function paint(v) {
    const text = format(v);
    if (onFrame) onFrame(text);
    else el.textContent = text;
  }

  el.set = (next, { animate = true } = {}) => {
    const goal = Number(next);
    if (!Number.isFinite(goal)) {
      cancelAnimationFrame(frame);
      target = NaN;
      paint(next);
      return;
    }
    const from = Number.isFinite(shown) ? shown : goal;
    target = goal;
    cancelAnimationFrame(frame);
    if (!animate || reducedMotion.matches || from === goal || (!onFrame && !el.isConnected)) {
      shown = goal;
      paint(goal);
      return;
    }
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - (1 - t) ** 4;
      shown = from + (goal - from) * eased;
      paint(t === 1 ? goal : shown);
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
  };
  Object.defineProperty(el, 'value', { get: () => target });
  paint(shown);
  return el;
}

/**
 * Indicateur chiffré : libellé, grande valeur tabulaire, unité, variation ou aide, et un
 * emplacement à droite de la valeur pour une mini-courbe.
 * @param {Object} props
 * @param {string} props.label
 * @param {number|string} [props.value] Nombre (animé si `format` est fourni) ou texte déjà formaté.
 * @param {(value: number) => string} [props.format] Ex. `fmtMs`, `fmtBytes` : l'unité est séparée automatiquement.
 * @param {string} [props.unit] Unité explicite (prioritaire sur celle déduite de `format`).
 * @param {{label: string, tone?: 'success'|'danger'|'warning'|'neutral', direction?: 'up'|'down'}} [props.delta] Variation.
 * @param {string|Node} [props.hint] Ligne d'aide sous la valeur.
 * @param {string} [props.icon] Icône du libellé.
 * @param {'local'|'custom'|'grpc'|'rest'} [props.protocol] Pastille à la couleur du protocole.
 * @param {'md'|'lg'} [props.size='md'] Valeur en 28 ou 40 px.
 * @param {Node} [props.trailing] Contenu de l'emplacement de droite (mini-courbe…).
 * @returns {HTMLElement & {trailing: HTMLElement, update: (next: {value?: number|string, unit?: string, delta?: Object|null, hint?: string|Node|null, label?: string}) => void}}
 */
export function Stat({ label, value, format, unit, delta, hint, icon: iconName, protocol: protocolId, size = 'md', trailing } = {}) {
  const labelText = h('span.stat__label-text', label);
  const valueEl = h('span.stat__number.num');
  const unitEl = h('span.stat__unit');
  const deltaEl = h('span.stat__delta');
  const hintEl = h('span.stat__hint');
  const trailingEl = h('div.stat__trailing', trailing ?? null);
  let explicitUnit = unit;

  const counter = CountUp({
    value: typeof value === 'number' ? value : 0,
    format: format ?? ((v) => fmtNumber(v)),
    onFrame: (text) => {
      const parts = splitUnit(text);
      valueEl.textContent = parts.value;
      setUnit(explicitUnit ?? parts.unit);
    },
  });

  function setUnit(text) {
    unitEl.textContent = text ?? '';
    unitEl.hidden = !text;
  }

  function setValue(next, animate) {
    if (typeof next === 'number' && Number.isFinite(next)) {
      counter.set(next, { animate: animate && el.isConnected });
    } else {
      counter.set(NaN, { animate: false });
      valueEl.textContent = next === null || next === undefined || Number.isNaN(next) ? '—' : String(next);
      setUnit(explicitUnit);
    }
  }

  function setDelta(next) {
    deltaEl.replaceChildren();
    deltaEl.hidden = !next;
    if (!next) return;
    deltaEl.dataset.tone = next.tone ?? 'neutral';
    if (next.direction) deltaEl.appendChild(icon(next.direction === 'up' ? 'arrow-up-right' : 'arrow-down-right', { size: 12, stroke: 2 }));
    deltaEl.appendChild(document.createTextNode(next.label));
  }

  function setHint(next) {
    clear(hintEl, next);
    hintEl.hidden = next === null || next === undefined || next === '';
  }

  const foot = h('div.stat__foot', deltaEl, hintEl);
  const el = h(
    'div.stat',
    { class: `stat--${size}` },
    h(
      'div.stat__label.t-label',
      protocolId ? h('span.stat__swatch', { style: { background: protocol(protocolId).color }, 'aria-hidden': 'true' }) : null,
      iconName ? icon(iconName, { size: 13 }) : null,
      labelText,
    ),
    h('div.stat__row', h('div.stat__value', valueEl, unitEl), trailingEl),
    foot,
  );
  trailingEl.hidden = !trailing;

  el.trailing = trailingEl;
  el.update = (next) => {
    if (next.label !== undefined) clear(labelText, next.label);
    if (next.unit !== undefined) {
      explicitUnit = next.unit;
      setUnit(explicitUnit);
    }
    if (next.value !== undefined) setValue(next.value, true);
    if (next.delta !== undefined) setDelta(next.delta);
    if (next.hint !== undefined) setHint(next.hint);
    foot.hidden = deltaEl.hidden && hintEl.hidden;
  };
  setValue(value, false);
  setDelta(delta);
  setHint(hint);
  foot.hidden = deltaEl.hidden && hintEl.hidden;
  return el;
}

/**
 * Liste clé / valeur.
 * @param {Object} props
 * @param {Array<{label: string, value: string|number|Node, mono?: boolean, copy?: boolean|string, tone?: string}>} props.items
 *   `mono` : valeur à chasse fixe ; `copy` : bouton copier (vrai = copie la valeur affichée, chaîne = texte à copier) ;
 *   `tone` : `success|warning|danger|info|accent` colore la valeur.
 * @param {1|2|3} [props.columns=1] Nombre de paires par ligne.
 * @param {'row'|'stacked'} [props.layout='row'] Libellé à gauche de la valeur, ou au-dessus.
 * @param {boolean} [props.dense]
 * @returns {HTMLElement & {set: (items: Array<Object>) => void}}
 */
export function KeyValue({ items = [], columns = 1, layout = 'row', dense = false } = {}) {
  const el = h('dl.kv', { class: [`kv--${layout}`, { 'kv--dense': dense }], style: { '--kv-columns': columns } });
  el.set = (next) => {
    el.replaceChildren();
    for (const item of next) {
      const value = item.value === null || item.value === undefined || item.value === '' ? '—' : item.value;
      const copyText = typeof item.copy === 'string' ? item.copy : value instanceof Node ? value.textContent : String(value);
      el.appendChild(
        h(
          'div.kv__row',
          h('dt.kv__label', item.label),
          h(
            'dd.kv__value',
            { class: [{ num: item.mono }, item.tone ? `fg-${item.tone}` : null] },
            h('span.kv__text', value),
            item.copy ? CopyButton({ text: copyText, label: `Copier ${item.label.toLowerCase()}` }) : null,
          ),
        ),
      );
    }
  };
  el.set(items);
  return el;
}

/**
 * Étapes numérotées avec états.
 * @param {Object} props
 * @param {Array<{title: string, description?: string|Node, state?: 'pending'|'active'|'done'|'error'}>} props.steps
 * @param {'horizontal'|'vertical'} [props.orientation='horizontal']
 * @returns {HTMLElement & {setState: (index: number, state: 'pending'|'active'|'done'|'error') => void, setStep: (index: number) => void}}
 *   `setStep(i)` marque les étapes avant `i` comme faites, `i` comme active, les suivantes en attente.
 */
export function Stepper({ steps = [], orientation = 'horizontal' } = {}) {
  const items = steps.map((step, index) =>
    h(
      'li.stepper__step',
      h('span.stepper__marker', h('span.stepper__number.num', String(index + 1)), h('span.stepper__glyph')),
      h('div.stepper__text', h('span.stepper__title', step.title), step.description ? h('span.stepper__description', step.description) : null),
    ),
  );
  const el = h('ol.stepper', { class: `stepper--${orientation}` }, items);

  el.setState = (index, state) => {
    const item = items[index];
    if (!item) return;
    item.dataset.state = state;
    if (state === 'active') item.setAttribute('aria-current', 'step');
    else item.removeAttribute('aria-current');
    const glyph = item.querySelector('.stepper__glyph');
    glyph.replaceChildren();
    if (state === 'done') glyph.appendChild(icon('check', { size: 12, stroke: 2.5 }));
    if (state === 'error') glyph.appendChild(icon('x', { size: 12, stroke: 2.5 }));
  };
  el.setStep = (active) => {
    items.forEach((_, index) => el.setState(index, index < active ? 'done' : index === active ? 'active' : 'pending'));
  };
  steps.forEach((step, index) => el.setState(index, step.state ?? 'pending'));
  return el;
}
