/**
 * Bilan — provenance des chiffres : bandeau « pas encore chiffré », campagne de mesure avec sa
 * progression, et les quatre expériences qui alimentent le bilan avec leur état.
 */

import { h, clear } from '../../core/dom.js';
import { fmtNumber, fmtRelative } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { Button, Card, IconButton, Progress, Skeleton } from '../../components/ui.js';
import { EXPERIMENTS, EXPERIMENT_NAMES } from './campaign.js';
import { pageLink } from './data.js';

const SOURCES = Object.freeze({
  benchmark: { hint: 'Tailles, sérialisation, latences', page: 'benchmark', action: 'Lancer un benchmark rapide' },
  failures: { hint: 'Latence, échéance, coupure, panne, rejeu', page: 'chaos', action: 'Jouer les scénarios de pannes' },
  contract: { hint: 'Un client v1 face aux serveurs v2', page: 'contract', action: 'Jouer les scénarios de contrat' },
  batches: { hint: 'Plusieurs appels sur une seule connexion', page: 'console', query: { method: 'get_product_details', mode: 'async' }, action: 'Lancer les lots d’appels simultanés' },
});

const GLYPHS = Object.freeze({ done: 'circle-check', partial: 'circle-dot', pending: 'circle-dashed', queued: 'clock', running: 'loader-circle', error: 'circle-alert' });
const MULTIPLEXED = ['custom', 'grpc'];
const dateFormat = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });

function played(count, total) {
  if (!count) return 'Aucun scénario joué';
  if (total) return `${count} sur ${total} scénarios joués`;
  return `${count} ${count > 1 ? 'scénarios joués' : 'scénario joué'}`;
}

/** État d'une expérience d'après les sources du bilan : `{state, status, aside, hint}`. */
function describe(id, model) {
  const sources = model.summary.sources ?? {};
  if (id === 'benchmark') {
    if (!sources.benchmark_id) return { state: 'pending', status: 'Pas encore lancé' };
    const date = new Date(sources.benchmark_created_at);
    const config = model.report?.config;
    return {
      state: 'done',
      status: Number.isNaN(date.getTime()) ? sources.benchmark_id : dateFormat.format(date),
      aside: Number.isNaN(date.getTime()) ? '' : fmtRelative(date),
      hint: config ? `${config.method} · ${fmtNumber(config.iterations)} appels par protocole` : sources.benchmark_id,
    };
  }
  if (id === 'batches') {
    const protocols = sources.async_batches ?? [];
    if (!protocols.length) return { state: 'pending', status: 'Aucun lot lancé' };
    return {
      state: protocols.some((name) => MULTIPLEXED.includes(name)) ? 'done' : 'partial',
      status: `${protocols.length} ${protocols.length > 1 ? 'lots' : 'lot'} : ${protocols.map((name) => protocol(name).short).join(', ')}`,
    };
  }
  const count = (id === 'failures' ? sources.failure_scenarios : sources.contract_scenarios)?.length ?? 0;
  const total = (id === 'failures' ? model.failures : model.contract)?.length ?? null;
  return { state: !count ? 'pending' : total && count < total ? 'partial' : 'done', status: played(count, total) };
}

function SourceTile(id, onRun) {
  const source = SOURCES[id];
  const link = pageLink(source.page, source.query);
  const glyph = h('span.learn-source__glyph', { 'aria-hidden': 'true' });
  const status = h('span.learn-source__status');
  const aside = h('span.learn-source__aside');
  const hint = h('p.learn-source__hint');
  const run = IconButton({ icon: 'play', label: source.action, size: 'sm', class: 'learn-noprint', onClick: onRun });
  const el = h(
    'li.learn-source',
    h('div.learn-source__top', glyph, h('span.learn-source__name.t-label', EXPERIMENT_NAMES[id]), run),
    h('p.learn-source__line', status, aside),
    hint,
    link ? h('a.learn-source__link.learn-noprint', { href: link.href }, `Ouvrir ${link.label}`, icon('arrow-up-right', { size: 12 })) : null,
  );

  function paint({ state, status: text, aside: extra = '', hint: detail = source.hint }, locked) {
    if (el.dataset.state !== state) {
      el.dataset.state = state;
      clear(glyph, icon(GLYPHS[state], { size: 15, class: state === 'running' ? 'spin' : '' }));
      run.setIcon(state === 'pending' ? 'play' : 'rotate-ccw');
    }
    clear(status, text);
    clear(aside, extra);
    aside.hidden = !extra;
    clear(hint, detail);
    hint.title = state === 'error' || state === 'running' ? detail : '';
    run.disabled = locked;
  }
  return { el, paint };
}

/**
 * Panneau de provenance.
 * @param {{onRun: (experiment: string|null) => void}} props `onRun(null)` lance la campagne ; `onRun(id)` une seule expérience.
 * @returns {HTMLElement & {
 *   setLoading: () => void,
 *   update: (model: {summary: Object, report: Object|null, failures: Object[]|null, contract: Object[]|null}) => void,
 *   setRun: (run: {current: string, queue: string[], step: number, steps: number, progress: number|null, message: string}|null) => void,
 *   setError: (experiment: string, message: string|null) => void,
 *   setBusy: (job: {title: string, message: string, progress: number}|null) => void,
 *   missing: () => string[],
 * }} `missing()` : expériences qui n'alimentent pas encore complètement le bilan.
 */
export function StatusPanel({ onRun }) {
  const mark = h('span.learn-status__icon', { 'aria-hidden': 'true' });
  const title = h('h2.learn-status__title');
  const lead = h('p.learn-status__lead');
  const primary = Button({ label: 'Mesurer maintenant', variant: 'primary', size: 'lg', icon: 'play', kbd: 'mod+enter', onClick: () => onRun(null) });
  const bar = Progress({ size: 'sm' });
  const message = h('p.learn-status__message');
  const live = h('div.learn-status__live', { hidden: true }, bar, message);
  const tiles = Object.fromEntries(EXPERIMENTS.map((id) => [id, SourceTile(id, () => onRun(id))]));
  const content = h(
    'div.learn-status__content',
    h('div.learn-status__head', mark, h('div.learn-status__text', title, lead), h('div.learn-status__actions.learn-noprint', primary)),
    live,
    h('ol.learn-sources', { 'aria-label': 'Expériences qui alimentent le bilan' }, EXPERIMENTS.map((id) => tiles[id].el)),
  );
  const skeleton = h('div.learn-status__skeleton', Skeleton({ variant: 'block', height: 44, width: 44 }), h('div', Skeleton({ width: 280, height: 18 }), Skeleton({ lines: 2 })));
  const el = Card({ padding: 'none', class: 'learn-status' }, skeleton, content);
  el.setAttribute('aria-label', 'Provenance des chiffres');

  let model = null;
  let run = null;
  let busy = null;
  let headState = '';
  const errors = new Map();

  function headline(states) {
    const todo = EXPERIMENTS.filter((id) => states[id].state !== 'done');
    if (!model.summary.measured) {
      return {
        state: 'pending',
        glyph: 'flask-conical',
        title: 'Le bilan n’est pas encore chiffré',
        lead: 'Les arguments sont là ; leurs chiffres attendent vos mesures. Une campagne rapide enchaîne le banc d’essai, les scénarios de pannes et de contrat, puis un lot d’appels simultanés, et remplit cette page au fur et à mesure.',
        button: 'Mesurer maintenant',
        variant: 'primary',
      };
    }
    if (todo.length) {
      return {
        state: 'partial',
        glyph: 'circle-dot',
        title: 'Bilan partiellement chiffré',
        lead: `Il manque ${todo.length > 1 ? 'des expériences' : 'une expérience'} pour chiffrer tous les arguments : ${todo.map((id) => EXPERIMENT_NAMES[id].toLowerCase()).join(', ')}.`,
        button: 'Compléter les mesures',
        variant: 'primary',
      };
    }
    return {
      state: 'measured',
      glyph: 'badge-check',
      title: 'Bilan chiffré par vos mesures',
      lead: 'Chaque chiffre de cette page vient d’une expérience jouée dans ce laboratoire, sur cette machine. Relancez les expériences : un résultat n’a de valeur que s’il se reproduit.',
      button: 'Tout remesurer',
      variant: 'secondary',
    };
  }

  function paint() {
    skeleton.hidden = Boolean(model);
    content.hidden = !model;
    if (!model) return;

    const locked = Boolean(run || busy);
    const states = {};
    for (const id of EXPERIMENTS) {
      let state = describe(id, model);
      if (run?.current === id) state = { state: 'running', status: 'En cours…', hint: run.message || SOURCES[id].hint };
      else if (run?.queue.includes(id)) state = { ...state, state: 'queued', status: 'En attente', aside: '' };
      else if (errors.has(id)) state = { state: 'error', status: 'Échec', hint: errors.get(id) };
      states[id] = state;
      tiles[id].paint(state, locked);
    }

    const base = Object.fromEntries(EXPERIMENTS.map((id) => [id, describe(id, model)]));
    const head = headline(base);
    if (headState !== head.state) {
      headState = head.state;
      el.dataset.state = head.state;
      clear(mark, icon(head.glyph, { size: 20 }));
      primary.classList.toggle('btn--primary', head.variant === 'primary');
      primary.classList.toggle('btn--secondary', head.variant !== 'primary');
      primary.setIcon(head.state === 'measured' ? 'rotate-ccw' : 'play');
    }
    clear(title, head.title);
    clear(lead, head.lead);
    primary.setLabel(run ? 'Mesure en cours…' : head.button);
    primary.setLoading(Boolean(run));
    primary.querySelector('.btn__icon').hidden = Boolean(run);   // Button garde son icône à côté de l'indicateur d'attente
    primary.setDisabled(Boolean(busy) && !run);

    live.hidden = !locked;
    if (run) {
      bar.set({
        label: `Étape ${run.step} sur ${run.steps} · ${EXPERIMENT_NAMES[run.current]}`,
        value: run.progress ?? 0,
        indeterminate: run.progress === null,
        detail: undefined,
        tone: 'accent',
      });
      clear(message, run.message);
    } else if (busy) {
      bar.set({ label: `Laboratoire occupé · ${busy.title}`, value: busy.progress ?? 0, indeterminate: false, detail: undefined, tone: 'info' });
      clear(message, busy.message || 'Une expérience lancée ailleurs est en cours : le bilan se mettra à jour à sa fin.');
    }
  }

  el.setLoading = () => {
    model = null;
    paint();
  };
  el.update = (next) => {
    model = next;
    paint();
  };
  el.setRun = (next) => {
    run = next;
    if (run) errors.delete(run.current);
    paint();
  };
  el.setError = (experiment, text) => {
    if (text) errors.set(experiment, text);
    else errors.delete(experiment);
    paint();
  };
  el.setBusy = (job) => {
    busy = job;
    paint();
  };
  el.missing = () => (model ? EXPERIMENTS.filter((id) => describe(id, model).state !== 'done') : []);
  paint();
  return el;
}
