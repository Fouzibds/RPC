/**
 * Petits marqueurs : Badge, Chip, ProtocolChip, StatusDot, Kbd.
 */

import { h, clear, cx } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { formatKeys } from '../../core/shortcuts.js';

/** @typedef {'neutral'|'accent'|'success'|'warning'|'danger'|'info'} Tone */

/**
 * Point d'état, avec halo pulsé facultatif.
 * @param {{tone?: Tone|'muted', pulse?: boolean, label?: string, size?: 'sm'|'md'}} [props]
 *   `label` ajoute un texte à droite du point.
 * @returns {HTMLElement & {set: (next: {tone?: string, pulse?: boolean, label?: string}) => void}}
 */
export function StatusDot({ tone = 'neutral', pulse = false, label = '', size = 'md' } = {}) {
  const dot = h('span.status-dot__dot', { 'aria-hidden': 'true' });
  const text = h('span.status-dot__label');
  const el = h('span.status-dot', { class: `status-dot--${size}` }, dot, text);

  el.set = (next) => {
    if (next.tone !== undefined) el.dataset.tone = next.tone;
    if (next.pulse !== undefined) el.dataset.pulse = String(Boolean(next.pulse));
    if (next.label !== undefined) {
      clear(text, next.label);
      text.hidden = !next.label;
    }
  };
  el.set({ tone, pulse, label });
  return el;
}

/**
 * Étiquette non interactive.
 * @param {Object} [props]
 * @param {string|number} [props.label]
 * @param {Tone} [props.tone='neutral']
 * @param {'soft'|'outline'|'solid'} [props.variant='soft']
 * @param {'sm'|'md'} [props.size='md']
 * @param {boolean} [props.dot] Point de couleur avant le libellé.
 * @param {boolean} [props.pulse] Fait pulser le point.
 * @param {string} [props.icon] Icône avant le libellé.
 * @param {boolean} [props.mono] Libellé en police à chasse fixe (valeurs mesurées).
 * @param {string} [props.title] Info-bulle native.
 * @returns {HTMLElement & {setLabel: (label: string|number) => void, setTone: (tone: Tone) => void, update: (next: {label?: string|number, tone?: Tone, icon?: string|null, dot?: boolean, pulse?: boolean, title?: string}) => void}}
 *   `update` ne change que les propriétés fournies ; `icon: null` retire l'icône.
 */
export function Badge({ label = '', tone = 'neutral', variant = 'soft', size = 'md', dot = false, pulse = false, icon: iconName, mono = false, title } = {}) {
  const state = { dot: Boolean(dot), pulse: Boolean(pulse), icon: iconName ?? null };
  const text = h('span.badge__label', { class: { num: mono } }, String(label));
  const el = h('span.badge', { class: [`badge--${variant}`, `badge--${size}`], dataset: { tone }, title }, text);
  let lead = [];

  /** Point et icône précèdent le libellé ; ils sont recréés quand `update` les change. */
  function renderLead() {
    lead.forEach((node) => node.remove());
    lead = [
      state.dot ? h('span.badge__dot', { dataset: { pulse: String(state.pulse) }, 'aria-hidden': 'true' }) : null,
      state.icon ? icon(state.icon, { size: size === 'sm' ? 11 : 12, stroke: 2 }) : null,
    ].filter(Boolean);
    lead.forEach((node) => el.insertBefore(node, text));
  }

  el.setLabel = (next) => {
    clear(text, String(next));
  };
  el.setTone = (next) => {
    el.dataset.tone = next;
  };
  el.update = (next = {}) => {
    if (next.label !== undefined) el.setLabel(next.label);
    if (next.tone !== undefined) el.setTone(next.tone);
    if (next.title !== undefined) el.title = next.title;
    if (next.dot !== undefined) state.dot = Boolean(next.dot);
    if (next.pulse !== undefined) state.pulse = Boolean(next.pulse);
    if (next.icon !== undefined) state.icon = next.icon || null;
    if (next.dot !== undefined || next.pulse !== undefined || next.icon !== undefined) renderLead();
  };
  renderLead();
  return el;
}

/**
 * Puce interactive : sélectionnable (filtre, légende cliquable) et/ou supprimable.
 * @param {Object} [props]
 * @param {string} [props.label]
 * @param {Tone} [props.tone='neutral']
 * @param {string} [props.color] Couleur CSS du point (ex. `var(--proto-grpc)`) ; implique un point.
 * @param {boolean} [props.dot]
 * @param {string} [props.icon]
 * @param {boolean} [props.selected]
 * @param {boolean} [props.disabled]
 * @param {(selected: boolean) => void} [props.onToggle] Rend la puce basculable (`aria-pressed`).
 * @param {() => void} [props.onClick] Simple clic, sans état.
 * @param {() => void} [props.onRemove] Ajoute une croix de suppression.
 * @returns {HTMLElement & {selected: boolean, setSelected: (on: boolean) => void, setLabel: (label: string) => void, update: (next: {label?: string, tone?: Tone, icon?: string|null, dot?: boolean, color?: string|null}) => void}}
 *   `update` ne change que les propriétés fournies ; `icon: null` et `color: null` les retirent.
 */
export function Chip({ label = '', tone = 'neutral', color, dot = false, icon: iconName, selected = false, disabled = false, onToggle, onClick, onRemove } = {}) {
  const state = { dot: Boolean(dot), color: color ?? null, icon: iconName ?? null };
  const text = h('span.chip__label', label);
  const interactive = Boolean(onToggle || onClick);
  const main = h(
    interactive ? 'button.chip__main' : 'span.chip__main',
    {
      type: interactive ? 'button' : null,
      disabled: interactive ? disabled : null,
      onClick: () => {
        if (onToggle) {
          el.setSelected(!el.selected);
          onToggle(el.selected);
        }
        onClick?.();
      },
    },
    text,
  );
  const remove = onRemove
    ? h('button.chip__remove', { type: 'button', 'aria-label': `Retirer ${label}`, disabled, onClick: () => onRemove() }, icon('x', { size: 12, stroke: 2 }))
    : null;
  const el = h('span.chip', { dataset: { tone }, class: { 'chip--interactive': interactive, 'chip--toggle': Boolean(onToggle) } }, main, remove);
  let lead = [];

  function renderLead() {
    lead.forEach((node) => node.remove());
    lead = [
      state.dot || state.color ? h('span.chip__dot', { style: state.color ? { background: state.color } : null, 'aria-hidden': 'true' }) : null,
      state.icon ? icon(state.icon, { size: 13 }) : null,
    ].filter(Boolean);
    lead.forEach((node) => main.insertBefore(node, text));
  }

  el.selected = false;
  el.setSelected = (value) => {
    el.selected = Boolean(value);
    el.dataset.selected = String(el.selected);
    if (onToggle) main.setAttribute('aria-pressed', String(el.selected));
  };
  el.setLabel = (next) => {
    clear(text, next);
    remove?.setAttribute('aria-label', `Retirer ${next}`);
  };
  el.update = (next = {}) => {
    if (next.label !== undefined) el.setLabel(next.label);
    if (next.tone !== undefined) el.dataset.tone = next.tone;
    if (next.dot !== undefined) state.dot = Boolean(next.dot);
    if (next.color !== undefined) state.color = next.color || null;
    if (next.icon !== undefined) state.icon = next.icon || null;
    if (next.dot !== undefined || next.color !== undefined || next.icon !== undefined) renderLead();
  };
  renderLead();
  el.setSelected(selected);
  return el;
}

/**
 * Puce d'un protocole, à sa couleur officielle.
 * @param {'local'|'custom'|'grpc'|'rest'|string} id
 * @param {{short?: boolean, size?: 'sm'|'md', variant?: 'soft'|'outline'|'plain', icon?: boolean}} [options]
 *   `short` : libellé court (« gRPC ») ; `variant: 'plain'` : point + texte sans fond ; `icon` : icône au lieu du point.
 * @returns {HTMLElement}
 */
export function ProtocolChip(id, { short = false, size = 'md', variant = 'soft', icon: withIcon = false } = {}) {
  const info = protocol(id);
  return h(
    'span.proto-chip',
    {
      class: [`proto-chip--${variant}`, `proto-chip--${size}`],
      dataset: { protocol: info.id },
      style: { '--proto': info.color, '--proto-fg': info.fg, '--proto-soft': info.soft, '--proto-line': info.line },
      title: short ? info.label : null,
    },
    withIcon ? icon(info.icon, { size: size === 'sm' ? 11 : 12, stroke: 2 }) : h('span.proto-chip__dot', { 'aria-hidden': 'true' }),
    h('span.proto-chip__label', short ? info.short : info.label),
  );
}

/**
 * Touches de clavier. `Kbd('mod+k')` → Ctrl K ; `Kbd('g o')` → G puis O ; `Kbd('?')`.
 * @param {string} keys Même grammaire que `registerShortcut` : séquence séparée par des espaces, combinaisons avec `+`.
 * @param {{size?: 'sm'|'md'}} [options]
 * @returns {HTMLElement}
 */
export function Kbd(keys, { size = 'md' } = {}) {
  const el = h('span.kbd-group', { class: cx(`kbd-group--${size}`) });
  const steps = formatKeys(keys);
  clear(
    el,
    steps.map((step, index) => [
      index > 0 ? h('span.kbd-then', 'puis') : null,
      step.map((key) => (key === 'Entrée' ? h('kbd.kbd', { 'aria-label': 'Entrée' }, icon('corner-down-left', { size: 11, stroke: 2 })) : h('kbd.kbd', key))),
    ]),
  );
  return el;
}
