/**
 * Chaos réseau — section « Le client » : protocole, procédure, échéance, stratégie (naïf ou
 * résilient) et les trois façons de solliciter le réseau : un appel, une rafale, un trafic continu.
 * Tous les appels passent par le proxy de chaos (`via_proxy: true`).
 */

import { h, clear } from '../../core/dom.js';
import { fmtMs, fmtNumber } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { registerShortcut } from '../../core/shortcuts.js';
import { Badge, Button, Callout, Card, Field, NumberInput, ProtocolChip, Segmented, Select, Toggle } from '../../components/ui.js';
import { errorText, fmtDelay, fmtTimes } from './model.js';

const BURST = 20;
const MAX_IN_FLIGHT = 2;
const HTTP_TIMEOUT_MS = 90000;

/** Procédures proposées : arguments fixes, choisis pour ne jamais échouer « métier ». */
const METHODS = [
  { value: 'get_product_details', params: { product_id: 'SKU-1001' }, call: 'get_product_details(\'SKU-1001\')', icon: 'package-search', description: 'Lecture : la rejouer est sans danger' },
  { value: 'update_stock', params: { product_id: 'SKU-1001', delta: 1 }, call: 'update_stock(\'SKU-1001\', +1)', icon: 'warehouse', description: 'Écriture non idempotente : un rejeu compte double' },
  { value: 'calculate_factorial', params: { n: 20 }, call: 'calculate_factorial(20)', icon: 'calculator', description: 'Calcul pur, sans état' },
];

const RATES = [1, 2, 5, 10];

/**
 * @typedef {Object} ClientSelection
 * @property {'custom'|'grpc'|'rest'} protocol
 * @property {string} method
 * @property {Object} params
 * @property {number} timeoutMs
 * @property {boolean} resilient
 */

/**
 * Carte « Le client » et moteur d'appels.
 * @param {{store: Object, api: Object, toast: Object}} ctx Contexte de la page.
 * @param {Object} hooks
 * @param {(selection: ClientSelection) => void} hooks.onSelection Protocole, procédure ou stratégie modifiés.
 * @param {(inFlight: number, looping: boolean) => void} hooks.onActivity Nombre de requêtes en cours.
 * @param {(response: Object, sentAt: number) => void} hooks.onResult Réponse de `POST /api/call`.
 * @returns {{el: HTMLElement, remote: HTMLElement, start: () => void, callOnce: () => Promise<void>, setBusy: (job: Object|null) => void, destroy: () => void}}
 *   `remote` est une télécommande compacte (résumé du client et mêmes trois actions) à placer près de la
 *   chronologie ; `start` publie la sélection initiale une fois les autres sections prêtes.
 */
export function ClientPanel(ctx, hooks) {
  const { store, api, toast } = ctx;
  const state = { protocol: 'custom', method: METHODS[0].value, timeoutMs: 1000, resilient: false, maxAttempts: 3, baseDelayMs: 100, breaker: true, idempotency: true, rate: 2 };
  let inFlight = 0;
  let looping = false;
  let loopTimer = 0;
  let busyJob = null;
  let destroyed = false;

  const methodOf = () => METHODS.find((method) => method.value === state.method) ?? METHODS[0];
  const selection = () => ({ protocol: state.protocol, method: state.method, params: methodOf().params, timeoutMs: state.timeoutMs, resilient: state.resilient });
  const policy = () => (state.resilient ? { max_attempts: state.maxAttempts, base_delay_ms: state.baseDelayMs, breaker: state.breaker, auto_idempotency_key: state.idempotency } : null);

  /* --- Résultat du dernier appel ---------------------------------------------- */

  const last = h('div.chaos-last', { 'aria-live': 'polite' }, h('span.chaos-last__placeholder', 'Aucun appel pour l’instant : lancez-en un, puis déréglez le réseau.'));

  function showSync(response) {
    const attempts = response.attempts?.length ?? 0;
    const code = response.error?.code ?? 'OK';
    const tone = response.ok ? 'success' : code === 'TIMEOUT' ? 'warning' : 'danger';
    const facts = [h('span.num', fmtMs(response.duration_ms))];
    if (attempts === 0) facts.push('aucune tentative');
    else facts.push(attempts > 1 ? `${attempts} tentatives` : '1 tentative');
    if (response.ok && response.method === 'update_stock' && response.result) {
      facts.push(h('span', 'stock ', h('span.num', fmtNumber(response.result.new_stock ?? response.result.stock))));
      if (response.result.applied === false) facts.push('rejeu reconnu');
    }
    clear(
      last,
      h('div.chaos-last__row', Badge({ label: code, tone, mono: true, size: 'sm' }), facts.map((fact) => h('span.chaos-last__fact', fact))),
      response.ok ? null : h('p.chaos-last__message', { title: errorText(response.error) }, errorText(response.error)),
    );
  }

  function showBatch(response) {
    const failed = response.errors ?? 0;
    clear(
      last,
      h(
        'div.chaos-last__row',
        Badge({ label: failed ? `${response.count - failed}/${response.count}` : `${response.count}/${response.count}`, tone: failed ? (failed === response.count ? 'danger' : 'warning') : 'success', mono: true, size: 'sm' }),
        h('span.chaos-last__fact', 'rafale en ', h('span.num', fmtMs(response.wall_ms))),
        h('span.chaos-last__fact', 'somme ', h('span.num', fmtMs(response.sum_ms))),
        response.speedup ? h('span.chaos-last__fact', 'chevauchement ', h('span.num', fmtTimes(response.speedup))) : null,
      ),
      response.strategy ? h('p.chaos-last__message', response.strategy) : null,
    );
  }

  /* --- Moteur ----------------------------------------------------------------- */

  function notify() {
    hooks.onActivity?.(inFlight, looping);
  }

  function refuse(error) {
    stopLoop();
    if (error?.status === 409) toast.warn('Laboratoire occupé', { description: errorText(error) });
    else if (error?.offline) toast.error('Laboratoire injoignable', { description: 'L’appel n’a pas pu être envoyé.' });
    else toast.error('Appel refusé', { description: errorText(error) });
  }

  async function send(mode, { quiet = false } = {}) {
    const picked = selection();
    const sentAt = Date.now();
    inFlight += 1;
    notify();
    try {
      const response = await api.post(
        '/api/call',
        { protocol: picked.protocol, method: picked.method, params: picked.params, mode, count: mode === 'async' ? BURST : 1, via_proxy: true, timeout_ms: picked.timeoutMs, policy: policy() },
        { timeoutMs: HTTP_TIMEOUT_MS },
      );
      if (destroyed) return;
      if (mode === 'async') showBatch(response);
      else showSync(response);
      hooks.onResult?.(response, sentAt);
    } catch (error) {
      if (!destroyed && !(quiet && !looping)) refuse(error);
    } finally {
      inFlight -= 1;
      if (!destroyed) notify();
    }
  }

  /** Les trois actions existent en deux exemplaires synchronisés : dans la carte et dans la télécommande. */
  const actionSet = (size, kbd, labels) => ({
    labels,
    single: Button({ label: '1 appel', variant: 'primary', icon: 'play', size, kbd, onClick: () => callOnce() }),
    burst: Button({ label: `Rafale ×${BURST}`, icon: 'fast-forward', size, onClick: () => runBurst() }),
    loop: Button({ label: labels.start, icon: 'repeat', size, onClick: () => (looping ? stopLoop() : startLoop()) }),
  });
  const sets = [actionSet('md', 'mod+enter', { start: 'Trafic continu', stop: 'Arrêter le trafic' }), actionSet('sm', undefined, { start: 'Trafic continu', stop: 'Arrêter' })];
  const everyLoop = (looping_) => sets.forEach((set) => {
    set.loop.setLabel(looping_ ? set.labels.stop : set.labels.start);
    set.loop.setIcon(looping_ ? 'square' : 'repeat');
    set.loop.dataset.active = String(looping_);
  });
  const every = (name, apply) => sets.forEach((set) => apply(set[name]));
  const [{ single, burst, loop }] = sets;
  const rate = Segmented({ size: 'sm', value: state.rate, ariaLabel: 'Cadence du trafic continu', options: RATES.map((value) => ({ value, label: `${value}/s` })), onChange: (value) => (state.rate = value) });

  async function callOnce() {
    if (busyJob || single.dataset.loading === 'true') return;
    every('single', (button) => button.setLoading(true));
    await send('sync');
    if (!destroyed) every('single', (button) => button.setLoading(false));
  }

  async function runBurst() {
    if (busyJob || burst.dataset.loading === 'true') return;
    every('burst', (button) => button.setLoading(true));
    await send('async');
    if (!destroyed) every('burst', (button) => button.setLoading(false));
  }

  function tick() {
    if (!looping) return;
    if (inFlight < MAX_IN_FLIGHT) send('sync', { quiet: true });
    loopTimer = window.setTimeout(tick, 1000 / state.rate);
  }

  function startLoop() {
    if (looping || busyJob) return;
    looping = true;
    everyLoop(true);
    last.setAttribute('aria-live', 'off');
    notify();
    tick();
  }

  function stopLoop() {
    if (!looping) return;
    looping = false;
    window.clearTimeout(loopTimer);
    everyLoop(false);
    last.setAttribute('aria-live', 'polite');
    notify();
  }

  /* --- Formulaire -------------------------------------------------------------- */

  const breakerToggle = Toggle({ checked: state.breaker, label: 'Disjoncteur', description: 'Après plusieurs échecs de suite, refuse les appels sans toucher au réseau.', onChange: (on) => (state.breaker = on) });
  const idempotencyToggle = Toggle({ checked: state.idempotency, label: 'Clé d’idempotence', description: 'Jointe aux écritures : le serveur reconnaît un rejeu et ne l’applique pas deux fois.', onChange: (on) => (state.idempotency = on) });
  const policyPanel = h(
    'div.chaos-policy',
    { hidden: true },
    h(
      'div.chaos-policy__row',
      Field({ label: 'Tentatives max.', control: NumberInput({ value: state.maxAttempts, min: 1, max: 6, step: 1, width: '100%', ariaLabel: 'Nombre maximal de tentatives', onChange: (value) => { state.maxAttempts = value; changed(); } }) }),
      Field({ label: 'Attente de base', control: NumberInput({ value: state.baseDelayMs, min: 0, max: 2000, step: 50, suffix: 'ms', width: '100%', ariaLabel: 'Attente de base entre deux tentatives', onChange: (value) => (state.baseDelayMs = value) }), hint: 'Doublée à chaque échec.' }),
    ),
    breakerToggle,
    idempotencyToggle,
  );
  const naiveNote = h('p.chaos-policy__naive', 'Une seule tentative, comme pour un appel local : la moindre panne remonte telle quelle à l’appelant.');

  function describeBreaker(snapshot) {
    const text = breakerToggle.querySelector('.toggle__description');
    if (!text || !snapshot) return;
    text.textContent = `S’ouvre après ${fmtNumber(snapshot.failure_threshold)} échecs de suite, puis retente au bout de ${fmtNumber(snapshot.reset_timeout_s)}\u202Fs.`;
  }

  const strategy = Segmented({
    block: true,
    value: 'naive',
    ariaLabel: 'Stratégie du client',
    options: [
      { value: 'naive', label: 'Client naïf', icon: 'zap' },
      { value: 'resilient', label: 'Client résilient', icon: 'shield-check' },
    ],
    onChange: (value) => {
      state.resilient = value === 'resilient';
      policyPanel.hidden = !state.resilient;
      naiveNote.hidden = state.resilient;
      changed();
    },
  });

  const protocolPicker = Segmented({
    block: true,
    value: state.protocol,
    ariaLabel: 'Protocole',
    options: REMOTE_PROTOCOL_IDS.map((id) => ({ value: id, label: protocol(id).short, color: protocol(id).color })),
    onChange: (value) => {
      state.protocol = value;
      describeBreaker(store.get().stats?.breakers?.[value] ?? store.get().status?.breakers?.[value]);
      changed();
    },
  });

  const methodPicker = Select({
    block: true,
    value: state.method,
    ariaLabel: 'Procédure',
    options: METHODS.map((method) => ({ value: method.value, label: method.call, icon: method.icon, description: method.description })),
    onChange: (value) => {
      state.method = value;
      changed();
    },
  });
  methodPicker.classList.add('chaos-method');

  const timeout = NumberInput({
    value: state.timeoutMs,
    min: 50,
    max: 10000,
    step: 50,
    suffix: 'ms',
    width: '100%',
    ariaLabel: 'Échéance de l’appel',
    onChange: (value) => {
      state.timeoutMs = value;
      changed();
    },
  });

  /* --- Télécommande ------------------------------------------------------------ */

  const summary = h('div.chaos-remote__summary');
  const remote = h('div.chaos-remote', summary, h('div.chaos-remote__actions', sets[1].single, sets[1].burst, sets[1].loop));

  function changed() {
    clear(
      summary,
      ProtocolChip(state.protocol, { short: true, size: 'sm' }),
      h('span.chaos-remote__call.num', { title: methodOf().call }, state.method),
      h('span.chaos-remote__fact', state.resilient ? `résilient · ${state.maxAttempts} tentatives` : 'naïf · 1 tentative'),
      h('span.chaos-remote__fact', { title: 'Échéance de chaque tentative' }, icon('timer', { size: 12 }), h('span.num', fmtDelay(state.timeoutMs))),
    );
    hooks.onSelection?.(selection());
  }

  const busyNote = h('div.chaos-busy', { hidden: true });
  const el = Card(
    { title: 'Le client', subtitle: 'Ce que fait l’appelant quand le réseau se dérègle', icon: 'user', class: 'chaos-card chaos-card--client' },
    busyNote,
    h(
      'div.chaos-client',
      h(
        'div.chaos-client__group',
        Field({ label: 'Protocole', control: protocolPicker, aside: 'via le proxy de chaos' }),
        Field({ label: 'Procédure', control: methodPicker }),
        Field({ label: 'Échéance', control: timeout, hint: 'Sans réponse à temps, l’appelant abandonne : TIMEOUT.' }),
      ),
      h('div.chaos-client__group', Field({ label: 'Stratégie', control: strategy }), naiveNote, policyPanel),
      h('div.chaos-client__group', h('div.chaos-actions', single, burst), h('div.chaos-actions.chaos-actions--loop', loop, rate), last),
    ),
  );

  const offShortcut = registerShortcut({ keys: 'mod+enter', description: 'Chaos réseau : lancer un appel', group: 'Page', run: () => callOnce() });
  const offBreakers = store.subscribe(
    (appState) => appState.stats?.breakers?.[state.protocol] ?? null,
    (snapshot) => describeBreaker(snapshot),
    { immediate: true },
  );

  return {
    el,
    remote,
    start: changed,
    callOnce,
    setBusy(job) {
      busyJob = job;
      if (job) stopLoop();
      busyNote.hidden = !job;
      if (job) clear(busyNote, Callout({ tone: 'info', icon: 'flask-conical', text: 'Appels suspendus pendant le scénario : ils fausseraient ses mesures.' }));
      for (const set of sets) for (const button of [set.single, set.burst, set.loop]) button.setDisabled(Boolean(job));
    },
    destroy() {
      destroyed = true;
      stopLoop();
      offShortcut();
      offBreakers();
    },
  };
}
