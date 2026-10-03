/**
 * Banc d'essai — carte de configuration : préréglages, suites, procédure, protocoles,
 * répétitions, latences du balayage, bouton de lancement et progression en direct.
 */

import { h, clear } from '../../core/dom.js';
import { fmtDuration, fmtNumber } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { Badge, Button, Callout, Card, Chip, Field, NumberInput, Segmented, Select, Toggle } from '../../components/ui.js';
import { MEASURABLE_METHODS, PRESETS, SUITES, SUITE_IDS, SWEEP_CHOICES, estimateRun, matchPreset } from './model.js';
import { FieldGroup, RunProgress } from './progress.js';

const DEFAULT_PRESET = 'standard';
function methodOptions(catalog) {
  const known = new Map((catalog?.methods ?? []).map((method) => [method.name, method]));
  return MEASURABLE_METHODS.map((name) => ({ value: name, label: name, description: known.get(name)?.title }));
}

/**
 * Carte de configuration et de lancement.
 * @param {Object} props
 * @param {Object} props.store Magasin de l'application (catalogue des procédures).
 * @param {() => void} props.onRun Lance le benchmark avec la configuration courante.
 * @returns {{el: HTMLElement, value: () => Object, valid: () => boolean, applyPreset: (id: string) => void,
 *   enableSuite: (id: string) => void, setRunning: (job: Object|null, suites?: string[]) => void,
 *   setProgress: (job: Object) => void, setStarting: (on: boolean) => void, setOffline: (on: boolean) => void,
 *   setNotice: (notice: {tone: string, title: string, text?: string}|null) => void, destroy: () => void}}
 */
export function createConfigPanel({ store, onRun }) {
  const base = PRESETS.find((preset) => preset.id === DEFAULT_PRESET);
  const state = {
    suites: new Set(SUITE_IDS),
    protocols: new Set(PROTOCOL_IDS),
    method: MEASURABLE_METHODS[0],
    iterations: base.iterations,
    warmup: base.warmup,
    concurrency: 1,
    sweep: new Set(base.sweep_latencies_ms),
    hidden: { serialization_iterations: base.serialization_iterations, sweep_iterations: base.sweep_iterations },
  };
  let running = false;
  let starting = false;
  let offline = false;

  const sweepList = () => [...state.sweep].sort((a, b) => a - b);

  function value() {
    return {
      suites: SUITE_IDS.filter((id) => state.suites.has(id)),
      method: state.method,
      protocols: PROTOCOL_IDS.filter((id) => state.protocols.has(id)),
      iterations: state.iterations,
      warmup: state.warmup,
      concurrency: state.concurrency,
      sweep_latencies_ms: sweepList(),
      ...state.hidden,
    };
  }

  /* --- Préréglages ------------------------------------------------------------------------ */
  const custom = Badge({ label: 'Personnalisé', tone: 'accent', size: 'sm' });
  const presets = Segmented({
    ariaLabel: 'Préréglage',
    value: DEFAULT_PRESET,
    options: PRESETS.map((preset) => ({ value: preset.id, label: `${preset.label} · ${fmtNumber(preset.iterations)}`, title: `${fmtNumber(preset.iterations)} appels par protocole, ${fmtNumber(preset.warmup)} d’échauffement` })),
    onChange: (id) => applyPreset(id),
  });

  function applyPreset(id) {
    const preset = PRESETS.find((item) => item.id === id);
    if (!preset) return;
    state.iterations = preset.iterations;
    state.warmup = preset.warmup;
    state.sweep = new Set(preset.sweep_latencies_ms);
    state.hidden = { serialization_iterations: preset.serialization_iterations, sweep_iterations: preset.sweep_iterations };
    iterations.setValue(state.iterations);
    warmup.setValue(state.warmup);
    for (const chip of sweepChips) chip.setSelected(state.sweep.has(chip.latency));
    sync();
  }

  /* --- Suites ----------------------------------------------------------------------------- */
  const suiteToggles = SUITES.map((suite) => {
    const toggle = Toggle({
      checked: true,
      size: 'sm',
      label: suite.label,
      description: suite.question,
      onChange: (on) => {
        if (on) state.suites.add(suite.id);
        else state.suites.delete(suite.id);
        sync();
      },
    });
    toggle.suite = suite.id;
    return toggle;
  });
  const suitesField = FieldGroup({
    label: 'Suites',
    control: h(
      'div.bench-config__suites',
      suiteToggles.map((toggle, index) => h('div.bench-config__suite', { dataset: { suite: toggle.suite } }, h('span.bench-config__suite-icon', { 'aria-hidden': 'true' }, icon(SUITES[index].icon, { size: 15 })), toggle)),
    ),
  });

  /* --- Procédure, protocoles, répétitions -------------------------------------------------- */
  const method = Select({
    options: methodOptions(store.get().catalog),
    value: state.method,
    block: true,
    ariaLabel: 'Procédure mesurée',
    onChange: (name) => {
      state.method = name;
      sync();
    },
  });
  const offCatalog = store.subscribe('catalog', (catalog) => method.setOptions(methodOptions(catalog)));

  const protocolChips = PROTOCOL_IDS.map((id) => {
    const info = protocol(id);
    const chip = Chip({
      label: info.short,
      color: info.color,
      selected: true,
      onToggle: (on) => {
        if (on) state.protocols.add(id);
        else state.protocols.delete(id);
        sync();
      },
    });
    chip.style.setProperty('--tone-soft', info.soft);
    chip.style.setProperty('--tone-line', info.line);
    chip.dataset.protocol = id;
    chip.title = info.label;
    return chip;
  });
  const protocolsField = FieldGroup({ label: 'Protocoles', control: h('div.bench-config__chips', protocolChips) });

  const iterations = NumberInput({
    value: state.iterations,
    min: 10,
    max: 100000,
    step: 100,
    width: '100%',
    ariaLabel: 'Appels chronométrés par protocole',
    onChange: (next) => {
      state.iterations = next;
      sync();
    },
  });
  const warmup = NumberInput({
    value: state.warmup,
    min: 0,
    max: 5000,
    step: 10,
    width: '100%',
    ariaLabel: 'Appels d’échauffement non comptés',
    onChange: (next) => {
      state.warmup = next;
      sync();
    },
  });
  const concurrency = NumberInput({
    value: state.concurrency,
    min: 1,
    max: 64,
    step: 1,
    width: '100%',
    ariaLabel: 'Clients en parallèle',
    onChange: (next) => {
      state.concurrency = next;
      sync();
    },
  });

  const sweepChips = SWEEP_CHOICES.map((latency) => {
    const chip = Chip({
      label: `${fmtNumber(latency)} ms`,
      selected: state.sweep.has(latency),
      tone: 'accent',
      onToggle: (on) => {
        if (on) state.sweep.add(latency);
        else state.sweep.delete(latency);
        sync();
      },
    });
    chip.latency = latency;
    return chip;
  });
  const sweepField = FieldGroup({
    label: 'Latences du balayage « local vs distant »',
    control: h('div.bench-config__chips', sweepChips),
  });

  /* --- Lancement et progression ------------------------------------------------------------ */
  const run = Button({ label: 'Lancer le benchmark', icon: 'play', variant: 'primary', size: 'lg', kbd: 'mod+enter', id: 'bench-run', onClick: () => onRun() });
  const estimate = h('p.bench-config__estimate');
  const noticeSlot = h('div.bench-config__notice');
  const progress = RunProgress();

  const fields = h(
    'fieldset.bench-config__fields',
    suitesField,
    h(
      'div.bench-config__row',
      Field({ label: 'Procédure mesurée', control: method, hint: 'Appelée avec ses paramètres par défaut.' }),
      protocolsField,
      Field({ label: 'Itérations', control: iterations, hint: 'Appels par protocole.' }),
      Field({ label: 'Échauffement', control: warmup, hint: 'Appels non comptés.' }),
      Field({ label: 'Clients', control: concurrency, hint: 'En parallèle.' }),
    ),
    sweepField,
  );

  const el = Card(
    {
      title: 'Configuration de la mesure',
      subtitle: 'Latences mesurées bus de traces coupé, connexion déjà ouverte, après échauffement.',
      icon: 'sliders-horizontal',
      actions: [custom, presets],
      class: 'bench-config',
    },
    fields,
    noticeSlot,
    h('div.bench-config__foot', estimate, progress.el, run),
  );
  el.id = 'bench-config';

  function problems() {
    const issues = { suites: '', protocols: '', sweep: '' };
    if (!state.suites.size) issues.suites = 'Choisissez au moins une suite.';
    if (!state.protocols.size) issues.protocols = 'Choisissez au moins un protocole.';
    if (state.suites.has('network') && !state.sweep.size) issues.sweep = 'Choisissez au moins une latence, ou désactivez la suite « Réseau ».';
    return issues;
  }

  const valid = () => Object.values(problems()).every((text) => !text);

  function sync() {
    const config = value();
    const preset = matchPreset(config);
    presets.setValue(preset);
    custom.hidden = preset !== null;

    const issues = problems();
    suitesField.setError(issues.suites);
    sweepField.setError(issues.sweep);
    if (issues.protocols) protocolsField.setError(issues.protocols);
    else if (!state.protocols.has('local')) protocolsField.setHint('Sans l’appel local, le surcoût « × local » ne sera pas calculé.');
    else protocolsField.setHint('L’appel local sert de référence.');
    if (!issues.sweep) sweepField.setHint(state.suites.has('network') ? 'Aller-retour ajouté par le proxy de chaos à chaque appel distant.' : 'Sans effet : la suite « Réseau » est désactivée.');
    for (const chip of sweepChips) chip.querySelector('button').disabled = !state.suites.has('network');

    const cost = estimateRun(config);
    const parts = [];
    if (cost.calls) parts.push(`${fmtNumber(cost.calls)} appels chronométrés`);
    if (cost.sweepCalls) parts.push(`balayage : ${fmtNumber(cost.sweepCalls)} appels${cost.sweepWaitS >= 1 ? `, au moins ${fmtDuration(Math.round(cost.sweepWaitS))} d’attente imposée par la latence simulée` : ''}`);
    if (!parts.length && config.suites.length) parts.push('tailles et sérialisation seulement : quelques dizaines d’appels');
    clear(estimate, parts.length ? [h('span.bench-config__estimate-label', 'Cette configuration : '), parts.join(' · '), '.'] : 'Aucune suite sélectionnée.');

    run.setDisabled(running || starting || offline || !valid());
    run.title = offline ? 'Le laboratoire est injoignable' : '';
  }

  function setRunning(job, suites = []) {
    running = Boolean(job);
    fields.disabled = running;
    el.dataset.running = String(running);
    estimate.hidden = running;
    run.setLoading(running);
    run.setLabel(running ? 'Mesure en cours…' : 'Lancer le benchmark');
    if (running) progress.start(job, suites.length ? suites : SUITE_IDS);
    else progress.stop();
    sync();
  }

  sync();

  return {
    el,
    value,
    valid,
    applyPreset,
    enableSuite(id) {
      state.suites.add(id);
      suiteToggles.find((toggle) => toggle.suite === id)?.setChecked(true);
      sync();
    },
    setRunning,
    setProgress: (job) => progress.set(job),
    setStarting(on) {
      starting = on;
      run.setLoading(on || running);
      sync();
    },
    setOffline(on) {
      offline = on;
      sync();
    },
    setNotice(notice) {
      clear(noticeSlot, notice ? Callout({ tone: notice.tone, title: notice.title, text: notice.text, actions: notice.actions }) : null);
    },
    destroy() {
      progress.stop();
      offCatalog();
    },
  };
}
