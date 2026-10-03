/**
 * Chaos réseau — panneau de verdict d'un scénario guidé : chiffres clés, graphique conçu pour
 * la leçon du scénario, phrase de verdict et leçon. Tout vient de `result.metrics` et de
 * `result.steps` tels que `benchmark_lab.failure_simulation` les a mesurés.
 */

import { h } from '../../core/dom.js';
import { fmtMs, fmtNumber } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';
import { BarChart, Timeline } from '../../components/charts.js';
import { Callout } from '../../components/ui.js';
import { BREAKER_STATES, CHAOS_KINDS, fmtTimes } from './model.js';

const BREAKER_SPANS = {
  st_closed: { label: 'Disjoncteur fermé', tone: 'success', style: 'solid' },
  st_open: { label: 'Disjoncteur ouvert', tone: 'danger', style: 'hatch' },
  st_half_open: { label: 'Disjoncteur semi-ouvert', tone: 'warning', style: 'solid' },
};

const FAULT_KINDS = { reset: 'net_reset', refuse: 'net_reset', lost_reply: 'net_lost' };

function figure({ label, value, hint, tone }) {
  return h('div.chaos-figure', { dataset: { tone: tone ?? '' } }, h('span.chaos-figure__label', label), h('span.chaos-figure__value.num', value), hint ? h('span.chaos-figure__hint', hint) : null);
}

/** Convertit les étapes d'un scénario en marques de chronologie ; `laneOf` choisit le couloir. */
function stepItems(steps, laneOf) {
  const items = [];
  steps.forEach((step, index) => {
    const lane = laneOf(step, index);
    const detail = step.detail ?? {};
    const id = `s${index}`;
    if (!lane) return;
    if (step.kind === 'attempt') {
      const ok = step.status === 'ok';
      items.push({ id, lane, start: step.t_ms - Number(detail.duration_ms ?? 0), end: step.t_ms, kind: ok ? (detail.attempt > 1 ? 'retry' : 'ok') : detail.code === 'TIMEOUT' ? 'timeout' : 'error', label: String(detail.attempt ?? ''), title: step.label, detail: detail.code });
    } else if (step.kind === 'backoff') {
      items.push({ id, lane, start: step.t_ms, end: step.t_ms + Number(detail.backoff_ms ?? 0), kind: 'backoff', title: step.label });
    } else if (step.kind === 'rpc_call' && detail.attempts === undefined) {
      const kind = step.status === 'ok' ? 'ok' : step.status === 'slow' ? 'slow' : step.status === 'timeout' ? 'timeout' : 'error';
      items.push({ id, lane, start: step.t_ms - Number(detail.duration_ms ?? 0), end: step.t_ms, kind, title: step.label, detail: detail.message ?? detail.code });
    } else if (step.kind === 'rpc_call' && step.status === 'open') {
      items.push({ id, lane, start: step.t_ms, kind: 'refused', title: step.label, detail: `${detail.code} en ${fmtMs(detail.duration_ms)}` });
    } else if (step.kind === 'network' && FAULT_KINDS[detail.fault]) {
      items.push({ id, lane, start: step.t_ms, kind: FAULT_KINDS[detail.fault], title: step.label });
    }
  });
  return items;
}

function latencyTrap(result, track) {
  const m = result.metrics;
  const chart = track(
    BarChart({
      orientation: 'horizontal',
      format: fmtMs,
      seriesLabel: 'Durée totale',
      ariaLabel: 'Durée totale de la boucle : locale, distante, puis un seul appel groupé',
      data: [
        { id: 'local', label: `Boucle locale · ${m.calls} appels`, short: 'Locale', protocol: 'local', value: m.local_total_ms, hint: 'Aucun réseau : la barre est invisible à cette échelle.' },
        { id: 'remote', label: `Boucle distante · ${m.calls} appels`, short: 'Distante', protocol: result.protocol, value: m.rpc_total_ms, hint: `${m.calls} allers-retours à ${fmtMs(m.latency_ms)} de latence.` },
        { id: 'batched', label: `Un appel groupé · ${m.batched_products} fiches`, short: 'Groupé', color: 'var(--success)', value: m.batched_total_ms, hint: 'Un seul aller-retour rapporte tout.' },
      ],
    }),
  );
  return {
    figures: [
      { label: 'Ralentissement de la boucle', value: fmtTimes(m.slowdown_x), tone: 'danger', hint: `${fmtMs(m.local_total_ms)} → ${fmtMs(m.rpc_total_ms)}` },
      { label: 'Par appel distant', value: fmtMs(m.per_call_ms), hint: `pour ${fmtMs(m.latency_ms)} de latence injectée` },
      { label: 'Gain de l’appel groupé', value: fmtTimes(m.batched_speedup_x), tone: 'success', hint: `${fmtMs(m.batched_total_ms)} au lieu de ${fmtMs(m.rpc_total_ms)}` },
    ],
    chart,
  };
}

function timeoutSpike(result, track) {
  const m = result.metrics;
  const chart = track(
    BarChart({
      orientation: 'horizontal',
      format: fmtMs,
      seriesLabel: 'Attente de l’appelant',
      ariaLabel: 'Temps pendant lequel l’appelant reste bloqué, sans puis avec échéance',
      data: [
        { id: 'spike', label: 'Pic injecté par le réseau', short: 'Pic', color: 'var(--neutral)', value: m.spike_ms },
        { id: 'without', label: 'Sans échéance : l’appelant attend', short: 'Sans', color: 'var(--warning)', value: m.no_deadline_ms },
        { id: 'with', label: `Échéance de ${fmtMs(m.deadline_ms)} : il est libéré`, short: 'Avec', color: 'var(--accent)', value: m.with_deadline_ms, hint: `Issue vue par l’appelant : ${m.with_deadline_outcome}.` },
      ],
    }),
  );
  const consumed = m.stock_before - m.stock_after;
  return {
    figures: [
      { label: 'Attente évitée', value: fmtMs(m.time_saved_ms), tone: 'success', hint: `${fmtMs(m.no_deadline_ms)} → ${fmtMs(m.with_deadline_ms)}` },
      { label: 'Issue vue par l’appelant', value: m.with_deadline_outcome, tone: 'warning', hint: 'ni succès, ni échec certain' },
      { label: 'Stock réellement débité', value: `${fmtNumber(m.stock_before)} → ${fmtNumber(m.stock_after)}`, tone: m.server_executed ? 'danger' : '', hint: m.server_executed ? `−${fmtNumber(consumed)} : l’appel abandonné a été exécuté` : 'l’appel abandonné n’a pas été exécuté' },
    ],
    chart,
  };
}

function connectionCut(result, track) {
  const m = result.metrics;
  let lane = 'naive';
  const items = stepItems(result.steps, (step) => {
    const current = lane;
    if (step.kind === 'rpc_call' && step.detail?.attempts === undefined) lane = 'resilient';
    return step.kind === 'local_call' ? null : current;
  });
  const chart = track(
    Timeline({
      ariaLabel: 'La même coupure face à un client naïf puis à un client résilient',
      kinds: CHAOS_KINDS,
      minSpan: 50,
      laneHeight: 44,
      lanes: [
        { id: 'naive', label: 'Client naïf', sublabel: 'une seule tentative' },
        { id: 'resilient', label: 'Client résilient', sublabel: 'réessaie après une attente' },
      ],
      items,
    }),
  );
  return {
    figures: [
      { label: 'Client naïf', value: m.naive_outcome, tone: 'danger', hint: `${m.naive_error} en ${fmtMs(m.naive_ms)}` },
      { label: 'Client résilient', value: m.resilient_outcome, tone: m.resilient_outcome === 'OK' ? 'success' : 'danger', hint: `tentative n° ${m.resilient_attempts}, en ${fmtMs(m.resilient_total_ms)}` },
      { label: 'Prix de la résilience', value: fmtMs(m.resilient_backoff_ms), hint: 'd’attente entre les tentatives' },
    ],
    chart,
  };
}

function serverOutage(result, track) {
  const m = result.metrics;
  const end = result.steps.length ? result.steps[result.steps.length - 1].t_ms : 0;
  const spans = [];
  let state = 'closed';
  let since = 0;
  result.steps.forEach((step) => {
    if (step.kind !== 'breaker' || !step.detail?.to) return;
    spans.push({ id: `b${spans.length}`, lane: 'breaker', start: since, end: step.t_ms, kind: `st_${state}`, label: BREAKER_STATES[state]?.label.toLowerCase(), title: BREAKER_SPANS[`st_${state}`].label });
    state = step.detail.to;
    since = step.t_ms;
  });
  spans.push({ id: `b${spans.length}`, lane: 'breaker', start: since, end, kind: `st_${state}`, label: BREAKER_STATES[state]?.label.toLowerCase(), title: BREAKER_SPANS[`st_${state}`].label });
  const calls = stepItems(result.steps, (step) => (step.kind === 'breaker' || step.kind === 'local_call' ? null : 'calls'));
  const timeline = track(
    Timeline({
      ariaLabel: 'État du disjoncteur et appels pendant la panne',
      kinds: { ...CHAOS_KINDS, ...BREAKER_SPANS },
      minSpan: 200,
      laneHeight: 44,
      lanes: [
        { id: 'calls', label: 'Appels', sublabel: 'tentatives et refus', protocol: result.protocol },
        { id: 'breaker', label: 'Disjoncteur', sublabel: `seuil : ${m.failures_before_open} échecs` },
      ],
      items: [...calls, ...spans],
    }),
  );
  const bars = track(
    BarChart({
      orientation: 'horizontal',
      format: fmtMs,
      log: true,
      rowHeight: 30,
      seriesLabel: 'Temps avant l’échec',
      ariaLabel: 'Temps perdu par un échec lent et par un échec rapide',
      data: [
        { id: 'slow', label: `Échec lent · ${m.slow_fail_outcome} après les tentatives`, short: 'Lent', color: 'var(--danger)', value: m.slow_fail_ms },
        { id: 'fast', label: `Échec rapide · ${m.fast_fail_outcome}`, short: 'Rapide', color: 'var(--accent)', value: m.fast_fail_ms, hint: 'Aucun octet n’est envoyé.' },
      ],
    }),
  );
  const final = BREAKER_STATES[m.breaker_state];
  return {
    figures: [
      { label: 'Échec lent', value: fmtMs(m.slow_fail_ms), tone: 'danger', hint: `${m.failures_before_open} tentatives épuisées` },
      { label: 'Échec rapide', value: fmtMs(m.fast_fail_ms), tone: 'success', hint: `${fmtTimes(m.fast_fail_speedup_x)} plus vite, ${m.fast_fail_calls} appels refusés` },
      { label: 'Après la reprise', value: final?.label ?? m.breaker_state, tone: m.recovered ? 'success' : 'danger', hint: m.recovered ? `rétabli à l’appel d’essai n° ${m.recovery_probes}` : 'service non rétabli' },
    ],
    chart: h('div.chaos-verdict__stack', timeline, h('p.chaos-verdict__note', 'Temps perdu avant de savoir que l’appel échoue (échelle logarithmique) :'), bars),
  };
}

function duplicateExecution(result) {
  const m = result.metrics;
  const column = ({ title, stock, removed, executions, attempts, tone }) =>
    h(
      'div.chaos-ledger__col',
      { dataset: { tone } },
      h('span.chaos-ledger__title', title),
      h('div.chaos-ledger__units', { 'aria-hidden': 'true' }, Array.from({ length: Math.max(0, removed) }, () => h('span.chaos-ledger__unit'))),
      h('span.chaos-ledger__stock.num', fmtNumber(stock)),
      h('span.chaos-ledger__delta.num', removed === 0 ? '±0' : `−${fmtNumber(removed)}`),
      h('span.chaos-ledger__hint', executions === undefined ? '' : `${executions} exécution${executions > 1 ? 's' : ''} pour ${attempts} tentative${attempts > 1 ? 's' : ''}`),
    );
  const chart = h(
    'div.chaos-ledger',
    { role: 'img', 'aria-label': `Stock avant ${m.stock_before}, attendu ${m.expected_after}, sans clé ${m.naive_after}, avec clé ${m.idempotent_after}` },
    column({ title: 'Avant', stock: m.stock_before, removed: 0, tone: 'neutral' }),
    column({ title: 'Attendu', stock: m.expected_after, removed: m.stock_before - m.expected_after, tone: 'info' }),
    column({ title: 'Sans clé', stock: m.naive_after, removed: m.stock_before - m.naive_after, executions: m.naive_executions, attempts: m.naive_attempts, tone: m.naive_after === m.expected_after ? 'success' : 'danger' }),
    column({ title: 'Avec clé', stock: m.idempotent_after, removed: m.stock_before - m.idempotent_after, executions: m.idempotent_executions, attempts: m.idempotent_attempts, tone: m.idempotent_after === m.expected_after ? 'success' : 'danger' }),
  );
  return {
    figures: [
      { label: 'Sans clé d’idempotence', value: `${m.naive_executions} exécution${m.naive_executions > 1 ? 's' : ''}`, tone: m.naive_executions > 1 ? 'danger' : 'success', hint: `stock ${fmtNumber(m.naive_after)} au lieu de ${fmtNumber(m.expected_after)}` },
      { label: 'Avec clé d’idempotence', value: `${m.idempotent_executions} exécution${m.idempotent_executions > 1 ? 's' : ''}`, tone: m.idempotent_executions === 1 ? 'success' : 'danger', hint: m.deduplicated ? 'rejeu reconnu par le serveur' : 'rejeu non reconnu' },
    ],
    chart,
  };
}

const BUILDERS = { latency_trap: latencyTrap, timeout_spike: timeoutSpike, connection_cut: connectionCut, server_outage: serverOutage, duplicate_execution: duplicateExecution };

/**
 * Panneau de verdict d'un scénario terminé.
 * @param {Object} result Résultat de `run_scenario` (`id`, `protocol`, `metrics`, `steps`, `verdict`, `lesson`).
 * @param {(chart: {destroy: () => void}) => *} track Enregistre un graphique à détruire au démontage.
 * @returns {HTMLElement}
 */
export function Verdict(result, track) {
  const built = BUILDERS[result.id]?.(result, track) ?? { figures: [], chart: null };
  return h(
    'div.chaos-verdict',
    built.figures.length ? h('div.chaos-figures', built.figures.map(figure)) : null,
    built.chart ? h('div.chaos-verdict__chart', built.chart) : null,
    h('p.chaos-verdict__sentence', h('span.chaos-verdict__proto', { style: { '--c': protocol(result.protocol).color } }, protocol(result.protocol).short), result.verdict),
    Callout({ tone: 'accent', icon: 'lightbulb', title: 'À retenir', text: result.lesson }),
  );
}
