/**
 * Segmented (choix exclusif à curseur animé) et Tabs (onglets à soulignement animé).
 */

import { h, append, uid } from '../../core/dom.js';
import { icon } from '../../core/icons.js';

/**
 * Fait glisser un indicateur (`--x`, `--w`) sous l'élément actif et le recale quand
 * la taille change. Renvoie la fonction de recalage ; `beforeLayout` est appelé avant chacun.
 */
function trackIndicator(container, indicator, getActive, beforeLayout) {
  let ready = false;
  function update() {
    beforeLayout?.();
    const active = getActive();
    if (!active || !container.isConnected) {
      indicator.style.opacity = '0';
      return;
    }
    indicator.style.opacity = '1';
    indicator.style.setProperty('--x', `${active.offsetLeft}px`);
    indicator.style.setProperty('--w', `${active.offsetWidth}px`);
    if (!ready) {
      indicator.style.transition = 'none';
      void indicator.offsetWidth;
      indicator.style.transition = '';
      ready = true;
    }
  }
  new ResizeObserver(update).observe(container);
  return update;
}

function roving(buttons, current, event) {
  const enabled = buttons.filter((button) => !button.disabled);
  if (!enabled.length) return null;
  const index = enabled.indexOf(current);
  const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
  if (event.key in keys) {
    event.preventDefault();
    return enabled[(index + keys[event.key] + enabled.length) % enabled.length];
  }
  if (event.key === 'Home' || event.key === 'End') {
    event.preventDefault();
    return enabled[event.key === 'Home' ? 0 : enabled.length - 1];
  }
  return null;
}

/**
 * @typedef {Object} SegmentedOption
 * @property {string|number} value
 * @property {string} [label]
 * @property {string} [icon]
 * @property {string} [color] Pastille de couleur (ex. `var(--proto-rest)`).
 * @property {string} [title] Info-bulle native : nom d'un segment réduit à une icône, ou raison pour laquelle il est désactivé.
 * @property {boolean} [disabled] Segment non sélectionnable (sauté au clavier).
 */

/**
 * Contrôle segmenté : un seul choix parmi quelques options, curseur animé.
 * @param {Object} props
 * @param {SegmentedOption[]} props.options
 * @param {string|number} [props.value] Valeur initiale (première option par défaut).
 * @param {'sm'|'md'} [props.size='md']
 * @param {boolean} [props.block] Segments de largeur égale sur toute la largeur.
 * @param {boolean} [props.disabled] Désactive tout le contrôle.
 * @param {string} [props.ariaLabel]
 * @param {(value: string|number) => void} [props.onChange]
 * @returns {HTMLElement & {value: string|number, setValue: (value: string|number) => void, setDisabled: (on: boolean) => void, setOptions: (options: SegmentedOption[], value?: string|number) => void}}
 *   `setDisabled(true)` désactive tous les segments (ceux désactivés un à un le restent ensuite) ;
 *   `setOptions(options, value?)` remplace les segments — libellés, états désactivés — sans notifier
 *   `onChange` : `value` s'impose s'il est fourni ; sinon la valeur courante est conservée tant que son
 *   segment existe et reste actif, et à défaut le premier segment actif est choisi (relire `.value`).
 */
export function Segmented({ options = [], value, size = 'md', block = false, disabled = false, ariaLabel, onChange } = {}) {
  let items = options;
  let current = value ?? options[0]?.value;
  let locked = Boolean(disabled);
  let buttons = [];
  const thumb = h('span.segmented__thumb', { 'aria-hidden': 'true' });
  const el = h('div.segmented', { class: [`segmented--${size}`, { 'segmented--block': block }], role: 'radiogroup', 'aria-label': ariaLabel }, thumb);
  const update = trackIndicator(el, thumb, () => buttons[items.findIndex((option) => option.value === current)]);

  function select(next, notify) {
    const changed = next !== current;
    current = next;
    buttons.forEach((button, index) => {
      const on = items[index].value === current;
      button.setAttribute('aria-checked', String(on));
      button.tabIndex = on ? 0 : -1;
    });
    update();
    if (changed && notify) onChange?.(current);
  }

  function build() {
    buttons.forEach((button) => button.remove());
    buttons = items.map((option) =>
      h(
        'button.segmented__item',
        {
          type: 'button',
          role: 'radio',
          disabled: locked || Boolean(option.disabled),
          title: option.title,
          'aria-label': option.label ? null : option.title,
          onClick: () => select(option.value, true),
        },
        option.color ? h('span.segmented__swatch', { style: { background: option.color }, 'aria-hidden': 'true' }) : null,
        option.icon ? icon(option.icon, { size: size === 'sm' ? 13 : 14 }) : null,
        option.label ? h('span', option.label) : null,
      ),
    );
    append(el, buttons);
  }

  el.addEventListener('keydown', (event) => {
    const target = roving(buttons, document.activeElement, event);
    if (!target) return;
    target.focus();
    select(items[buttons.indexOf(target)].value, true);
  });

  Object.defineProperty(el, 'value', { get: () => current });
  el.setValue = (next) => select(next, false);
  el.setDisabled = (on) => {
    locked = Boolean(on);
    el.dataset.disabled = String(locked);
    el.setAttribute('aria-disabled', String(locked));
    buttons.forEach((button, index) => {
      button.disabled = locked || Boolean(items[index].disabled);
    });
  };
  el.setOptions = (next, nextValue) => {
    items = next ?? [];
    const imposed = nextValue !== undefined && items.some((option) => option.value === nextValue);
    const kept = items.some((option) => option.value === current && !option.disabled);
    const fallback = items.find((option) => !option.disabled) ?? items[0];
    build();
    select(imposed ? nextValue : kept ? current : fallback?.value, false);
  };
  build();
  el.setDisabled(locked);
  select(current, false);
  return el;
}

/**
 * @typedef {Object} TabSpec
 * @property {string} id
 * @property {string} label
 * @property {string} [icon]
 * @property {string|number|Node} [badge] Compteur ou nœud affiché après le libellé.
 * @property {Node|(() => Node)} [content] Panneau associé (créé à la première activation si c'est une fonction).
 * @property {boolean} [disabled]
 */

/**
 * Onglets. Si les onglets portent un `content`, les panneaux sont gérés par le composant ;
 * sinon, n'utilisez que la barre et réagissez à `onChange`. Quand les onglets et les `actions`
 * dépassent la largeur disponible, la liste d'onglets défile horizontalement (bords estompés) et
 * l'onglet actif est toujours ramené dans la partie visible.
 * @param {Object} props
 * @param {TabSpec[]} props.tabs
 * @param {string} [props.value] Onglet actif initial (le premier par défaut).
 * @param {'underline'|'pill'} [props.variant='underline']
 * @param {Node|Node[]} [props.actions] Contrôles alignés à droite de la barre d'onglets.
 * @param {(id: string) => void} [props.onChange]
 * @returns {HTMLElement & {value: string, setValue: (id: string) => void, setBadge: (id: string, badge: string|number|Node|null) => void}}
 */
export function Tabs({ tabs = [], value, variant = 'underline', actions, onChange } = {}) {
  const base = uid('tabs');
  let current = value ?? tabs[0]?.id;
  const indicator = h('span.tabs__indicator', { 'aria-hidden': 'true' });
  const badges = new Map();
  const panels = new Map();
  const hasPanels = tabs.some((tab) => tab.content !== undefined);

  const buttons = tabs.map((tab) => {
    const badge = h('span.tabs__badge.num');
    badges.set(tab.id, badge);
    return h(
      'button.tabs__tab',
      {
        type: 'button',
        role: 'tab',
        id: `${base}-tab-${tab.id}`,
        disabled: Boolean(tab.disabled),
        'aria-controls': hasPanels ? `${base}-panel-${tab.id}` : null,
        onClick: () => select(tab.id, true),
      },
      tab.icon ? icon(tab.icon, { size: 15 }) : null,
      h('span', tab.label),
      badge,
    );
  });

  const list = h('div.tabs__list', { role: 'tablist' }, buttons, indicator);
  const panelHost = hasPanels ? h('div.tabs__panels') : null;
  const el = h('div.tabs', { class: `tabs--${variant}` }, h('div.tabs__bar', list, actions ? h('div.tabs__actions', actions) : null), panelHost);
  const activeButton = () => buttons[tabs.findIndex((tab) => tab.id === current)];

  /** Quand les onglets débordent, la liste défile : `data-fade` estompe le ou les bords où il en reste. */
  function syncFade() {
    const max = list.scrollWidth - list.clientWidth;
    const before = list.scrollLeft > 1;
    const after = list.scrollLeft < max - 1;
    list.dataset.fade = max <= 1 ? 'none' : before && after ? 'both' : before ? 'start' : after ? 'end' : 'none';
  }

  /** Amène l'onglet actif dans la partie visible de la liste, sans faire défiler la page. */
  function reveal(smooth) {
    const active = activeButton();
    if (!active || list.scrollWidth <= list.clientWidth) return;
    const margin = 28;
    const start = active.offsetLeft - margin;
    const end = active.offsetLeft + active.offsetWidth + margin;
    let left = null;
    if (start < list.scrollLeft) left = Math.max(0, start);
    else if (end > list.scrollLeft + list.clientWidth) left = end - list.clientWidth;
    if (left !== null) list.scrollTo({ left, behavior: smooth ? 'smooth' : 'auto' });
  }

  let placed = false;
  const update = trackIndicator(list, indicator, activeButton, () => {
    if (!placed && list.clientWidth > 0) {
      placed = true;
      reveal(false);
    }
    syncFade();
  });
  list.addEventListener('scroll', syncFade, { passive: true });

  function panelFor(tab) {
    if (!panels.has(tab.id)) {
      const content = typeof tab.content === 'function' ? tab.content() : tab.content;
      const panel = append(h('div.tabs__panel', { role: 'tabpanel', id: `${base}-panel-${tab.id}`, 'aria-labelledby': `${base}-tab-${tab.id}`, tabIndex: 0 }), content);
      panels.set(tab.id, panel);
      panelHost.appendChild(panel);
    }
    return panels.get(tab.id);
  }

  function select(id, notify) {
    const changed = id !== current;
    current = id;
    buttons.forEach((button, index) => {
      const on = tabs[index].id === current;
      button.setAttribute('aria-selected', String(on));
      button.tabIndex = on ? 0 : -1;
    });
    if (hasPanels) {
      const active = tabs.find((tab) => tab.id === current);
      if (active) panelFor(active);
      panels.forEach((panel, panelId) => {
        panel.hidden = panelId !== current;
      });
    }
    if (placed) reveal(notify);
    update();
    if (changed && notify) onChange?.(current);
  }

  list.addEventListener('keydown', (event) => {
    const target = roving(buttons, document.activeElement, event);
    if (!target) return;
    target.focus({ preventScroll: true });
    select(tabs[buttons.indexOf(target)].id, true);
  });

  Object.defineProperty(el, 'value', { get: () => current });
  el.setValue = (id) => select(id, false);
  el.setBadge = (id, badge) => {
    const node = badges.get(id);
    if (!node) return;
    node.replaceChildren();
    if (badge !== null && badge !== undefined && badge !== '') append(node, badge);
    node.hidden = !node.hasChildNodes();
    update();
  };
  tabs.forEach((tab) => el.setBadge(tab.id, tab.badge ?? null));
  select(current, false);
  return el;
}
