/**
 * Champs de formulaire : Input, Textarea, NumberInput, Toggle, Checkbox, Field.
 */

import { h, clear, uid } from '../../core/dom.js';
import { icon } from '../../core/icons.js';

/**
 * Champ de texte sur une ligne.
 * @param {Object} [props]
 * @param {string} [props.value]
 * @param {string} [props.placeholder]
 * @param {string} [props.type='text']
 * @param {'sm'|'md'|'lg'} [props.size='md']
 * @param {string} [props.icon] Icône à gauche.
 * @param {string|Node} [props.prefix] Texte fixe avant la saisie.
 * @param {string|Node} [props.suffix] Texte fixe (unité) ou nœud après la saisie.
 * @param {boolean} [props.mono] Police à chasse fixe.
 * @param {boolean} [props.invalid]
 * @param {boolean} [props.disabled]
 * @param {number|string} [props.width] Largeur fixe (px ou valeur CSS).
 * @param {string} [props.ariaLabel]
 * @param {(value: string, event: Event) => void} [props.onInput] À chaque frappe.
 * @param {(value: string, event: Event) => void} [props.onChange] À la validation / perte de focus.
 * @param {(value: string) => void} [props.onEnter]
 * @returns {HTMLElement & {input: HTMLInputElement, value: string, focus: () => void, setInvalid: (on: boolean) => void, setDisabled: (on: boolean) => void}}
 */
export function Input({
  value = '',
  placeholder = '',
  type = 'text',
  size = 'md',
  icon: iconName,
  prefix,
  suffix,
  mono = false,
  invalid = false,
  disabled = false,
  width,
  ariaLabel,
  onInput,
  onChange,
  onEnter,
} = {}) {
  const input = h('input.input__control', {
    type,
    value,
    placeholder,
    disabled,
    spellcheck: false,
    autocomplete: 'off',
    'aria-label': ariaLabel,
    class: { mono },
    onInput: (event) => onInput?.(input.value, event),
    onChange: (event) => onChange?.(input.value, event),
    onKeydown: (event) => {
      if (event.key === 'Enter') onEnter?.(input.value);
    },
  });
  const el = h(
    'div.input',
    {
      class: [`input--${size}`],
      style: width !== undefined ? { width: typeof width === 'number' ? `${width}px` : width } : null,
      onPointerdown: (event) => {
        if (event.target === el) {
          event.preventDefault();
          input.focus();
        }
      },
    },
    iconName ? h('span.input__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: 15 })) : null,
    prefix ? h('span.input__affix', prefix) : null,
    input,
    suffix ? h('span.input__affix.input__affix--suffix', suffix) : null,
  );
  el.input = input;
  Object.defineProperty(el, 'value', {
    get: () => input.value,
    set: (next) => {
      input.value = next ?? '';
    },
  });
  el.focus = () => input.focus();
  el.setInvalid = (on) => {
    el.dataset.invalid = String(Boolean(on));
    input.setAttribute('aria-invalid', String(Boolean(on)));
  };
  el.setDisabled = (on) => {
    input.disabled = Boolean(on);
    el.dataset.disabled = String(Boolean(on));
  };
  el.setInvalid(invalid);
  el.setDisabled(disabled);
  return el;
}

/**
 * Zone de texte multiligne (paramètres JSON, notes).
 * @param {Object} [props]
 * @param {string} [props.value]
 * @param {string} [props.placeholder]
 * @param {number} [props.rows=4]
 * @param {boolean} [props.mono]
 * @param {boolean} [props.invalid]
 * @param {boolean} [props.disabled]
 * @param {string} [props.ariaLabel]
 * @param {(value: string, event: Event) => void} [props.onInput]
 * @param {(value: string, event: Event) => void} [props.onChange]
 * @returns {HTMLTextAreaElement & {setInvalid: (on: boolean) => void}}
 */
export function Textarea({ value = '', placeholder = '', rows = 4, mono = false, invalid = false, disabled = false, ariaLabel, onInput, onChange } = {}) {
  const el = h('textarea.textarea', {
    rows,
    placeholder,
    disabled,
    spellcheck: false,
    'aria-label': ariaLabel,
    class: { mono },
    onInput: (event) => onInput?.(el.value, event),
    onChange: (event) => onChange?.(el.value, event),
  });
  el.value = value;
  el.setInvalid = (on) => {
    el.dataset.invalid = String(Boolean(on));
    el.setAttribute('aria-invalid', String(Boolean(on)));
  };
  el.setInvalid(invalid);
  return el;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function precisionOf(step) {
  const text = String(step);
  return text.includes('.') ? text.split('.')[1].length : 0;
}

/**
 * Champ numérique avec boutons − / + (maintenir pour répéter) et flèches du clavier.
 * @param {Object} [props]
 * @param {number} [props.value=0]
 * @param {number} [props.min=-Infinity]
 * @param {number} [props.max=Infinity]
 * @param {number} [props.step=1]
 * @param {string} [props.suffix] Unité affichée après la valeur (« ms », « % »).
 * @param {'sm'|'md'|'lg'} [props.size='md']
 * @param {number|string} [props.width=132]
 * @param {boolean} [props.disabled]
 * @param {string} [props.ariaLabel]
 * @param {(value: number) => void} [props.onChange] À chaque changement validé.
 * @returns {HTMLElement & {input: HTMLInputElement, value: number, setValue: (value: number) => void, setDisabled: (on: boolean) => void}}
 */
export function NumberInput({ value = 0, min = -Infinity, max = Infinity, step = 1, suffix = '', size = 'md', width = 132, disabled = false, ariaLabel, onChange } = {}) {
  const precision = precisionOf(step);
  let current = clamp(Number(value) || 0, min, max);

  const input = h('input.input__control.num', {
    type: 'text',
    inputmode: precision > 0 || min < 0 ? 'text' : 'numeric',
    autocomplete: 'off',
    spellcheck: false,
    role: 'spinbutton',
    'aria-label': ariaLabel,
    'aria-valuemin': Number.isFinite(min) ? min : null,
    'aria-valuemax': Number.isFinite(max) ? max : null,
  });

  function render() {
    input.value = current.toFixed(precision);
    input.setAttribute('aria-valuenow', String(current));
    minus.disabled = disabled || current <= min;
    plus.disabled = disabled || current >= max;
  }

  function commit(next, notify = true) {
    const parsed = Number(String(next).replace(',', '.'));
    const safe = Number.isFinite(parsed) ? clamp(Number(parsed.toFixed(precision)), min, max) : current;
    const changed = safe !== current;
    current = safe;
    render();
    if (changed && notify) onChange?.(current);
  }

  function repeating(delta) {
    let timer = 0;
    let interval = 0;
    const stop = () => {
      window.clearTimeout(timer);
      window.clearInterval(interval);
    };
    return {
      onPointerdown: (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        input.focus({ preventScroll: true });
        commit(current + delta);
        timer = window.setTimeout(() => {
          interval = window.setInterval(() => commit(current + delta), 60);
        }, 380);
      },
      onPointerup: stop,
      onPointerleave: stop,
      onPointercancel: stop,
    };
  }

  const minus = h('button.number__step', { type: 'button', tabIndex: -1, 'aria-label': 'Diminuer', ...repeating(-step) }, icon('minus', { size: 13, stroke: 2 }));
  const plus = h('button.number__step', { type: 'button', tabIndex: -1, 'aria-label': 'Augmenter', ...repeating(step) }, icon('plus', { size: 13, stroke: 2 }));

  input.addEventListener('change', () => commit(input.value));
  input.addEventListener('keydown', (event) => {
    const big = event.shiftKey ? 10 : 1;
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      commit(current + step * big);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      commit(current - step * big);
    } else if (event.key === 'Enter') {
      commit(input.value);
    }
  });

  const el = h(
    'div.input.number',
    { class: [`input--${size}`], style: { width: typeof width === 'number' ? `${width}px` : width } },
    input,
    suffix ? h('span.input__affix.input__affix--suffix', suffix) : null,
    h('span.number__steps', minus, plus),
  );
  el.input = input;
  Object.defineProperty(el, 'value', { get: () => current });
  el.setValue = (next) => commit(next, false);
  el.setDisabled = (on) => {
    disabled = Boolean(on);
    input.disabled = disabled;
    el.dataset.disabled = String(disabled);
    render();
  };
  el.setDisabled(disabled);
  return el;
}

/**
 * Interrupteur.
 * @param {Object} [props]
 * @param {boolean} [props.checked]
 * @param {string} [props.label] Libellé à droite de l'interrupteur.
 * @param {string} [props.description] Ligne d'explication sous le libellé.
 * @param {'sm'|'md'} [props.size='md']
 * @param {boolean} [props.disabled]
 * @param {string} [props.ariaLabel] Nom accessible quand il n'y a pas de libellé visible.
 * @param {string} [props.title] Info-bulle native de l'interrupteur entier (affichée même désactivé : ce qu'il fait, ou pourquoi il est verrouillé).
 * @param {(checked: boolean) => void} [props.onChange]
 * @returns {HTMLElement & {checked: boolean, setChecked: (on: boolean) => void, setDisabled: (on: boolean) => void}}
 */
export function Toggle({ checked = false, label = '', description = '', size = 'md', disabled = false, ariaLabel, title, onChange } = {}) {
  const id = uid('toggle');
  const control = h(
    'button.toggle__control',
    {
      type: 'button',
      role: 'switch',
      id,
      disabled,
      'aria-label': label ? null : ariaLabel,
      onClick: () => {
        el.setChecked(!el.checked);
        onChange?.(el.checked);
      },
    },
    h('span.toggle__thumb'),
  );
  const el = h(
    'div.toggle',
    { class: `toggle--${size}`, title },
    control,
    label
      ? h('label.toggle__text', { htmlFor: id }, h('span.toggle__label', label), description ? h('span.toggle__description', description) : null)
      : null,
  );
  el.checked = false;
  el.setChecked = (on) => {
    el.checked = Boolean(on);
    control.setAttribute('aria-checked', String(el.checked));
  };
  el.setDisabled = (on) => {
    control.disabled = Boolean(on);
    el.dataset.disabled = String(Boolean(on));
  };
  el.setChecked(checked);
  el.setDisabled(disabled);
  return el;
}

/**
 * Case à cocher.
 * @param {Object} [props]
 * @param {boolean} [props.checked]
 * @param {boolean} [props.indeterminate]
 * @param {string} [props.label]
 * @param {string} [props.description]
 * @param {boolean} [props.disabled]
 * @param {(checked: boolean) => void} [props.onChange]
 * @returns {HTMLLabelElement & {input: HTMLInputElement, checked: boolean, setChecked: (on: boolean) => void}}
 */
export function Checkbox({ checked = false, indeterminate = false, label = '', description = '', disabled = false, onChange } = {}) {
  const input = h('input.checkbox__input', {
    type: 'checkbox',
    checked,
    indeterminate,
    disabled,
    onChange: () => onChange?.(input.checked),
  });
  const el = h(
    'label.checkbox',
    { dataset: { disabled: String(disabled) } },
    input,
    h('span.checkbox__box', { 'aria-hidden': 'true' }, icon('check', { size: 12, stroke: 3, class: 'checkbox__check' }), h('span.checkbox__dash')),
    label ? h('span.checkbox__text', h('span.checkbox__label', label), description ? h('span.checkbox__description', description) : null) : null,
  );
  el.input = input;
  Object.defineProperty(el, 'checked', { get: () => input.checked });
  el.setChecked = (on) => {
    input.checked = Boolean(on);
    input.indeterminate = false;
  };
  return el;
}

/** Champs de saisie : cliquer leur libellé ne fait que leur donner le focus. */
const TYPED = 'input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]), textarea, select';
/** Contrôles qu'un clic sur leur libellé actionne. */
const PRESSABLE = 'button, input[type="checkbox"], input[type="radio"]';

/**
 * Cible du libellé d'un `Field` : le champ natif d'un contrôle simple, ou `null` quand le contrôle
 * réunit plusieurs boutons ou cases (Segmented, puces, groupe de boutons) — cliquer le libellé
 * actionnerait le premier ; il nomme alors le groupe.
 */
function labelTarget(control) {
  if (control?.input instanceof HTMLElement) return control.input;
  if (!(control instanceof HTMLElement)) return null;
  if (control.matches(`${TYPED}, ${PRESSABLE}`)) return control;
  const typed = control.querySelector(TYPED);
  if (typed) return typed;
  const pressable = control.querySelectorAll(PRESSABLE);
  return pressable.length === 1 ? pressable[0] : null;
}

/**
 * Habillage d'un contrôle : libellé, aide, message d'erreur. Le libellé est lié au champ natif
 * d'un contrôle simple (`<label for>` : cliquer le libellé active le champ). Quand le contrôle est
 * un groupe de boutons — Segmented, puces, ButtonGroup — le libellé nomme le groupe par
 * `aria-labelledby` et cliquer dessus n'active aucun bouton.
 * @param {Object} props
 * @param {string} props.label
 * @param {Node} props.control Le contrôle (Input, Select, Slider, Segmented, groupe de puces…).
 * @param {string|Node} [props.hint] Aide sous le contrôle.
 * @param {string} [props.error] Message d'erreur (remplace l'aide, ton danger).
 * @param {string|Node} [props.aside] Élément à droite du libellé (valeur courante, lien, « facultatif »).
 * @param {boolean} [props.inline] Libellé à gauche et contrôle à droite, sur une ligne.
 * @returns {HTMLElement & {setError: (message: string) => void, setHint: (hint: string|Node) => void}}
 */
export function Field({ label, control, hint, error = '', aside, inline = false } = {}) {
  const target = labelTarget(control);
  const hintEl = h('p.field__hint');
  let labelEl;
  if (target) {
    if (!target.id) target.id = uid('field');
    labelEl = h('label.field__label', { htmlFor: target.id }, label);
  } else {
    labelEl = h('span.field__label', { id: uid('field-label') }, label);
    if (control instanceof HTMLElement) {
      if (!control.hasAttribute('role')) control.setAttribute('role', 'group');
      if (!control.hasAttribute('aria-labelledby')) control.setAttribute('aria-labelledby', labelEl.id);
    }
  }
  const el = h(
    'div.field',
    { class: { 'field--inline': inline } },
    h('div.field__top', labelEl, aside ? h('span.field__aside', aside) : null),
    h('div.field__control', control),
    hintEl,
  );
  let currentHint = hint;
  function render(message) {
    const content = message || currentHint;
    clear(hintEl, content);
    hintEl.hidden = !content;
    el.dataset.invalid = String(Boolean(message));
  }
  el.setError = (message) => render(message);
  el.setHint = (next) => {
    currentHint = next;
    render('');
  };
  render(error);
  return el;
}
