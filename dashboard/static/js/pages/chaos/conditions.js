/**
 * Chaos réseau — section « Conditions réseau » : préréglages, réglages fins, interrupteurs
 * « trou noir » et « panne », pannes ponctuelles armées par protocole (voir `faults.js`).
 * Chaque changement part aussitôt vers `PUT /api/network` (regroupé pendant un glissement).
 */

import { h, clear } from '../../core/dom.js';
import { fmtNumber, fmtPercent } from '../../core/format.js';
import { icon, hasIcon } from '../../core/icons.js';
import { presetInfo } from '../../core/network.js';
import { Badge, Button, Callout, Card, Field, Skeleton, Slider, Toggle, Tooltip } from '../../components/ui.js';
import { FaultArming } from './faults.js';
import { conditionsBrief, conditionsSummary, errorText, fmtDelay } from './model.js';

const PUT_DEBOUNCE_MS = 140;
const LOCAL_EDIT_GRACE_MS = 1600;

const percent0 = (value) => fmtPercent(value, { decimals: 0 });

const SLIDERS = [
  { field: 'latency_ms', label: 'Latence aller-retour', min: 0, max: 1000, step: 5, format: fmtDelay, aside: 'moitié par sens' },
  { field: 'jitter_ms', label: 'Gigue', min: 0, max: 200, step: 1, format: (value) => `± ${fmtDelay(value)}` },
  { field: 'spike_probability', label: 'Probabilité de pic', min: 0, max: 1, step: 0.01, format: percent0, tone: 'warning' },
  { field: 'spike_ms', label: 'Durée du pic', min: 0, max: 3000, step: 50, format: fmtDelay, tone: 'warning' },
  { field: 'reset_probability', label: 'Probabilité de coupure', min: 0, max: 1, step: 0.01, format: percent0, tone: 'danger' },
  { field: 'bandwidth_kbps', label: 'Débit maximal', min: 0, max: 2000, step: 20, format: (value) => (value > 0 ? `${fmtNumber(value)} kb/s` : 'illimité'), tone: 'info' },
];

/** Libellés courts des tuiles ; le libellé et la description du serveur restent dans l'info-bulle. */
const PRESET_LABELS = { ideal: 'Idéal', lan: 'LAN', wan: 'WAN', mobile_3g: 'Mobile 3G', satellite: 'Satellite', flaky: 'Instable', blackhole: 'Trou noir', outage: 'Panne' };

const SWITCHES = [
  { field: 'blackhole', label: 'Trou noir', icon: 'circle-slash', text: 'Les octets partent, rien ne revient.' },
  { field: 'down', label: 'Panne du serveur', icon: 'power', text: 'Toute connexion est coupée d’emblée.' },
];

function skeleton() {
  return h(
    'div.chaos-cond',
    { 'aria-busy': 'true' },
    h('div.chaos-presets', Array.from({ length: 8 }, () => Skeleton({ variant: 'block', height: 52 }))),
    h('div.chaos-sliders', Array.from({ length: 4 }, () => Skeleton({ lines: 2 }))),
  );
}

/**
 * Carte « Conditions réseau ».
 * @param {{store: Object, api: Object, ws: Object, toast: Object}} ctx Contexte de la page.
 * @returns {{el: HTMLElement, setBusy: (job: Object|null) => void, destroy: () => void}}
 */
export function ConditionsPanel(ctx) {
  const { store, api, ws, toast } = ctx;
  const cleanups = [];
  const actual = {};
  let presets = [];
  let busyJob = null;
  let ready = false;
  let pending = {};
  let timer = 0;
  let inFlight = false;
  let lastEdit = 0;
  let armedTimer = 0;
  let destroyed = false;

  const controls = { sliders: new Map(), toggles: new Map(), switchTiles: new Map(), tiles: new Map() };
  const faults = FaultArming({ onArm: (kind, id, count) => arm(kind, id, count) });
  const customBadge = Badge({ label: 'Personnalisé', tone: 'accent', size: 'sm' });
  customBadge.hidden = true;
  // Bouton-icône : dans une colonne de 300 px, un libellé ferait passer le titre de la carte sur deux lignes.
  const resetButton = Button({
    icon: 'rotate-ccw',
    variant: 'ghost',
    size: 'sm',
    class: 'btn--icon',
    ariaLabel: 'Réinitialiser le réseau',
    title: 'Réinitialiser : réseau idéal, pannes désarmées',
    onClick: () => reset(),
  });
  const busyNote = h('div.chaos-busy', { hidden: true });
  const body = h('div');
  const el = Card(
    { title: 'Conditions réseau', subtitle: 'Communes aux trois proxys', icon: 'sliders-horizontal', actions: resetButton, class: 'chaos-card chaos-card--conditions' },
    busyNote,
    body,
  );

  /* --- Lecture --------------------------------------------------------------- */

  function shown(spec, value) {
    const real = Number(actual[spec.field] ?? 0);
    return Math.abs(Math.round(real / spec.step) * spec.step - value) < spec.step / 1000 ? real : value;
  }

  function sync(conditions) {
    if (!conditions) return;
    Object.assign(actual, conditions);
    if (!ready) return;
    for (const [id, tile] of controls.tiles) tile.setAttribute('aria-pressed', String(conditions.preset === id));
    customBadge.hidden = conditions.preset !== 'custom';
    for (const spec of SLIDERS) controls.sliders.get(spec.field).setValue(Number(conditions[spec.field] ?? 0));
    for (const spec of SWITCHES) {
      const on = Boolean(conditions[spec.field]);
      controls.toggles.get(spec.field).setChecked(on);
      controls.switchTiles.get(spec.field).dataset.on = String(on);
    }
  }

  function accept(response, { force = false } = {}) {
    if (!response || destroyed) return;
    if (response.armed) faults.set(response.armed);
    const conditions = response.conditions ?? null;
    if (!conditions) return;
    store.set({ network: conditions });
    if (force || !Object.keys(pending).length) sync(conditions);
  }

  /* --- Écriture -------------------------------------------------------------- */

  function refuse(error) {
    if (error?.status === 409) toast.warn('Laboratoire occupé', { description: errorText(error) });
    else if (error?.offline) toast.error('Laboratoire injoignable', { description: 'Les conditions réseau n’ont pas été modifiées.' });
    else toast.error('Réglage refusé', { description: errorText(error) });
  }

  async function put(changes) {
    inFlight = true;
    try {
      accept(await api.put('/api/network', changes), { force: 'preset' in changes });
    } catch (error) {
      refuse(error);
      await load({ quiet: true });
    } finally {
      inFlight = false;
      if (Object.keys(pending).length && !timer) flush();
    }
  }

  function flush() {
    window.clearTimeout(timer);
    timer = 0;
    if (inFlight || destroyed) return;
    const changes = pending;
    pending = {};
    if (Object.keys(changes).length) put(changes);
  }

  function edit(field, value) {
    lastEdit = Date.now();
    actual[field] = value;
    actual.preset = 'custom';
    pending[field] = value;
    customBadge.hidden = false;
    for (const tile of controls.tiles.values()) tile.setAttribute('aria-pressed', 'false');
    window.clearTimeout(timer);
    timer = window.setTimeout(flush, PUT_DEBOUNCE_MS);
  }

  function choosePreset(id) {
    lastEdit = Date.now();
    window.clearTimeout(timer);
    timer = 0;
    pending = {};
    return put({ preset: id });
  }

  async function arm(kind, id, count) {
    try {
      accept(await api.post('/api/network/arm', { kind, protocol: id, count }));
    } catch (error) {
      refuse(error);
    }
  }

  async function reset() {
    resetButton.setLoading(true);
    try {
      const stale = faults.armedPairs();
      await choosePreset('ideal');
      for (const [kind, id] of stale) accept(await api.post('/api/network/arm', { kind, protocol: id, count: 0 }));
    } catch (error) {
      refuse(error);
    } finally {
      resetButton.setLoading(false);
    }
  }

  /* --- Rendu ----------------------------------------------------------------- */

  function presetTile(preset) {
    const info = presetInfo(preset.id);
    const tile = h(
      'button.chaos-preset',
      { type: 'button', 'aria-pressed': 'false', dataset: { tone: info.tone === 'danger' ? 'danger' : 'accent', preset: preset.id }, onClick: () => choosePreset(preset.id) },
      h('span.chaos-preset__head', h('span.chaos-preset__icon', { 'aria-hidden': 'true' }, icon(hasIcon(info.icon) ? info.icon : 'wifi', { size: 14 })), h('span.chaos-preset__label', PRESET_LABELS[preset.id] ?? preset.label)),
      h('span.chaos-preset__meta', conditionsBrief(preset.conditions)),
    );
    cleanups.push(Tooltip(tile, `${preset.label} — ${conditionsSummary(preset.conditions)}. ${preset.description ?? ''}`, { placement: 'top', delay: 400 }));
    controls.tiles.set(preset.id, tile);
    return tile;
  }

  function sliderField(spec) {
    const slider = Slider({
      value: Number(actual[spec.field] ?? 0),
      min: spec.min,
      max: spec.max,
      step: spec.step,
      tone: spec.tone,
      ariaLabel: spec.label,
      format: (value) => spec.format(shown(spec, value)),
      onInput: (value) => edit(spec.field, value),
    });
    controls.sliders.set(spec.field, slider);
    return Field({ label: spec.label, control: slider, aside: spec.aside });
  }

  function switchTile(spec) {
    const toggle = Toggle({
      checked: Boolean(actual[spec.field]),
      ariaLabel: spec.label,
      onChange: (on) => {
        tile.dataset.on = String(on);
        edit(spec.field, on);
        flush();
      },
    });
    const tile = h(
      'div.chaos-switch',
      { dataset: { on: String(Boolean(actual[spec.field])) } },
      h('span.chaos-switch__icon', { 'aria-hidden': 'true' }, icon(spec.icon, { size: 16 })),
      h('div.chaos-switch__text', h('span.chaos-switch__label', spec.label), h('span.chaos-switch__hint', spec.text)),
      toggle,
    );
    controls.toggles.set(spec.field, toggle);
    controls.switchTiles.set(spec.field, tile);
    return tile;
  }

  function renderReady() {
    for (const map of Object.values(controls)) map.clear();
    ready = true;
    clear(
      body,
      h(
        'div.chaos-cond',
        h('div.chaos-cond__group', h('div.chaos-cond__head', h('div.t-label', 'Préréglages'), customBadge), h('div.chaos-presets', { role: 'group', 'aria-label': 'Préréglages réseau' }, presets.map(presetTile))),
        h('div.chaos-cond__group', h('div.t-label', 'Réglages fins'), h('div.chaos-sliders', SLIDERS.map(sliderField))),
        h('div.chaos-cond__group', h('div.t-label', 'Pannes franches'), h('div.chaos-switches', SWITCHES.map(switchTile))),
        h('div.chaos-cond__group', h('div.t-label', 'Pannes ponctuelles'), faults.el),
      ),
    );
    sync(actual);
    applyBusy();
  }

  function renderProblem(error) {
    ready = false;
    const offline = Boolean(error?.offline);
    clear(
      body,
      Callout({
        tone: offline ? 'warning' : 'danger',
        title: offline ? 'Laboratoire injoignable' : 'Conditions réseau indisponibles',
        text: offline ? 'Les réglages réapparaîtront dès que le laboratoire répondra de nouveau.' : errorText(error),
        actions: Button({ label: 'Réessayer', icon: 'refresh-cw', size: 'sm', onClick: () => load() }),
      }),
    );
  }

  async function load({ quiet = false } = {}) {
    if (!quiet) {
      ready = false;
      clear(body, skeleton());
    }
    try {
      const response = await api.get('/api/network', { timeoutMs: 8000 });
      if (destroyed) return;
      presets = response.presets ?? [];
      faults.set(response.armed ?? {});
      Object.assign(actual, response.conditions ?? {});
      store.set({ network: response.conditions ?? store.get().network });
      if (ready) sync(response.conditions);
      else renderReady();
    } catch (error) {
      if (!destroyed && !(quiet && ready)) renderProblem(error);
    }
  }

  function applyBusy() {
    const on = Boolean(busyJob);
    busyNote.hidden = !on;
    if (on) {
      const title = busyJob.kind === 'benchmark' ? 'Banc d’essai en cours' : `Scénario « ${busyJob.title ?? 'guidé'} » en cours`;
      clear(busyNote, Callout({ tone: 'info', icon: 'flask-conical', title, text: 'Il règle le réseau lui-même : les commandes vous sont rendues dès qu’il se termine.' }));
    }
    resetButton.setDisabled(on);
    if (!ready) return;
    for (const tile of controls.tiles.values()) tile.disabled = on;
    for (const slider of controls.sliders.values()) slider.setDisabled(on);
    for (const toggle of controls.toggles.values()) toggle.setDisabled(on);
    faults.setDisabled(on);
  }

  /* --- Liaisons -------------------------------------------------------------- */

  cleanups.push(
    store.subscribe('network', (conditions) => {
      if (!conditions || inFlight || Object.keys(pending).length || Date.now() - lastEdit < LOCAL_EDIT_GRACE_MS) return;
      sync(conditions);
    }),
    store.subscribe('api', (state, previous) => {
      if (state === 'online' && previous !== 'online' && !ready) load();
    }),
    ws.on('network', (message) => {
      const stage = message.event?.stage;
      const consumed = stage === 'network.lost_reply' || (stage === 'network.reset' && message.event?.detail?.reason === 'armed');
      if (!consumed) return;
      window.clearTimeout(armedTimer);
      armedTimer = window.setTimeout(() => load({ quiet: true }), 200);
    }),
  );

  load();

  return {
    el,
    setBusy(job) {
      const ended = busyJob && !job;
      busyJob = job;
      applyBusy();
      if (ended) load({ quiet: true });
    },
    destroy() {
      if (Object.keys(pending).length) api.put('/api/network', pending).catch(() => {});
      destroyed = true;
      window.clearTimeout(timer);
      window.clearTimeout(armedTimer);
      for (const off of cleanups) off?.();
    },
  };
}
