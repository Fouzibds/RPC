/**
 * Fenêtres modales et tiroirs latéraux : piège de focus, fermeture par Échap ou clic
 * sur le voile, restitution du focus, verrou de défilement.
 */

import { h, append, clear, uid } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { lockScroll, pushLayer, trapFocus } from '../core/layers.js';
import { Button } from './ui/buttons.js';

const LEAVE_MS = 160;

function closeButton(onClick) {
  return h('button.btn.btn--ghost.btn--icon.btn--md', { type: 'button', 'aria-label': 'Fermer', onClick }, icon('x', { size: 16 }));
}

function resolve(part, close) {
  return typeof part === 'function' ? part(close) : part;
}

/**
 * @typedef {Object} OverlayHandle
 * @property {HTMLElement} el Le panneau (fenêtre ou tiroir).
 * @property {HTMLElement} body Le conteneur du contenu.
 * @property {() => void} close Ferme et nettoie.
 * @property {(title: string) => void} setTitle
 */

/**
 * Ouvre une fenêtre modale centrée.
 * @param {Object} options
 * @param {string} options.title
 * @param {string} [options.description] Sous-titre sous le titre.
 * @param {string} [options.icon] Icône dans l'en-tête.
 * @param {Node|Node[]|string|((close: () => void) => Node|Node[])} [options.body]
 * @param {Node|Node[]|((close: () => void) => Node|Node[])} [options.footer] Boutons du pied (alignés à droite).
 * @param {'sm'|'md'|'lg'|'xl'} [options.size='md'] Largeurs 400 / 520 / 720 / 960 px.
 * @param {boolean} [options.dismissible=true] Échap, clic sur le voile et croix ferment la fenêtre.
 * @param {() => void} [options.onClose]
 * @returns {OverlayHandle}
 */
export function openModal({ title, description, icon: iconName, body, footer, size = 'md', dismissible = true, onClose } = {}) {
  const titleId = uid('modal-title');
  let closed = false;

  const titleEl = h('h2.modal__title', { id: titleId }, title);
  const bodyEl = h('div.modal__body.scroll-y');
  const panel = h(
    'div.modal',
    { class: `modal--${size}`, role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId, tabIndex: -1 },
    h(
      'header.modal__header',
      iconName ? h('span.modal__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: 18 })) : null,
      h('div.modal__heading', titleEl, description ? h('p.modal__description', description) : null),
      dismissible ? closeButton(() => close()) : null,
    ),
    bodyEl,
  );
  const backdrop = h(
    'div.overlay.overlay--modal',
    {
      onPointerdown: (event) => {
        if (dismissible && event.target === backdrop) close();
      },
    },
    panel,
  );

  function close() {
    if (closed) return;
    closed = true;
    popLayer();
    releaseFocus();
    unlock();
    backdrop.dataset.leaving = 'true';
    window.setTimeout(() => backdrop.remove(), LEAVE_MS);
    onClose?.();
  }

  append(bodyEl, resolve(body, close));
  const footerContent = resolve(footer, close);
  if (footerContent) panel.appendChild(h('footer.modal__footer', footerContent));

  document.body.appendChild(backdrop);
  const unlock = lockScroll();
  const popLayer = pushLayer({ onEscape: () => dismissible && close() });
  const releaseFocus = trapFocus(panel, {
    initial: bodyEl.querySelector('[autofocus], input, select, textarea') ?? panel.querySelector('.modal__footer .btn--primary, .modal__footer .btn--danger') ?? panel,
  });

  return {
    el: panel,
    body: bodyEl,
    close,
    setTitle: (next) => {
      clear(titleEl, next);
    },
  };
}

/**
 * Demande une confirmation.
 * @param {Object} options
 * @param {string} options.title
 * @param {string|Node} [options.text]
 * @param {string} [options.confirmLabel='Confirmer']
 * @param {string} [options.cancelLabel='Annuler']
 * @param {'primary'|'danger'} [options.tone='primary']
 * @param {string} [options.icon]
 * @returns {Promise<boolean>} `true` si l'utilisateur confirme.
 */
export function confirm({ title, text, confirmLabel = 'Confirmer', cancelLabel = 'Annuler', tone = 'primary', icon: iconName } = {}) {
  return new Promise((resolvePromise) => {
    let answer = false;
    openModal({
      title,
      icon: iconName,
      size: 'sm',
      body: text ? h('p.modal__text', text) : null,
      footer: (close) => [
        Button({ label: cancelLabel, variant: 'ghost', onClick: close }),
        Button({
          label: confirmLabel,
          variant: tone,
          onClick: () => {
            answer = true;
            close();
          },
        }),
      ],
      onClose: () => resolvePromise(answer),
    });
  });
}

/**
 * Ouvre un tiroir latéral.
 * @param {Object} options
 * @param {'right'|'left'} [options.side='right']
 * @param {number} [options.width=440] Largeur en pixels.
 * @param {string} options.title
 * @param {string} [options.subtitle]
 * @param {string} [options.icon]
 * @param {Node|Node[]} [options.actions] Contrôles dans l'en-tête, avant la croix.
 * @param {Node|Node[]|string|((close: () => void) => Node|Node[])} [options.body]
 * @param {Node|Node[]|((close: () => void) => Node|Node[])} [options.footer]
 * @param {boolean} [options.modal=true] `false` : pas de voile ni de piège de focus, la page reste utilisable.
 * @param {string} [options.class] Classe supplémentaire sur le panneau.
 * @param {() => void} [options.onClose]
 * @returns {OverlayHandle}
 */
export function openDrawer({ side = 'right', width = 440, title, subtitle, icon: iconName, actions, body, footer, modal = true, class: className, onClose } = {}) {
  const titleId = uid('drawer-title');
  let closed = false;

  const titleEl = h('h2.drawer__title', { id: titleId }, title);
  const bodyEl = h('div.drawer__body.scroll-y');
  const panel = h(
    'aside.drawer',
    {
      class: [`drawer--${side}`, { 'drawer--docked': !modal }, className],
      role: modal ? 'dialog' : 'complementary',
      'aria-modal': modal ? 'true' : null,
      'aria-labelledby': titleId,
      tabIndex: -1,
      style: { '--drawer-width': `${width}px` },
    },
    h(
      'header.drawer__header',
      iconName ? h('span.drawer__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: 16 })) : null,
      h('div.drawer__heading', titleEl, subtitle ? h('p.drawer__subtitle', subtitle) : null),
      actions ? h('div.drawer__actions', actions) : null,
      closeButton(() => close()),
    ),
    bodyEl,
  );

  const backdrop = modal
    ? h(
        'div.overlay.overlay--drawer',
        {
          onPointerdown: (event) => {
            if (event.target === backdrop) close();
          },
        },
        panel,
      )
    : null;
  const root = backdrop ?? panel;

  function close() {
    if (closed) return;
    closed = true;
    cleanups.forEach((cleanup) => cleanup());
    root.dataset.leaving = 'true';
    window.setTimeout(() => root.remove(), LEAVE_MS);
    onClose?.();
  }

  append(bodyEl, resolve(body, close));
  const footerContent = resolve(footer, close);
  if (footerContent) panel.appendChild(h('footer.drawer__footer', footerContent));
  document.body.appendChild(root);

  const cleanups = [];
  if (modal) {
    cleanups.push(lockScroll(), pushLayer({ onEscape: () => close() }), trapFocus(panel));
  } else {
    panel.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !event.defaultPrevented) close();
    });
  }

  return {
    el: panel,
    body: bodyEl,
    close,
    setTitle: (next) => {
      clear(titleEl, next);
    },
  };
}
