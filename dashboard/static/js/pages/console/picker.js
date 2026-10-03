/**
 * Console RPC — sélecteur de procédure : chaque ligne montre le nom, le titre, la forme d'appel
 * (unaire, flux…) et l'idempotence ; une procédure que le protocole courant ne propose pas reste
 * visible mais inactive, avec une info-bulle qui explique pourquoi.
 */

import { h } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { Badge, Tooltip, openPopover } from '../../components/ui.js';
import { KINDS, unavailableReason } from './model.js';

/**
 * Badge de la forme d'appel d'une procédure.
 * @param {string} kind `unary` | `server_stream` | `client_stream` | `bidi_stream`
 * @returns {HTMLElement}
 */
export function KindBadge(kind) {
  const info = KINDS[kind] ?? { label: kind, tone: 'neutral' };
  return Badge({ label: info.label, tone: info.tone, size: 'sm', icon: info.icon, title: info.text });
}

/**
 * Marqueur d'idempotence : rejouer l'appel est-il sans danger ?
 * @param {boolean} idempotent
 * @returns {HTMLElement}
 */
export function IdempotenceBadge(idempotent) {
  return idempotent
    ? Badge({ label: 'idempotent', tone: 'success', size: 'sm', variant: 'outline', title: 'Rejouer l’appel ne change rien de plus : une nouvelle tentative est sans danger.' })
    : Badge({ label: 'non idempotent', tone: 'warning', size: 'sm', variant: 'outline', title: 'Rejouer l’appel applique son effet une seconde fois.' });
}

/**
 * Sélecteur de procédure.
 * @param {Object} props
 * @param {Object[]} props.methods `MethodSpec` du catalogue.
 * @param {string} props.value Procédure sélectionnée.
 * @param {string} props.protocol Protocole courant (décide des procédures inactives).
 * @param {(name: string) => void} props.onChange
 * @returns {HTMLElement & {set: (next: {value?: string, protocol?: string}) => void, close: () => void}}
 */
export function ProcedurePicker({ methods, value, protocol: protocolId, onChange }) {
  let current = value;
  let currentProtocol = protocolId;
  let popover = null;
  let detach = [];

  const valueEl = h('span.select__value');
  const trigger = h(
    'button.select__trigger',
    { type: 'button', 'aria-haspopup': 'listbox', 'aria-expanded': 'false', 'aria-label': 'Procédure', onClick: () => (popover ? close() : open()), onKeydown },
    valueEl,
    icon('chevrons-up-down', { size: 14, class: 'select__chevron' }),
  );
  const el = h('div.select.select--md.select--block.console-picker', trigger);

  function renderValue() {
    const spec = methods.find((method) => method.name === current);
    valueEl.replaceChildren();
    if (spec) valueEl.append(icon(KINDS[spec.kind]?.icon ?? 'circle', { size: 15 }), h('span.select__label.truncate.mono', spec.name), h('span.console-picker__title.truncate', spec.title));
    else valueEl.append(h('span.select__placeholder.truncate', 'Choisir une procédure…'));
  }

  function options() {
    return popover ? Array.from(popover.el.querySelectorAll('[role="option"]')) : [];
  }

  function focusOption(index) {
    const nodes = options();
    nodes[(index + nodes.length) % nodes.length]?.focus({ preventScroll: true });
  }

  function choose(spec) {
    if (unavailableReason(spec, currentProtocol)) return;
    const changed = spec.name !== current;
    current = spec.name;
    renderValue();
    close();
    trigger.focus({ preventScroll: true });
    if (changed) onChange(current);
  }

  function open() {
    if (popover) return;
    const list = h(
      'div.console-picker__list',
      { role: 'listbox', 'aria-label': 'Procédures du catalogue' },
      methods.map((spec) => {
        const reason = unavailableReason(spec, currentProtocol);
        const row = h(
          'button.console-picker__option',
          {
            type: 'button',
            role: 'option',
            'aria-selected': String(spec.name === current),
            'aria-disabled': reason ? 'true' : null,
            onClick: () => choose(spec),
          },
          h('span.console-picker__icon', { 'aria-hidden': 'true' }, icon(KINDS[spec.kind]?.icon ?? 'circle', { size: 15 })),
          h('span.console-picker__text', h('span.console-picker__name.mono', spec.name), h('span.console-picker__desc', spec.title)),
          h('span.console-picker__badges', KindBadge(spec.kind), IdempotenceBadge(spec.idempotent)),
        );
        if (reason) detach.push(Tooltip(row, reason, { placement: 'right' }));
        return row;
      }),
    );
    list.addEventListener('keydown', (event) => {
      const index = options().indexOf(document.activeElement);
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        focusOption(index + (event.key === 'ArrowDown' ? 1 : -1));
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault();
        focusOption(event.key === 'Home' ? 0 : -1);
      } else if (event.key === 'Tab') {
        close();
      }
    });
    popover = openPopover(trigger, list, {
      placement: 'bottom-start',
      offset: 6,
      matchWidth: true,
      onClose: () => {
        popover = null;
        detach.forEach((off) => off());
        detach = [];
        trigger.setAttribute('aria-expanded', 'false');
        if (document.activeElement === document.body && trigger.isConnected) trigger.focus({ preventScroll: true });
      },
    });
    trigger.setAttribute('aria-expanded', 'true');
    focusOption(Math.max(0, methods.findIndex((spec) => spec.name === current)));
  }

  function close() {
    popover?.close();
  }

  function onKeydown(event) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      open();
    }
  }

  el.set = (next) => {
    if (next.value !== undefined) current = next.value;
    if (next.protocol !== undefined) currentProtocol = next.protocol;
    close();
    renderValue();
  };
  el.close = close;
  renderValue();
  return el;
}
