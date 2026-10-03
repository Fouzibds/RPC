/**
 * Éléments flottants : calcul de position par rapport à une ancre (avec retournement et
 * maintien dans la fenêtre), cycle de vie d'un popover et menu déroulant.
 */

import { h, on } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { pushLayer } from '../../core/layers.js';
import { Kbd } from './badges.js';

const MARGIN = 8;

/**
 * Positionne `floating` (en `position: fixed`) autour de `anchor`.
 * @param {HTMLElement} floating Élément déjà inséré dans le document.
 * @param {Element} anchor
 * @param {{placement?: string, offset?: number, matchWidth?: boolean}} [options]
 *   `placement` : `top|bottom|left|right` suivi au besoin de `-start|-end` (défaut `bottom-start`) ;
 *   `offset` : écart en pixels (6) ; `matchWidth` : largeur minimale = celle de l'ancre.
 * @returns {string} Le placement effectivement retenu (après retournement éventuel).
 */
export function place(floating, anchor, { placement = 'bottom-start', offset = 6, matchWidth = false } = {}) {
  const rect = anchor.getBoundingClientRect();
  if (matchWidth) floating.style.minWidth = `${Math.round(rect.width)}px`;
  floating.style.position = 'fixed';
  floating.style.left = '0px';
  floating.style.top = '0px';
  floating.style.maxHeight = '';
  const { offsetWidth: width, offsetHeight: height } = floating;
  const viewportW = document.documentElement.clientWidth;
  const viewportH = window.innerHeight;

  let [side, align = 'center'] = placement.split('-');
  const room = {
    top: rect.top - offset - MARGIN,
    bottom: viewportH - rect.bottom - offset - MARGIN,
    left: rect.left - offset - MARGIN,
    right: viewportW - rect.right - offset - MARGIN,
  };
  const opposite = { top: 'bottom', bottom: 'top', left: 'right', right: 'left' };
  const needed = side === 'top' || side === 'bottom' ? height : width;
  if (room[side] < needed && room[opposite[side]] > room[side]) side = opposite[side];

  let left;
  let top;
  if (side === 'top' || side === 'bottom') {
    top = side === 'top' ? rect.top - offset - height : rect.bottom + offset;
    left = align === 'start' ? rect.left : align === 'end' ? rect.right - width : rect.left + rect.width / 2 - width / 2;
    if (height > room[side]) floating.style.maxHeight = `${Math.max(120, Math.floor(room[side]))}px`;
    if (side === 'top' && height > room[side]) top = MARGIN;
  } else {
    left = side === 'left' ? rect.left - offset - width : rect.right + offset;
    top = align === 'start' ? rect.top : align === 'end' ? rect.bottom - height : rect.top + rect.height / 2 - height / 2;
  }
  left = Math.max(MARGIN, Math.min(left, viewportW - width - MARGIN));
  top = Math.max(MARGIN, Math.min(top, viewportH - Math.min(height, viewportH - 2 * MARGIN) - MARGIN));

  floating.style.left = `${Math.round(left)}px`;
  floating.style.top = `${Math.round(top)}px`;
  floating.dataset.side = side;
  return `${side}-${align}`;
}

/**
 * Ouvre un popover ancré : inséré dans `<body>`, repositionné au défilement et au
 * redimensionnement, fermé par Échap ou par un clic à l'extérieur.
 * @param {Element} anchor
 * @param {HTMLElement} content Élément à afficher (reçoit la classe `popover`).
 * @param {{placement?: string, offset?: number, matchWidth?: boolean, onClose?: () => void}} [options]
 * @returns {{el: HTMLElement, close: () => void, update: () => void}}
 */
export function openPopover(anchor, content, { placement = 'bottom-start', offset = 6, matchWidth = false, onClose } = {}) {
  content.classList.add('popover');
  document.body.appendChild(content);
  const update = () => place(content, anchor, { placement, offset, matchWidth });
  update();

  let closed = false;
  const cleanups = [
    on(window, 'resize', update),
    on(
      window,
      'scroll',
      (event) => {
        if (!(event.target instanceof Node) || !content.contains(event.target)) update();
      },
      true,
    ),
    on(
      document,
      'pointerdown',
      (event) => {
        if (!content.contains(event.target) && !anchor.contains(event.target)) close();
      },
      true,
    ),
    pushLayer({ onEscape: () => close() }),
  ];

  function close() {
    if (closed) return;
    closed = true;
    cleanups.forEach((cleanup) => cleanup());
    content.remove();
    onClose?.();
  }

  return { el: content, close, update };
}

/**
 * @typedef {Object} MenuItem
 * @property {string} label
 * @property {string} [icon]
 * @property {string} [hint] Texte secondaire à droite.
 * @property {string} [kbd] Raccourci affiché à droite (`'mod+k'`).
 * @property {'danger'} [tone]
 * @property {boolean} [disabled]
 * @property {() => void} [onSelect]
 */

/**
 * Ouvre un menu déroulant ancré à un élément (flèches, Entrée, Échap).
 * @param {HTMLElement} anchor Bouton déclencheur (reçoit le focus à la fermeture).
 * @param {Array<MenuItem|'divider'|{heading: string}>} items
 * @param {{placement?: string, minWidth?: number}} [options]
 * @returns {{close: () => void}}
 */
export function openMenu(anchor, items, { placement = 'bottom-end', minWidth = 200 } = {}) {
  const menu = h('div.menu', { role: 'menu', tabIndex: -1, style: { minWidth: `${minWidth}px` } });
  let popover = null;

  const buttons = [];
  for (const item of items) {
    if (item === 'divider') {
      menu.appendChild(h('div.menu__divider', { role: 'separator' }));
    } else if (item.heading) {
      menu.appendChild(h('div.menu__heading.t-label', item.heading));
    } else {
      const button = h(
        'button.menu__item',
        {
          type: 'button',
          role: 'menuitem',
          disabled: Boolean(item.disabled),
          class: { 'menu__item--danger': item.tone === 'danger' },
          onClick: () => {
            popover.close();
            item.onSelect?.();
          },
        },
        item.icon ? icon(item.icon, { size: 15 }) : null,
        h('span.menu__label', item.label),
        item.hint ? h('span.menu__hint', item.hint) : null,
        item.kbd ? Kbd(item.kbd) : null,
      );
      buttons.push(button);
      menu.appendChild(button);
    }
  }

  menu.addEventListener('keydown', (event) => {
    const enabled = buttons.filter((button) => !button.disabled);
    const index = enabled.indexOf(document.activeElement);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      enabled[(index + delta + enabled.length) % enabled.length]?.focus();
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      enabled[event.key === 'Home' ? 0 : enabled.length - 1]?.focus();
    } else if (event.key === 'Tab') {
      popover.close();
    }
  });

  anchor.setAttribute('aria-expanded', 'true');
  popover = openPopover(anchor, menu, {
    placement,
    onClose: () => {
      anchor.setAttribute('aria-expanded', 'false');
      if (document.activeElement === document.body || menu.contains(document.activeElement)) anchor.focus({ preventScroll: true });
    },
  });
  (buttons.find((button) => !button.disabled) ?? menu).focus({ preventScroll: true });
  return { close: popover.close };
}
