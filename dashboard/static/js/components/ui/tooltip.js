/**
 * Info-bulle : une seule bulle partagée, attachée à n'importe quel élément.
 */

import { h, clear, on } from '../../core/dom.js';
import { place } from './floating.js';
import { Kbd } from './badges.js';

const SHOW_DELAY_MS = 380;
const WARM_WINDOW_MS = 600;

let bubble = null;
let showTimer = 0;
let owner = null;
let lastHiddenAt = 0;
let stopWatching = null;

function hide() {
  window.clearTimeout(showTimer);
  if (!owner) return;
  owner = null;
  lastHiddenAt = performance.now();
  stopWatching?.();
  stopWatching = null;
  bubble?.remove();
}

/** Ferme la bulle dès que le pointeur quitte la cible, même si celle-ci a été retirée du document. */
function watchPointer() {
  stopWatching = on(document, 'pointermove', (event) => {
    if (!owner || !document.contains(owner) || !owner.contains(event.target)) hide();
  });
}

function show(target, getContent, placement, kbd) {
  const content = typeof getContent === 'function' ? getContent() : getContent;
  if (content === null || content === undefined || content === '') return;
  if (!bubble) bubble = h('div.tooltip', { role: 'tooltip' });
  clear(bubble, h('span.tooltip__text', content), kbd ? Kbd(kbd, { size: 'sm' }) : null);
  document.body.appendChild(bubble);
  owner = target;
  place(bubble, target, { placement, offset: 8 });
  stopWatching?.();
  if (target.matches(':hover')) watchPointer();
}

/**
 * Attache une info-bulle à un élément (survol après un court délai, ou focus clavier).
 * @param {Element} target
 * @param {string|Node|(() => string|Node|null)} content Texte, nœud, ou fonction évaluée à chaque affichage.
 * @param {{placement?: string, kbd?: string, delay?: number}} [options]
 *   `placement` : `top` (défaut), `bottom`, `left`, `right` (+ `-start|-end`), retourné si la place manque ;
 *   `kbd` : raccourci affiché à côté du texte (`'mod+k'`).
 * @returns {() => void} Détache l'info-bulle.
 */
export function Tooltip(target, content, { placement = 'top', kbd = '', delay = SHOW_DELAY_MS } = {}) {
  const open = () => show(target, content, placement, kbd);
  const cleanups = [
    on(target, 'pointerenter', (event) => {
      if (event.pointerType === 'touch') return;
      window.clearTimeout(showTimer);
      const warm = performance.now() - lastHiddenAt < WARM_WINDOW_MS;
      showTimer = window.setTimeout(open, warm ? 60 : delay);
    }),
    on(target, 'pointerleave', hide),
    on(target, 'pointerdown', hide),
    on(target, 'focusin', () => {
      if (target.matches(':focus-visible') || target.querySelector(':focus-visible')) open();
    }),
    on(target, 'focusout', hide),
    on(target, 'keydown', (event) => {
      if (event.key === 'Escape') hide();
    }),
  ];
  return () => {
    if (owner === target) hide();
    cleanups.forEach((cleanup) => cleanup());
  };
}

window.addEventListener('scroll', hide, true);
window.addEventListener('hashchange', hide);
