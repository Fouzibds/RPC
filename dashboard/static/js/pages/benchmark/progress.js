/**
 * Banc d'essai — briques de la carte de configuration : champ de groupe et panneau de
 * progression d'une mesure (étapes, barre, message en direct, temps écoulé).
 */

import { h, clear, uid } from '../../core/dom.js';
import { fmtDuration, fmtPercent } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { Progress } from '../../components/ui.js';
import { suiteInfo } from './model.js';

const PHASE_TITLES = {
  payload: 'Tailles des messages',
  serialization: 'Sérialisation JSON vs Protobuf',
  latency: 'Latence par protocole',
  network: 'Local vs distant',
  done: 'Banc d’essai terminé',
};

/**
 * Champ dont le contrôle est un groupe (interrupteurs, puces) : même présentation que `Field`,
 * mais le libellé nomme le groupe au lieu d'activer son premier bouton.
 * @param {{label: string, control: HTMLElement, hint?: string}} props
 * @returns {HTMLElement & {setError: (message: string) => void, setHint: (hint: string) => void}}
 */
export function FieldGroup({ label, control, hint = '' }) {
  const id = uid('bench-group');
  const hintEl = h('p.field__hint');
  control.setAttribute('role', 'group');
  control.setAttribute('aria-labelledby', id);
  const el = h('div.field', h('div.field__top', h('span.field__label', { id }, label)), h('div.field__control', control), hintEl);
  let currentHint = hint;
  function render(error) {
    const content = error || currentHint;
    clear(hintEl, content);
    hintEl.hidden = !content;
    el.dataset.invalid = String(Boolean(error));
  }
  el.setError = (error) => render(error);
  el.setHint = (next) => {
    currentHint = next;
    render('');
  };
  render('');
  return el;
}

/**
 * Panneau de progression d'une mesure : une étape par suite demandée, la barre d'avancement,
 * le dernier message du serveur et le temps écoulé.
 * @returns {{el: HTMLElement, start: (job: Object, suites: string[]) => void, set: (job: Object) => void, stop: () => void}}
 *   `job` : `{phase, progress, message, state?, created_at?}` tel que le publie la tâche de fond.
 */
export function RunProgress() {
  const steps = h('ol.bench-run__steps');
  const bar = Progress({ value: 0, label: 'Préparation' });
  const message = h('p.bench-run__message.num');
  const elapsed = h('span.bench-run__elapsed.num');
  const phaseLive = h('span.sr-only', { 'aria-live': 'polite' });
  const el = h('div.bench-run', { hidden: true }, h('div.bench-run__head', steps, elapsed), bar, message, phaseLive);
  let suites = [];
  let lastPhase = '';
  let startedAt = 0;
  let ticker = 0;

  function drawSteps(job) {
    const active = suites.indexOf(job.phase);
    const done = job.phase === 'done' || job.state === 'done';
    clear(
      steps,
      suites.map((id, index) => {
        const stepState = done || (active !== -1 && index < active) ? 'done' : index === active ? 'active' : 'pending';
        return h(
          'li.bench-run__step',
          { dataset: { state: stepState }, 'aria-current': stepState === 'active' ? 'step' : null },
          h('span.bench-run__marker', { 'aria-hidden': 'true' }, stepState === 'done' ? icon('check', { size: 11, stroke: 2.5 }) : null),
          suiteInfo(id)?.label ?? id,
        );
      }),
    );
  }

  function set(job) {
    drawSteps(job);
    const title = PHASE_TITLES[job.phase] ?? 'Préparation';
    bar.set({ value: job.progress ?? 0, label: title, detail: fmtPercent(job.progress ?? 0, { decimals: 0 }) });
    message.textContent = job.message || 'Démarrage du banc d’essai…';
    if (job.phase !== lastPhase) {
      lastPhase = job.phase;
      phaseLive.textContent = title;
    }
  }

  function tick() {
    elapsed.textContent = `Écoulé : ${fmtDuration(Math.max(0, Math.round((Date.now() - startedAt) / 1000)))}`;
  }

  return {
    el,
    set,
    start(job, runSuites) {
      window.clearInterval(ticker);
      suites = runSuites;
      lastPhase = '';
      startedAt = job.created_at ? job.created_at * 1000 : Date.now();
      el.hidden = false;
      tick();
      ticker = window.setInterval(tick, 1000);
      set(job);
    },
    stop() {
      window.clearInterval(ticker);
      el.hidden = true;
    },
  };
}
