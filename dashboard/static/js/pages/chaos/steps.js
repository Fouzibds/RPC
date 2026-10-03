/**
 * Chaos réseau — déroulé d'un scénario guidé : une ligne par étape de
 * `failure_simulation.run_scenario` (instant, glyphe, phrase, chiffres mesurés).
 */

import { h } from '../../core/dom.js';
import { fmtMs, fmtNumber } from '../../core/format.js';
import { icon } from '../../core/icons.js';

const STEP_ICONS = { local_call: 'cpu', rpc_call: 'arrow-right-left', attempt: 'arrow-right', backoff: 'hourglass', breaker: 'shield', network: 'cloud-lightning', state: 'database', note: 'info' };
const STATUS_TONES = { ok: 'success', slow: 'warning', timeout: 'warning', error: 'danger', retry: 'info', info: 'neutral', open: 'danger', dedup: 'accent' };

function figures(detail = {}) {
  const parts = [];
  if (detail.duration_ms !== undefined) parts.push(fmtMs(detail.duration_ms));
  else if (detail.backoff_ms !== undefined) parts.push(fmtMs(detail.backoff_ms));
  else if (detail.delay_ms !== undefined) parts.push(fmtMs(detail.delay_ms));
  else if (detail.stock !== undefined) parts.push(`stock ${fmtNumber(detail.stock)}`);
  if (detail.code && detail.code !== 'OK') parts.push(detail.code);
  return parts.join(' · ');
}

/**
 * Ligne d'une étape.
 * @param {{t_ms: number, kind: string, label: string, status: string, detail?: Object}} step
 * @returns {HTMLLIElement}
 */
export function stepRow(step) {
  return h(
    'li.chaos-step',
    { dataset: { tone: STATUS_TONES[step.status] ?? 'neutral', kind: step.kind } },
    h('span.chaos-step__time.num', `+${fmtMs(step.t_ms, { decimals: step.t_ms >= 1000 ? 2 : 0 })}`),
    h('span.chaos-step__glyph', { 'aria-hidden': 'true' }, icon(STEP_ICONS[step.kind] ?? 'circle-dot', { size: 13 })),
    h('span.chaos-step__label', step.label),
    h('span.chaos-step__figures.num', figures(step.detail)),
  );
}

/**
 * Liste défilante d'étapes ; `live` la fait défiler jusqu'à la dernière ligne.
 * @param {Object[]} steps
 * @param {boolean} live
 * @returns {{wrap: HTMLElement, append: (steps: Object[]) => void}}
 */
export function StepList(steps, live) {
  const list = h('ol.chaos-steps', { 'aria-label': 'Étapes du scénario' }, steps.map(stepRow));
  const wrap = h('div.chaos-steps__wrap', { tabIndex: 0 }, list);
  const toEnd = () => (wrap.scrollTop = wrap.scrollHeight);
  if (live) requestAnimationFrame(toEnd);
  return {
    wrap,
    append(more) {
      list.append(...more.map(stepRow));
      toEnd();
    },
  };
}
