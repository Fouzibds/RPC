/**
 * Retours d'état : EmptyState, Skeleton, Progress, Callout.
 */

import { h, append, clear, splitArgs } from '../../core/dom.js';
import { fmtPercent } from '../../core/format.js';
import { icon } from '../../core/icons.js';

const CALLOUT_ICONS = { info: 'info', success: 'circle-check', warning: 'triangle-alert', danger: 'circle-alert', accent: 'sparkles', neutral: 'info' };

/**
 * État vide : icône, titre, texte, action.
 * @param {Object} [props]
 * @param {string} [props.icon='inbox']
 * @param {string} [props.title]
 * @param {string|Node} [props.text]
 * @param {Node|Node[]} [props.action] Bouton(s) sous le texte.
 * @param {'sm'|'md'|'lg'} [props.size='md'] `sm` dans une carte ou un tableau, `lg` pour une page entière.
 * @param {'neutral'|'accent'|'warning'|'danger'} [props.tone='neutral'] Teinte de l'icône.
 * @returns {HTMLElement}
 */
export function EmptyState({ icon: iconName = 'inbox', title = '', text = '', action, size = 'md', tone = 'neutral' } = {}) {
  const iconSize = size === 'lg' ? 22 : size === 'sm' ? 16 : 18;
  return h(
    'div.empty',
    { class: `empty--${size}`, dataset: { tone } },
    h('div.empty__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: iconSize })),
    title ? h('p.empty__title', title) : null,
    text ? h('p.empty__text', text) : null,
    action ? h('div.empty__action', action) : null,
  );
}

/**
 * Squelette de chargement.
 * @param {Object} [props]
 * @param {'text'|'block'|'circle'} [props.variant='text']
 * @param {number|string} [props.width] Largeur (px ou CSS) ; 100 % par défaut.
 * @param {number|string} [props.height] Hauteur (px ou CSS) ; selon la variante par défaut.
 * @param {number} [props.lines=1] Nombre de lignes (variante `text`) ; la dernière est plus courte.
 * @returns {HTMLElement}
 */
export function Skeleton({ variant = 'text', width, height, lines = 1 } = {}) {
  const css = (value) => (typeof value === 'number' ? `${value}px` : value);
  if (variant === 'text' && lines > 1) {
    return h(
      'div.skeleton-lines',
      { 'aria-hidden': 'true', style: { width: css(width) } },
      Array.from({ length: lines }, (_, index) => h('span.skeleton.skeleton--text', { style: { width: index === lines - 1 ? '62%' : '100%' } })),
    );
  }
  return h('span.skeleton', { class: `skeleton--${variant}`, 'aria-hidden': 'true', style: { width: css(width), height: css(height) } });
}

/**
 * Barre de progression.
 * @param {Object} [props]
 * @param {number} [props.value=0] Avancement entre 0 et 1.
 * @param {string} [props.label] Libellé à gauche, au-dessus de la barre.
 * @param {string} [props.detail] Texte à droite (par défaut le pourcentage).
 * @param {boolean} [props.indeterminate] Durée inconnue : barre animée en continu.
 * @param {'accent'|'success'|'warning'|'danger'|'info'} [props.tone='accent']
 * @param {string} [props.color] Couleur CSS de la barre (ex. `var(--proto-grpc)`), prioritaire sur `tone`.
 * @param {'sm'|'md'} [props.size='md'] Épaisseur 4 ou 6 px.
 * @returns {HTMLElement & {set: (next: {value?: number, label?: string, detail?: string, indeterminate?: boolean, tone?: string}) => void}}
 */
export function Progress({ value = 0, label = '', detail, indeterminate = false, tone = 'accent', color, size = 'md' } = {}) {
  const labelEl = h('span.progress__label');
  const detailEl = h('span.progress__detail.num');
  const head = h('div.progress__head', labelEl, detailEl);
  const fill = h('span.progress__fill');
  const bar = h('div.progress__bar', { role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100 }, fill);
  const el = h('div.progress', { class: `progress--${size}`, style: color ? { '--progress-color': color } : null }, head, bar);

  const state = { value, label, detail, indeterminate, tone };
  el.set = (next) => {
    Object.assign(state, next);
    const ratio = Math.min(1, Math.max(0, Number(state.value) || 0));
    el.dataset.tone = state.tone;
    el.dataset.indeterminate = String(Boolean(state.indeterminate));
    fill.style.setProperty('--ratio', String(ratio));
    clear(labelEl, state.label);
    detailEl.textContent = state.detail ?? (state.indeterminate ? '' : fmtPercent(ratio, { decimals: 0 }));
    head.hidden = !state.label && !detailEl.textContent;
    if (state.indeterminate) bar.removeAttribute('aria-valuenow');
    else bar.setAttribute('aria-valuenow', String(Math.round(ratio * 100)));
    if (state.label) bar.setAttribute('aria-label', state.label);
  };
  el.set({});
  return el;
}

/**
 * Encadré d'information.
 * @param {Object} props
 * @param {'info'|'success'|'warning'|'danger'|'accent'|'neutral'} [props.tone='info']
 * @param {string} [props.title]
 * @param {string|Node} [props.text] Texte (ou passez des enfants).
 * @param {string} [props.icon] Icône (par défaut selon le ton).
 * @param {Node|Node[]} [props.actions] Boutons sous le texte.
 * @param {...*} children Contenu supplémentaire.
 * @returns {HTMLElement}
 */
export function Callout(props, ...children) {
  const [{ tone = 'info', title, text, icon: iconName, actions }, content] = splitArgs(props, children);
  return h(
    'div.callout',
    { dataset: { tone }, role: tone === 'danger' || tone === 'warning' ? 'alert' : 'note' },
    h('span.callout__icon', { 'aria-hidden': 'true' }, icon(iconName ?? CALLOUT_ICONS[tone] ?? 'info', { size: 16 })),
    append(
      h('div.callout__body', title ? h('p.callout__title', title) : null, text ? h('p.callout__text', text) : null),
      content.length ? h('div.callout__text', content) : null,
      actions ? h('div.callout__actions', actions) : null,
    ),
  );
}
