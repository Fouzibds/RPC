/**
 * Select : liste déroulante stylée, entièrement utilisable au clavier
 * (flèches, Début/Fin, Entrée/Espace, Échap, saisie des premières lettres une fois ouverte).
 */

import { h, uid } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { Badge } from './badges.js';
import { openPopover } from './floating.js';

/**
 * @typedef {Object} SelectOption
 * @property {string|number} value
 * @property {string} label
 * @property {string} [icon] Icône devant le libellé.
 * @property {string} [color] Couleur CSS d'une pastille (ex. `var(--proto-grpc)`).
 * @property {string} [hint] Texte secondaire aligné à droite.
 * @property {string|{label: string, tone?: string}} [badge] Petite étiquette à droite du libellé (« flux », « gRPC seul »…).
 * @property {string} [description] Seconde ligne, sous le libellé.
 * @property {boolean} [disabled] Option visible mais non sélectionnable.
 * @property {string} [title] Info-bulle native de l'option : précision, ou raison pour laquelle elle est désactivée.
 */

/**
 * Liste déroulante.
 * @param {Object} props
 * @param {SelectOption[]} props.options
 * @param {string|number} [props.value]
 * @param {string} [props.placeholder='Choisir…']
 * @param {'sm'|'md'|'lg'} [props.size='md']
 * @param {number|string} [props.width] Largeur fixe (px ou valeur CSS) ; sinon s'adapte au contenu.
 * @param {boolean} [props.block] Occupe toute la largeur.
 * @param {boolean} [props.disabled]
 * @param {string} [props.ariaLabel]
 * @param {(value: string|number, option: SelectOption) => void} [props.onChange]
 * @returns {HTMLElement & {value: string|number|undefined, setValue: (value: string|number) => void, setOptions: (options: SelectOption[]) => void, setDisabled: (on: boolean) => void, setPlaceholder: (text: string) => void}}
 *   `setPlaceholder` change le texte affiché tant qu'aucune option n'est choisie.
 */
export function Select({ options = [], value, placeholder = 'Choisir…', size = 'md', width, block = false, disabled = false, ariaLabel, onChange } = {}) {
  const listId = uid('listbox');
  let items = options;
  let current = value;
  let emptyText = placeholder;
  let popover = null;
  let activeIndex = -1;
  let typed = '';
  let typedTimer = 0;

  const valueEl = h('span.select__value');
  const trigger = h(
    'button.select__trigger',
    {
      type: 'button',
      role: 'combobox',
      'aria-haspopup': 'listbox',
      'aria-expanded': 'false',
      'aria-controls': listId,
      'aria-label': ariaLabel,
      onClick: () => (popover ? close() : open()),
      onKeydown,
    },
    valueEl,
    icon('chevrons-up-down', { size: 14, class: 'select__chevron' }),
  );
  const el = h(
    'div.select',
    { class: [`select--${size}`, { 'select--block': block }], style: width !== undefined ? { width: typeof width === 'number' ? `${width}px` : width } : null },
    trigger,
  );

  function optionContent(option, compact) {
    return [
      option.color ? h('span.select__swatch', { style: { background: option.color }, 'aria-hidden': 'true' }) : null,
      option.icon ? icon(option.icon, { size: 15 }) : null,
      compact
        ? h('span.select__label.truncate', option.label)
        : h('span.select__text', h('span.select__label', option.label), option.description ? h('span.select__description', option.description) : null),
    ];
  }

  function renderValue() {
    const selected = items.find((option) => option.value === current);
    valueEl.replaceChildren();
    if (selected) valueEl.append(...optionContent(selected, true).filter(Boolean));
    else valueEl.append(h('span.select__placeholder.truncate', emptyText));
  }

  function setActive(index, scroll = true) {
    if (!popover) return;
    activeIndex = index;
    const nodes = Array.from(popover.el.querySelectorAll('[role="option"]'));
    nodes.forEach((node, i) => {
      node.dataset.active = String(i === index);
    });
    const active = nodes[index];
    if (active) {
      trigger.setAttribute('aria-activedescendant', active.id);
      if (scroll) active.scrollIntoView({ block: 'nearest' });
    }
  }

  function move(delta) {
    if (!items.length) return;
    let index = activeIndex;
    for (let step = 0; step < items.length; step += 1) {
      index = (index + delta + items.length) % items.length;
      if (!items[index].disabled) break;
    }
    setActive(index);
  }

  function edge(first) {
    const order = first ? items.map((_, i) => i) : items.map((_, i) => items.length - 1 - i);
    const index = order.find((i) => !items[i].disabled);
    if (index !== undefined) setActive(index);
  }

  function choose(index) {
    const option = items[index];
    if (!option || option.disabled) return;
    const changed = option.value !== current;
    current = option.value;
    renderValue();
    close();
    if (changed) onChange?.(current, option);
  }

  function open() {
    if (popover || trigger.disabled) return;
    const list = h(
      'div.select__list',
      { role: 'listbox', id: listId },
      items.length
        ? items.map((option, index) =>
            h(
              'div.select__option',
              {
                role: 'option',
                id: `${listId}-${index}`,
                'aria-selected': String(option.value === current),
                'aria-disabled': option.disabled ? 'true' : null,
                title: option.title,
                onPointermove: () => {
                  if (!option.disabled && activeIndex !== index) setActive(index, false);
                },
                onClick: () => choose(index),
              },
              optionContent(option, false),
              option.badge ? Badge({ size: 'sm', ...(typeof option.badge === 'string' ? { label: option.badge } : option.badge) }) : null,
              option.hint ? h('span.select__hint', option.hint) : null,
              icon('check', { size: 14, stroke: 2, class: 'select__check' }),
            ),
          )
        : h('div.select__empty', 'Aucune option'),
    );
    list.addEventListener('pointerdown', (event) => event.preventDefault());
    popover = openPopover(trigger, list, {
      placement: 'bottom-start',
      offset: 6,
      matchWidth: true,
      onClose: () => {
        popover = null;
        activeIndex = -1;
        trigger.setAttribute('aria-expanded', 'false');
        trigger.removeAttribute('aria-activedescendant');
      },
    });
    trigger.setAttribute('aria-expanded', 'true');
    const selectedIndex = items.findIndex((option) => option.value === current && !option.disabled);
    if (selectedIndex !== -1) setActive(selectedIndex);
    else edge(true);
  }

  function close() {
    popover?.close();
  }

  function typeahead(char) {
    typed += char.toLowerCase();
    window.clearTimeout(typedTimer);
    typedTimer = window.setTimeout(() => {
      typed = '';
    }, 600);
    const index = items.findIndex((option) => !option.disabled && option.label.toLowerCase().startsWith(typed));
    if (index !== -1) setActive(index);
  }

  function onKeydown(event) {
    const { key } = event;
    if (key === 'ArrowDown' || key === 'ArrowUp') {
      event.preventDefault();
      if (!popover) open();
      else move(key === 'ArrowDown' ? 1 : -1);
    } else if (key === 'Home' || key === 'End') {
      if (!popover) return;
      event.preventDefault();
      edge(key === 'Home');
    } else if (key === 'Enter' || key === ' ') {
      event.preventDefault();
      if (popover) choose(activeIndex);
      else open();
    } else if (key === 'Tab') {
      close();
    } else if (popover && key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      typeahead(key);
    }
  }

  Object.defineProperty(el, 'value', { get: () => current });
  el.setValue = (next) => {
    current = next;
    renderValue();
  };
  el.setOptions = (next) => {
    items = next;
    close();
    renderValue();
  };
  el.setDisabled = (on) => {
    trigger.disabled = Boolean(on);
    if (on) close();
  };
  el.setPlaceholder = (text) => {
    emptyText = text;
    renderValue();
  };
  el.setDisabled(disabled);
  renderValue();
  return el;
}
