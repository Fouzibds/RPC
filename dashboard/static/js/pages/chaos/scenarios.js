/**
 * Chaos réseau — « Scénarios guidés » : une carte par scénario de
 * `GET /api/failures/scenarios`, lancement (`POST /api/failures/run`), étapes en direct
 * (messages WebSocket `job`), puis verdict lu dans `GET /api/jobs/{id}`.
 */

import { h, clear } from '../../core/dom.js';
import { fmtDuration, fmtMs, fmtNumber } from '../../core/format.js';
import { icon, hasIcon } from '../../core/icons.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { Badge, Button, Callout, Card, EmptyState, Progress, ProtocolChip, Section, Select, Skeleton } from '../../components/ui.js';
import { errorText } from './model.js';
import { StepList } from './steps.js';
import { Verdict } from './verdicts.js';

const POLL_MS = 700;
/**
 * Section « Scénarios guidés ».
 * @param {{store: Object, api: Object, ws: Object, toast: Object, query?: Record<string, string>}} ctx Contexte de la page ;
 *   `query.scenario` désigne le scénario à présenter à l'ouverture (`#/chaos?scenario=<id>`).
 * @param {{onJob: (job: Object|null) => void}} hooks `onJob` signale le scénario en cours (ou sa fin).
 * @returns {{el: HTMLElement, open: (id: string) => void, setLocked: (job: Object|null) => void, destroy: () => void}}
 *   `open` sélectionne un scénario et amène la section à l'écran.
 */
export function Scenarios(ctx, hooks) {
  const { store, api, ws, toast } = ctx;
  const charts = [];
  const results = {};
  const failures = {};
  const cards = new Map();
  let scenarios = [];
  let selected = null;
  let requested = ctx.query?.scenario ?? null;
  let chosenProtocol = 'custom';
  let running = null;
  let queue = [];
  let seriesSize = 0;
  let pollTimer = 0;
  let lockedBy = null;
  let destroyed = false;

  const picker = Select({
    width: 170,
    size: 'sm',
    value: chosenProtocol,
    ariaLabel: 'Protocole des scénarios',
    options: REMOTE_PROTOCOL_IDS.map((id) => ({ value: id, label: protocol(id).label, color: protocol(id).color })),
    onChange: (value) => {
      chosenProtocol = value;
    },
  });
  const runAll = Button({ label: 'Tout lancer', icon: 'fast-forward', size: 'sm', onClick: () => startSeries() });
  const list = h('div.chaos-scen__list');
  const detail = h('div.chaos-scen__detail', { 'aria-live': 'polite' });
  const body = h('div.chaos-scen');
  const el = Section(
    {
      title: 'Scénarios guidés',
      description: 'Cinq expériences déterministes : chacune provoque sa panne, mesure ce qui arrive au client, puis remet le réseau en état.',
      actions: [h('span.chaos-scen__with', 'Protocole'), picker, runAll],
      id: 'chaos-scenarios',
    },
    body,
  );

  const track = (chart) => {
    charts.push(chart);
    return chart;
  };
  function dropCharts() {
    while (charts.length) charts.pop().destroy?.();
  }

  /* --- Liste ------------------------------------------------------------------ */

  function cardState(scenario) {
    if (running?.id === scenario.id) return { state: 'running', badge: Badge({ label: 'En cours', tone: 'accent', dot: true, pulse: true, size: 'sm' }) };
    if (failures[scenario.id]) return { state: 'error', badge: Badge({ label: 'Échec', tone: 'danger', size: 'sm' }) };
    if (results[scenario.id]) {
      const played = protocol(results[scenario.id].protocol);
      return { state: 'done', badge: Badge({ label: `Joué · ${played.short}`, title: `Joué avec ${played.label}`, tone: 'success', size: 'sm', icon: 'check' }) };
    }
    return { state: 'idle', badge: Badge({ label: 'Jamais joué', tone: 'neutral', size: 'sm' }) };
  }

  function refreshCards() {
    const blocked = Boolean(running || lockedBy);
    for (const [id, card] of cards) {
      const scenario = scenarios.find((item) => item.id === id);
      const view = cardState(scenario);
      card.el.dataset.state = view.state;
      // Une fois joué, la durée réelle est dans le détail : l'estimation cède sa place au badge.
      card.duration.hidden = view.state === 'done';
      card.el.dataset.selected = String(selected === id);
      card.select.setAttribute('aria-pressed', String(selected === id));
      clear(card.status, view.badge);
      card.run.setDisabled(blocked);
      card.run.setLoading(running?.id === id);
      card.run.setLabel(results[id] || failures[id] ? 'Relancer' : 'Lancer');
    }
    runAll.setDisabled(blocked || !scenarios.length);
    runAll.setLabel(running && seriesSize ? `Série ${seriesSize - queue.length}/${seriesSize}` : 'Tout lancer');
    picker.setDisabled(Boolean(running));
  }

  function scenarioCard(scenario) {
    const select = h(
      'button.chaos-scen__select',
      { type: 'button', 'aria-pressed': 'false', onClick: () => choose(scenario.id) },
      h('span.chaos-scen__icon', { 'aria-hidden': 'true' }, icon(hasIcon(scenario.icon) ? scenario.icon : 'flask-conical', { size: 16 })),
      h('span.chaos-scen__heading', h('span.chaos-scen__title', scenario.title), h('span.chaos-scen__concept', scenario.concept)),
    );
    const duration = h('span.chaos-scen__duration', icon('timer', { size: 12 }), h('span.num', `≈ ${fmtDuration(scenario.duration_hint_s)}`));
    const status = h('span.chaos-scen__status');
    const run = Button({ label: 'Lancer', icon: 'play', size: 'sm', onClick: () => start(scenario.id) });
    const card = h(
      'article.chaos-scen__item',
      { dataset: { state: 'idle', selected: 'false' } },
      select,
      h('p.chaos-scen__summary', { title: scenario.summary }, scenario.summary),
      h('div.chaos-scen__foot', duration, status, run),
    );
    cards.set(scenario.id, { el: card, select, duration, status, run });
    return card;
  }

  /* --- Détail ------------------------------------------------------------------ */

  function renderDetail() {
    dropCharts();
    const scenario = scenarios.find((item) => item.id === selected);
    if (!scenario) {
      clear(detail);
      return;
    }
    const isRunning = running?.id === scenario.id;
    const result = results[scenario.id];
    const failure = failures[scenario.id];
    const usedProtocol = isRunning ? running.protocol : (result?.protocol ?? chosenProtocol);
    const head = h(
      'header.chaos-run__head',
      h('span.chaos-run__icon', { 'aria-hidden': 'true' }, icon(hasIcon(scenario.icon) ? scenario.icon : 'flask-conical', { size: 18 })),
      h('div.chaos-run__heading', h('h3.chaos-run__title', scenario.title), h('p.chaos-run__concept', `Notion : ${scenario.concept}`)),
      isRunning || result ? ProtocolChip(usedProtocol, { short: true, size: 'sm' }) : null,
      result && !isRunning ? h('span.chaos-run__took', 'joué en ', h('span.num', fmtMs(result.duration_ms))) : null,
    );

    if (isRunning) {
      const block = StepList(running.steps, true);
      running.view = { list: block, progress: Progress({ value: running.progress, label: running.message || 'Démarrage du scénario…', size: 'sm' }) };
      clear(detail, Card({ class: 'chaos-run', padding: 'md' }, head, running.view.progress, h('div.chaos-run__steps.chaos-run__steps--live', h('div.t-label', 'Étapes en direct'), block.wrap)));
      return;
    }
    if (failure) {
      clear(detail, Card({ class: 'chaos-run', padding: 'md' }, head, Callout({ tone: 'danger', title: 'Le scénario n’a pas abouti', text: failure, actions: Button({ label: 'Relancer', icon: 'rotate-ccw', size: 'sm', onClick: () => start(scenario.id) }) })));
      return;
    }
    if (!result) {
      clear(
        detail,
        Card(
          { class: 'chaos-run', padding: 'md' },
          head,
          EmptyState({
            icon: 'flask-conical',
            title: 'Pas encore joué',
            text: scenario.summary,
            action: Button({ label: 'Lancer ce scénario', icon: 'play', variant: 'primary', onClick: () => start(scenario.id) }),
          }),
        ),
      );
      return;
    }
    const block = StepList(result.steps ?? [], false);
    clear(
      detail,
      Card(
        { class: 'chaos-run', padding: 'md' },
        head,
        Verdict(result, track),
        h('div.chaos-run__steps', h('div.t-label', `Déroulé · ${fmtNumber(result.steps?.length ?? 0)} étapes horodatées`), block.wrap),
      ),
    );
  }

  function choose(id) {
    if (selected === id) return;
    selected = id;
    refreshCards();
    renderDetail();
  }

  /** Scénario demandé par l'URL : sélectionné puis amené à l'écran, dès que la liste est connue. */
  function reveal({ smooth }) {
    if (!requested || !scenarios.length) return;
    const id = requested;
    requested = null;
    if (!scenarios.some((scenario) => scenario.id === id)) return;
    choose(id);
    const still = !smooth || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    requestAnimationFrame(() => el.scrollIntoView({ block: 'start', behavior: still ? 'auto' : 'smooth' }));
  }

  /* --- Exécution ---------------------------------------------------------------- */

  function refuse(error) {
    queue = [];
    seriesSize = 0;
    if (error?.status === 409) toast.warn('Laboratoire occupé', { description: errorText(error) });
    else if (error?.offline) toast.error('Laboratoire injoignable', { description: 'Le scénario n’a pas pu démarrer.' });
    else toast.error('Scénario refusé', { description: errorText(error) });
  }

  async function start(id) {
    if (running || lockedBy || destroyed) return;
    selected = id;
    delete failures[id];
    running = { id, jobId: null, protocol: chosenProtocol, steps: [], progress: 0, message: '', view: null, early: [] };
    refreshCards();
    renderDetail();
    try {
      const response = await api.post('/api/failures/run', { scenario: id, protocol: running.protocol });
      if (destroyed) return;
      running.jobId = response.job_id;
      hooks.onJob?.(response.job ?? { id: response.job_id, title: scenarios.find((item) => item.id === id)?.title });
      pollTimer = window.setInterval(poll, POLL_MS);
      for (const message of running.early.splice(0)) onJob(message);
    } catch (error) {
      running = null;
      refuse(error);
      refreshCards();
      renderDetail();
    }
  }

  function startSeries() {
    if (running || lockedBy) return;
    queue = scenarios.map((scenario) => scenario.id);
    seriesSize = queue.length;
    start(queue.shift());
  }

  function addSteps(steps) {
    if (!running?.view) return;
    running.steps.push(...steps);
    running.view.list.append(steps);
  }

  function finish(snapshot) {
    if (!running) return;
    window.clearInterval(pollTimer);
    const { id } = running;
    running = null;
    if (snapshot.state === 'done' && snapshot.result) results[id] = snapshot.result;
    else failures[id] = errorText(snapshot.error) || 'Le scénario s’est interrompu sans résultat.';
    hooks.onJob?.(null);
    refreshCards();
    renderDetail();
    if (queue.length && !failures[id]) start(queue.shift());
    else {
      queue = [];
      seriesSize = 0;
      refreshCards();
    }
  }

  async function poll() {
    if (!running?.jobId) return;
    const jobId = running.jobId;
    try {
      const snapshot = await api.get(`/api/jobs/${encodeURIComponent(jobId)}`, { timeoutMs: 5000 });
      if (destroyed || running?.jobId !== jobId) return;
      if ((snapshot.steps?.length ?? 0) > running.steps.length) addSteps(snapshot.steps.slice(running.steps.length));
      if (snapshot.state !== 'running') finish(snapshot);
    } catch (error) {
      if (destroyed || running?.jobId !== jobId || error?.offline) return;
      finish({ state: 'error', error });
    }
  }

  function onJob(message) {
    if (running && running.jobId === null && message.kind === 'failure') running.early.push(message);
    if (!running || message.job_id !== running.jobId) return;
    if (message.step && message.step.t_ms > (running.steps[running.steps.length - 1]?.t_ms ?? -1)) addSteps([message.step]);
    running.progress = message.progress ?? running.progress;
    running.message = message.message ?? running.message;
    running.view?.progress.set({ value: running.progress, label: running.message });
    if (message.state && message.state !== 'running') poll();
  }

  /* --- Chargement ---------------------------------------------------------------- */

  function renderReady() {
    cards.clear();
    if (!scenarios.length) {
      clear(body, EmptyState({ icon: 'flask-conical', title: 'Aucun scénario disponible', text: 'Le laboratoire n’expose aucun scénario de panne.', action: Button({ label: 'Recharger', icon: 'refresh-cw', size: 'sm', onClick: () => load() }) }));
      return;
    }
    clear(list, scenarios.map(scenarioCard));
    clear(body, list, detail);
    selected = selected ?? scenarios.find((scenario) => results[scenario.id])?.id ?? scenarios[0].id;
    refreshCards();
    renderDetail();
    reveal({ smooth: false });
  }

  async function load() {
    clear(body, h('div.chaos-scen__list', { 'aria-busy': 'true' }, Array.from({ length: 5 }, () => Skeleton({ variant: 'block', height: 116 }))), h('div.chaos-scen__detail', Skeleton({ variant: 'block', height: 420 })));
    runAll.setDisabled(true);
    try {
      const response = await api.get('/api/failures/scenarios', { timeoutMs: 8000 });
      if (destroyed) return;
      scenarios = response.scenarios ?? [];
      Object.assign(results, response.results ?? {});
      renderReady();
    } catch (error) {
      if (destroyed) return;
      const offline = Boolean(error?.offline);
      clear(
        body,
        h(
          'div.chaos-scen__problem',
          Callout({
            tone: offline ? 'warning' : 'danger',
            title: offline ? 'Laboratoire injoignable' : 'Scénarios indisponibles',
            text: offline ? 'Les scénarios guidés réapparaîtront dès que le laboratoire répondra.' : errorText(error),
            actions: Button({ label: 'Réessayer', icon: 'refresh-cw', size: 'sm', onClick: () => load() }),
          }),
        ),
      );
    }
  }

  const offs = [
    ws.on('job', onJob),
    store.subscribe('api', (state, previous) => {
      if (state === 'online' && previous !== 'online' && !scenarios.length) load();
    }),
  ];
  load();

  return {
    el,
    open(id) {
      requested = id;
      reveal({ smooth: true });
    },
    setLocked(job) {
      lockedBy = job;
      if (scenarios.length) refreshCards();
    },
    destroy() {
      destroyed = true;
      window.clearInterval(pollTimer);
      for (const off of offs) off();
      dropCharts();
    },
  };
}
