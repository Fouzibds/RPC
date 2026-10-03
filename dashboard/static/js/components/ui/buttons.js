/**
 * Boutons : Button, IconButton, ButtonGroup, CopyButton.
 */

import { h, append, clear } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { Kbd } from './badges.js';
import { Tooltip } from './tooltip.js';

function spinner(size) {
  return h('span.btn__spinner', { 'aria-hidden': 'true' }, icon('loader-circle', { size, stroke: 2, class: 'spin' }));
}

/**
 * Bouton.
 * @param {Object} [props]
 * @param {string} [props.label]
 * @param {'primary'|'secondary'|'ghost'|'danger'} [props.variant='secondary']
 * @param {'sm'|'md'|'lg'} [props.size='md'] Hauteurs 28 / 32 / 40 px.
 * @param {string} [props.icon] Icône avant le libellé.
 * @param {string} [props.iconRight] Icône après le libellé.
 * @param {string} [props.kbd] Raccourci affiché dans le bouton (`'mod+k'`).
 * @param {boolean} [props.loading] Affiche un indicateur et bloque le clic.
 * @param {boolean} [props.disabled]
 * @param {boolean} [props.block] Occupe toute la largeur disponible.
 * @param {string} [props.href] Rend un lien `<a>` au style de bouton.
 * @param {'button'|'submit'} [props.type='button']
 * @param {string} [props.title] Info-bulle.
 * @param {string} [props.ariaLabel] Nom accessible, quand le libellé visible est absent ou masqué par la page.
 * @param {(event: MouseEvent) => void} [props.onClick]
 * @param {string} [props.id]
 * @param {string} [props.class]
 * @returns {HTMLElement & {setLoading: (on: boolean) => void, setLabel: (label: string) => void, setDisabled: (on: boolean) => void, setIcon: (name: string) => void}}
 *   `setLoading(true)` remplace l'icône de tête par l'indicateur d'attente et bloque le clic ;
 *   `setDisabled(true)` désactive le bouton (`aria-disabled` pour un lien).
 */
export function Button({
  label = '',
  variant = 'secondary',
  size = 'md',
  icon: iconName,
  iconRight,
  kbd,
  loading = false,
  disabled = false,
  block = false,
  href,
  type = 'button',
  title,
  ariaLabel,
  onClick,
  id,
  class: className,
} = {}) {
  const iconSize = size === 'lg' ? 16 : size === 'sm' ? 14 : 15;
  const text = h('span.btn__label', label);
  const lead = h('span.btn__icon.btn__icon--lead', { hidden: !iconName }, iconName ? icon(iconName, { size: iconSize }) : null);
  const el = h(
    href ? 'a.btn' : 'button.btn',
    {
      class: [`btn--${variant}`, `btn--${size}`, { 'btn--block': block }, className],
      type: href ? null : type,
      href,
      id,
      'aria-label': ariaLabel,
      onClick: (event) => {
        if (el.dataset.loading === 'true' || el.getAttribute('aria-disabled') === 'true') {
          event.preventDefault();
          return;
        }
        onClick?.(event);
      },
    },
    lead,
    text,
    iconRight ? h('span.btn__icon', icon(iconRight, { size: iconSize })) : null,
    kbd ? Kbd(kbd, { size: 'sm' }) : null,
  );
  if (!label) text.hidden = true;
  if (title) Tooltip(el, title);

  let spin = null;
  el.setLoading = (on) => {
    el.dataset.loading = String(Boolean(on));
    el.setAttribute('aria-busy', String(Boolean(on)));
    if (on && !spin) {
      spin = spinner(iconSize);
      el.prepend(spin);
    } else if (!on && spin) {
      spin.remove();
      spin = null;
    }
  };
  el.setLabel = (next) => {
    clear(text, next);
    text.hidden = !next;
  };
  el.setDisabled = (on) => {
    if (href) el.setAttribute('aria-disabled', String(Boolean(on)));
    else el.disabled = Boolean(on);
  };
  el.setIcon = (name) => {
    lead.replaceChildren(icon(name, { size: iconSize }));
    lead.hidden = false;
  };
  el.setDisabled(disabled);
  if (loading) el.setLoading(true);
  return el;
}

/**
 * Bouton carré ne contenant qu'une icône. `label` est obligatoire : il sert de nom accessible
 * et d'info-bulle.
 * @param {Object} props
 * @param {string} props.icon
 * @param {string} props.label
 * @param {string} [props.ariaLabel] Nom accessible s'il doit différer de l'info-bulle (par défaut `label`).
 * @param {'ghost'|'secondary'|'primary'|'danger'} [props.variant='ghost']
 * @param {'sm'|'md'|'lg'} [props.size='md']
 * @param {boolean} [props.active] État enfoncé (`aria-pressed`).
 * @param {boolean} [props.disabled]
 * @param {string} [props.kbd] Raccourci montré dans l'info-bulle.
 * @param {string|false} [props.tooltip='top'] Placement de l'info-bulle, ou `false` pour la supprimer.
 * @param {(event: MouseEvent) => void} [props.onClick]
 * @param {string} [props.id]
 * @param {string} [props.class]
 * @returns {HTMLButtonElement & {setIcon: (name: string) => void, setActive: (on: boolean) => void, setLabel: (label: string) => void, setDisabled: (on: boolean) => void}}
 *   `setLabel` change l'info-bulle (et le nom accessible, sauf si `ariaLabel` est fourni).
 */
export function IconButton({ icon: iconName, label, ariaLabel, variant = 'ghost', size = 'md', active, disabled = false, kbd = '', tooltip = 'top', onClick, id, class: className } = {}) {
  const iconSize = size === 'lg' ? 18 : size === 'sm' ? 14 : 16;
  let currentLabel = label;
  const el = h(
    'button.btn.btn--icon',
    { class: [`btn--${variant}`, `btn--${size}`, className], type: 'button', id, disabled, 'aria-label': ariaLabel ?? label, onClick },
    icon(iconName, { size: iconSize }),
  );
  if (tooltip) Tooltip(el, () => currentLabel, { placement: tooltip, kbd });

  el.setIcon = (name) => {
    el.replaceChildren(icon(name, { size: iconSize }));
  };
  el.setActive = (on) => {
    el.setAttribute('aria-pressed', String(Boolean(on)));
  };
  el.setLabel = (next) => {
    currentLabel = next;
    if (ariaLabel === undefined) el.setAttribute('aria-label', next);
  };
  el.setDisabled = (on) => {
    el.disabled = Boolean(on);
  };
  if (active !== undefined) el.setActive(active);
  return el;
}

/**
 * Groupe de boutons accolés (bordures fusionnées).
 * @param {...(HTMLElement|null|false)} buttons
 * @returns {HTMLElement}
 */
export function ButtonGroup(...buttons) {
  return append(h('div.btn-group', { role: 'group' }), ...buttons);
}

/**
 * Bouton « copier » : copie un texte dans le presse-papiers et confirme par une coche.
 * @param {Object} props
 * @param {string|(() => string)} props.text Texte à copier (ou fonction évaluée au clic).
 * @param {string} [props.label='Copier'] Nom accessible et info-bulle.
 * @param {'sm'|'md'} [props.size='sm']
 * @param {(ok: boolean) => void} [props.onCopy]
 * @returns {HTMLButtonElement}
 */
export function CopyButton({ text, label = 'Copier', size = 'sm', onCopy } = {}) {
  let timer = 0;
  const el = IconButton({
    icon: 'copy',
    label,
    size,
    class: 'copy-btn',
    onClick: async () => {
      const value = typeof text === 'function' ? text() : text;
      let ok = true;
      try {
        await navigator.clipboard.writeText(String(value ?? ''));
      } catch {
        ok = false;
      }
      el.setIcon(ok ? 'check' : 'x');
      el.dataset.state = ok ? 'copied' : 'failed';
      el.setLabel(ok ? 'Copié' : 'Copie impossible');
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        el.setIcon('copy');
        el.setLabel(label);
        delete el.dataset.state;
      }, 1400);
      onCopy?.(ok);
    },
  });
  return el;
}
