/**
 * Slider : curseur à piste remplie, bulle de valeur, graduations facultatives.
 * Repose sur un `<input type="range">` natif (clavier et accessibilité gratuits).
 */

import { h } from '../../core/dom.js';
import { fmtNumber } from '../../core/format.js';

/**
 * Curseur.
 * @param {Object} [props]
 * @param {number} [props.value=0]
 * @param {number} [props.min=0]
 * @param {number} [props.max=100]
 * @param {number} [props.step=1]
 * @param {(value: number) => string} [props.format] Formatage de la valeur (bulle, lecture à droite, graduations).
 * @param {boolean} [props.showValue=true] Affiche la valeur formatée à droite de la piste.
 * @param {Array<number|{value: number, label?: string}>} [props.ticks] Graduations sous la piste.
 * @param {'accent'|'success'|'warning'|'danger'|'info'} [props.tone='accent'] Couleur de la piste remplie.
 * @param {boolean} [props.disabled]
 * @param {string} [props.ariaLabel]
 * @param {(value: number) => void} [props.onInput] Pendant le glissement.
 * @param {(value: number) => void} [props.onChange] Au relâchement.
 * @returns {HTMLElement & {input: HTMLInputElement, value: number, setValue: (value: number) => void, setDisabled: (on: boolean) => void}}
 */
export function Slider({
  value = 0,
  min = 0,
  max = 100,
  step = 1,
  format = (v) => fmtNumber(v),
  showValue = true,
  ticks,
  tone = 'accent',
  disabled = false,
  ariaLabel,
  onInput,
  onChange,
} = {}) {
  const input = h('input.slider__input', {
    type: 'range',
    min,
    max,
    step,
    disabled,
    'aria-label': ariaLabel,
    onInput: () => {
      sync();
      onInput?.(Number(input.value));
    },
    onChange: () => onChange?.(Number(input.value)),
  });
  input.value = String(value);

  const bubble = h('output.slider__bubble.num', { 'aria-hidden': 'true' });
  const readout = showValue ? h('span.slider__value.num') : null;
  const ratioOf = (v) => (max === min ? 0 : (Number(v) - min) / (max - min));

  const tickList = ticks?.length
    ? h(
        'div.slider__ticks',
        { 'aria-hidden': 'true' },
        ticks.map((tick) => {
          const item = typeof tick === 'number' ? { value: tick } : tick;
          return h(
            'button.slider__tick',
            {
              type: 'button',
              tabIndex: -1,
              style: { '--at': ratioOf(item.value) },
              onClick: () => {
                if (input.disabled) return;
                input.value = String(item.value);
                sync();
                onInput?.(Number(input.value));
                onChange?.(Number(input.value));
              },
            },
            h('span.slider__tick-mark'),
            h('span.slider__tick-label.num', item.label ?? format(item.value)),
          );
        }),
      )
    : null;

  const el = h(
    'div.slider',
    { class: { 'slider--ticks': Boolean(tickList) }, dataset: { tone } },
    h('div.slider__main', h('div.slider__track', h('div.slider__rail'), input, bubble), tickList),
    readout,
  );

  function sync() {
    const current = Number(input.value);
    const text = format(current);
    el.style.setProperty('--ratio', String(ratioOf(current)));
    bubble.textContent = text;
    if (readout) readout.textContent = text;
    input.setAttribute('aria-valuetext', text);
  }

  el.input = input;
  Object.defineProperty(el, 'value', { get: () => Number(input.value) });
  el.setValue = (next) => {
    input.value = String(next);
    sync();
  };
  el.setDisabled = (on) => {
    input.disabled = Boolean(on);
    el.dataset.disabled = String(Boolean(on));
  };
  el.setDisabled(disabled);
  sync();
  return el;
}
