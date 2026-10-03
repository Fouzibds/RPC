/**
 * Pile des couches flottantes (fenêtres modales, tiroirs, palette, menus) : la touche Échap
 * ne ferme que la couche du dessus, et les raccourcis globaux se taisent tant qu'une couche
 * bloquante est ouverte. Fournit aussi le piège de focus et le verrou de défilement des modales.
 */

const stack = [];
let installed = false;
let scrollLocks = 0;

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function onKeydown(event) {
  if (event.key !== 'Escape' || !stack.length) return;
  const top = stack[stack.length - 1];
  event.preventDefault();
  event.stopPropagation();
  top.onEscape?.(event);
}

/**
 * Empile une couche flottante.
 * @param {{onEscape?: (event: KeyboardEvent) => void, blocking?: boolean}} layer
 *   `onEscape` est appelé quand Échap est pressée et que la couche est au sommet ;
 *   `blocking` (vrai par défaut) suspend les raccourcis globaux tant que la couche existe.
 * @returns {() => void} Dépile la couche (sans effet la seconde fois).
 */
export function pushLayer({ onEscape, blocking = true } = {}) {
  if (!installed) {
    installed = true;
    document.addEventListener('keydown', onKeydown, true);
  }
  const layer = { onEscape, blocking };
  stack.push(layer);
  return () => {
    const index = stack.indexOf(layer);
    if (index !== -1) stack.splice(index, 1);
  };
}

/**
 * Une couche bloquante (modale, palette, menu) est-elle ouverte ?
 * @returns {boolean}
 */
export function hasBlockingLayer() {
  return stack.some((layer) => layer.blocking);
}

/**
 * Bloque le défilement de la page (les verrous se cumulent).
 * @returns {() => void} Libère ce verrou.
 */
export function lockScroll() {
  const root = document.documentElement;
  let released = false;
  if (scrollLocks === 0) {
    root.style.setProperty('--scroll-lock-pad', `${window.innerWidth - root.clientWidth}px`);
    root.dataset.scrollLock = '';
  }
  scrollLocks += 1;
  return () => {
    if (released) return;
    released = true;
    scrollLocks -= 1;
    if (scrollLocks > 0) return;
    delete root.dataset.scrollLock;
    root.style.removeProperty('--scroll-lock-pad');
  };
}

/**
 * Éléments focalisables et visibles d'un conteneur, dans l'ordre de tabulation.
 * @param {Element} container
 * @returns {HTMLElement[]}
 */
export function focusables(container) {
  return Array.from(container.querySelectorAll(FOCUSABLE)).filter(
    (el) => el.getClientRects().length > 0 && !el.closest('[inert]'),
  );
}

/**
 * Enferme la tabulation dans un conteneur et y place le focus.
 * @param {HTMLElement} container
 * @param {{initial?: HTMLElement|null}} [options] Élément à focaliser d'abord (sinon le premier focalisable).
 * @returns {() => void} Libère le piège et rend le focus à l'élément qui l'avait.
 */
export function trapFocus(container, { initial = null } = {}) {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;

  function onTab(event) {
    if (event.key !== 'Tab') return;
    const items = focusables(container);
    if (!items.length) {
      event.preventDefault();
      container.focus();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !container.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !container.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  }

  container.addEventListener('keydown', onTab);
  const target = initial ?? focusables(container)[0] ?? container;
  target.focus({ preventScroll: true });

  return () => {
    container.removeEventListener('keydown', onTab);
    if (previous && document.contains(previous)) previous.focus({ preventScroll: true });
  };
}
