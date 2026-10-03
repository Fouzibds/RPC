/**
 * Notifications éphémères, empilées en bas à droite.
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';

const ICONS = { success: 'circle-check', error: 'circle-alert', info: 'info', warn: 'triangle-alert' };
const DURATIONS = { success: 4000, info: 4500, warn: 6000, error: 7000 };
const MAX_VISIBLE = 4;
const LEAVE_MS = 180;

let region = null;
const live = [];

function host() {
  if (!region) {
    region = h('div.toast-region', { role: 'region', 'aria-label': 'Notifications', 'aria-live': 'polite' });
    document.body.appendChild(region);
  }
  return region;
}

/**
 * @typedef {Object} ToastOptions
 * @property {string} [description] Seconde ligne, plus discrète.
 * @property {number} [duration] Millisecondes avant fermeture automatique ; `0` = reste jusqu'au clic.
 * @property {{label: string, onClick: () => void}} [action] Bouton d'action (ferme la notification).
 */

/**
 * @typedef {Object} ToastHandle
 * @property {() => void} dismiss Ferme la notification.
 */

function show(tone, title, { description = '', duration, action } = {}) {
  let timer = 0;
  let remaining = duration ?? DURATIONS[tone];
  let startedAt = 0;
  let gone = false;

  const el = h(
    'div.toast',
    {
      dataset: { tone },
      role: tone === 'error' ? 'alert' : 'status',
      onPointerenter: pause,
      onPointerleave: resume,
      onFocusin: pause,
      onFocusout: resume,
    },
    h('span.toast__icon', { 'aria-hidden': 'true' }, icon(ICONS[tone], { size: 16 })),
    h('div.toast__body', h('p.toast__title', title), description ? h('p.toast__description', description) : null),
    action
      ? h(
          'button.toast__action',
          {
            type: 'button',
            onClick: () => {
              dismiss();
              action.onClick?.();
            },
          },
          action.label,
        )
      : null,
    h('button.toast__close', { type: 'button', 'aria-label': 'Fermer la notification', onClick: dismiss }, icon('x', { size: 14 })),
  );

  function pause() {
    if (!timer) return;
    window.clearTimeout(timer);
    timer = 0;
    remaining -= performance.now() - startedAt;
  }

  function resume() {
    if (gone || timer || remaining <= 0 || !Number.isFinite(remaining)) return;
    startedAt = performance.now();
    timer = window.setTimeout(dismiss, Math.max(600, remaining));
  }

  function dismiss() {
    if (gone) return;
    gone = true;
    window.clearTimeout(timer);
    const index = live.indexOf(handle);
    if (index !== -1) live.splice(index, 1);
    el.dataset.leaving = 'true';
    window.setTimeout(() => el.remove(), LEAVE_MS);
  }

  const handle = { dismiss };
  host().appendChild(el);
  live.push(handle);
  while (live.length > MAX_VISIBLE) live[0].dismiss();
  if (remaining > 0) resume();
  return handle;
}

/**
 * Notifications. Chaque méthode prend un titre court et des options, et renvoie `{ dismiss }`.
 *
 *     toast.success('Réseau : préréglage « WAN » appliqué');
 *     toast.error('Appel refusé', { description: 'INVALID_ARGUMENT : delta manquant', action: { label: 'Réessayer', onClick } });
 */
export const toast = Object.freeze({
  /** @param {string} title @param {ToastOptions} [options] @returns {ToastHandle} */
  success: (title, options) => show('success', title, options),
  /** @param {string} title @param {ToastOptions} [options] @returns {ToastHandle} */
  error: (title, options) => show('error', title, options),
  /** @param {string} title @param {ToastOptions} [options] @returns {ToastHandle} */
  info: (title, options) => show('info', title, options),
  /** @param {string} title @param {ToastOptions} [options] @returns {ToastHandle} */
  warn: (title, options) => show('warn', title, options),
  /** Ferme toutes les notifications visibles. */
  clear: () => {
    for (const handle of Array.from(live)) handle.dismiss();
  },
});
