/**
 * Chaos réseau — jauges en direct (taux de succès, médiane et p95, délais dépassés, nouvelles
 * tentatives, état du disjoncteur) et carte « Local vs distant ». Les jauges portent sur les
 * derniers appels logiques lancés depuis cette page ; rien n'est affiché tant qu'aucun n'a eu lieu.
 */

import { h, clear } from '../../core/dom.js';
import { fmtMs, fmtNumber, fmtPercent, fmtUs, typo } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';
import { Donut, Sparkline, StackedBar } from '../../components/charts.js';
import { Badge, Button, Callout, Card, ProtocolChip, Skeleton } from '../../components/ui.js';
import { BREAKER_STATES, GAUGE_WINDOW, conditionsSummary, errorText, fmtTimes, percentile } from './model.js';

const LOCAL_SAMPLES = 5;

function metric(label, hint) {
  const value = h('span.chaos-metric__value.num', '—');
  const hintEl = h('span.chaos-metric__hint', hint ?? '');
  const el = h('div.chaos-metric', h('span.chaos-metric__label', label), value, hintEl);
  return {
    el,
    set(text, { tone, hint: nextHint } = {}) {
      value.textContent = text;
      if (tone) el.dataset.tone = tone;
      else delete el.dataset.tone;
      if (nextHint !== undefined) hintEl.textContent = nextHint;
    },
  };
}

/**
 * Carte des jauges en direct.
 * @param {{store: Object, ws: Object}} ctx Contexte de la page.
 * @returns {{el: HTMLElement, record: (response: Object) => void, setProtocol: (id: string) => void, clear: () => void, destroy: () => void}}
 */
export function Gauges(ctx) {
  const { store, ws } = ctx;
  let records = [];
  let selected = 'custom';
  const breakers = {};

  const donut = Donut({ value: 0, size: 96, thickness: 8, label: 'succès', tone: 'neutral', format: (value) => (records.length ? fmtPercent(value, { decimals: 0 }) : '—'), ariaLabel: 'Taux de succès' });
  const spark = Sparkline({ values: [], type: 'bars', width: 76, height: 20, tone: 'neutral', highlight: 'none', format: (value) => fmtMs(value), ariaLabel: 'Durée des derniers appels' });
  const median = metric('Médiane');
  const p95 = metric('p95');
  const timeouts = metric('Échéances dépassées');
  const retries = metric('Nouvelles tentatives');
  const breakerBadge = Badge({ label: '—', tone: 'neutral', dot: true });
  const breakerHint = h('span.chaos-metric__hint', 'non sollicité');
  const breaker = h('div.chaos-metric.chaos-metric--breaker', h('span.chaos-metric__label', 'Disjoncteur'), h('span.chaos-metric__badge', breakerBadge), breakerHint);
  const sample = h('span.chaos-gauges__sample', 'aucun appel');
  const outcomes = StackedBar({ segments: [], thickness: 8, format: (value) => fmtNumber(value), ariaLabel: 'Issue des derniers appels' });

  const el = Card(
    { title: 'Jauges', subtitle: `Sur les ${GAUGE_WINDOW} derniers appels du client`, icon: 'gauge', class: 'chaos-card chaos-card--gauges', actions: sample },
    h('div.chaos-outcomes', h('span.chaos-metric__label', 'Issue de chaque appel'), outcomes),
    h(
      'div.chaos-gauges',
      { 'aria-live': 'off' },
      h('div.chaos-gauges__donut', donut),
      h('div.chaos-gauges__grid', median.el, p95.el, breaker, timeouts.el, retries.el, h('div.chaos-metric.chaos-metric--spark', h('span.chaos-metric__label', 'Durées'), spark, h('span.chaos-metric__hint', 'chronologiques'))),
    ),
  );

  function renderBreaker() {
    const snapshot = breakers[selected];
    const info = BREAKER_STATES[snapshot?.state];
    if (!info) {
      breakerBadge.setLabel('—');
      breakerBadge.setTone('neutral');
      breakerHint.textContent = 'non sollicité';
      return;
    }
    breakerBadge.setLabel(info.label);
    breakerBadge.setTone(info.tone);
    if (snapshot.state === 'open' && Number(snapshot.retry_in_s) > 0) breakerHint.textContent = `essai dans ${fmtNumber(snapshot.retry_in_s, { maxDecimals: 1 })}\u202Fs`;
    else if (snapshot.state === 'closed' && snapshot.failure_threshold) breakerHint.textContent = `${fmtNumber(snapshot.failures ?? 0)}/${fmtNumber(snapshot.failure_threshold)} échecs`;
    else breakerHint.textContent = info.hint;
  }

  function render() {
    const total = records.length;
    sample.textContent = total ? `${fmtNumber(total)} appel${total > 1 ? 's' : ''}` : 'aucun appel';
    if (!total) {
      donut.update({ value: 0, tone: 'neutral' });
      spark.update({ values: [] });
      outcomes.update({ segments: [] });
      for (const cell of [median, p95, timeouts, retries]) cell.set('—', { hint: '' });
      renderBreaker();
      return;
    }
    const ok = records.filter((record) => record.ok).length;
    const ratio = ok / total;
    const durations = records.map((record) => record.durationMs);
    const lost = records.filter((record) => record.code === 'TIMEOUT').length;
    const expired = records.reduce((sum, record) => sum + record.timeouts, 0);
    const again = records.reduce((sum, record) => sum + Math.max(0, record.attempts - 1), 0);
    const refused = records.filter((record) => record.code === 'CIRCUIT_OPEN').length;
    donut.update({ value: ratio, tone: ratio >= 0.99 ? 'success' : ratio >= 0.8 ? 'warning' : 'danger' });
    spark.update({ values: durations });
    outcomes.update({
      segments: [
        { id: 'ok', label: 'Succès', value: ok, tone: 'success' },
        { id: 'timeout', label: 'Échéances dépassées', value: lost, tone: 'warning' },
        { id: 'error', label: 'Erreurs', value: total - ok - lost - refused, tone: 'danger' },
        { id: 'refused', label: 'Refus du disjoncteur', value: refused, tone: 'neutral', hint: 'Aucun octet envoyé : l’appel échoue aussitôt.' },
      ],
    });
    median.set(fmtMs(percentile(durations, 0.5)), { hint: 'échecs inclus' });
    p95.set(fmtMs(percentile(durations, 0.95)), { hint: '5\u202F% font pire' });
    timeouts.set(fmtNumber(expired), { tone: expired ? 'warning' : undefined, hint: expired ? 'tentatives expirées' : 'aucune' });
    retries.set(fmtNumber(again), { tone: again ? 'info' : undefined, hint: refused ? `${fmtNumber(refused)} refus` : again ? 'après un échec' : 'aucune' });
    renderBreaker();
  }

  function record(response) {
    if (!response) return;
    if (response.breaker) breakers[response.protocol] = response.breaker;
    const expiredIn = (attempts) => (attempts ?? []).filter((attempt) => attempt.code === 'TIMEOUT').length;
    const fresh =
      response.mode === 'async'
        ? (response.calls ?? []).map((call) => ({ ok: call.ok, durationMs: call.duration_ms, code: call.error?.code ?? 'OK', attempts: call.attempts ?? 1, timeouts: call.error?.code === 'TIMEOUT' ? 1 : 0 }))
        : [{ ok: response.ok, durationMs: response.duration_ms, code: response.error?.code ?? 'OK', attempts: response.attempts?.length ?? 0, timeouts: expiredIn(response.attempts) }];
    records = [...records, ...fresh].slice(-GAUGE_WINDOW);
    render();
  }

  const offs = [
    ws.on('resilience', (message) => {
      const event = message.event;
      if (event?.stage !== 'resilience.breaker' || !event.detail?.to) return;
      breakers[event.protocol] = { ...(breakers[event.protocol] ?? {}), state: event.detail.to, failures: event.detail.failures, failure_threshold: event.detail.failure_threshold, retry_in_s: event.detail.to === 'open' ? event.detail.reset_timeout_s : 0 };
      if (event.protocol === selected) renderBreaker();
    }),
    store.subscribe(
      (state) => state.stats?.breakers ?? null,
      (snapshots) => {
        if (!snapshots) return;
        for (const id of Object.keys(breakers)) if (!(id in snapshots)) delete breakers[id];
        Object.assign(breakers, snapshots);
        renderBreaker();
      },
      { immediate: true },
    ),
  ];
  render();

  return {
    el,
    record,
    setProtocol(id) {
      selected = id;
      renderBreaker();
    },
    clear() {
      records = [];
      render();
    },
    destroy() {
      for (const off of offs) off();
      donut.destroy();
      spark.destroy();
      outcomes.destroy();
    },
  };
}

/**
 * Carte « Local vs distant » : la même procédure, appelée en mémoire puis à travers le réseau
 * tel qu'il est réglé en ce moment.
 * @param {{store: Object, api: Object}} ctx Contexte de la page.
 * @param {{onMeasureRequested: () => void}} hooks
 * @returns {{el: HTMLElement, setSelection: (selection: Object) => void, record: (response: Object) => void, setBusy: (on: boolean) => void, destroy: () => void}}
 */
export function LocalVsRemote(ctx, hooks) {
  const { store, api } = ctx;
  let selection = null;
  let local = { state: 'loading' };
  let remote = null;
  let token = 0;
  let destroyed = false;

  const measure = Button({ label: 'Mesurer', icon: 'crosshair', size: 'sm', title: 'Refaire la mesure locale et lancer un appel distant', onClick: () => { measureLocal(); hooks.onMeasureRequested?.(); } });
  const body = h('div.chaos-compare', { 'aria-live': 'polite' });
  const totals = h('p.chaos-compare__totals');
  const el = Card({ title: 'Local vs distant', subtitle: 'Même appel, avec et sans réseau', icon: 'scale', class: 'chaos-card chaos-card--compare', actions: measure }, body, totals);

  function row({ id, name, value, sub, share, tone }) {
    return h(
      'div.chaos-compare__row',
      { dataset: { tone: tone ?? '' } },
      h('div.chaos-compare__head', ProtocolChip(id, { short: true, size: 'sm' }), h('span.chaos-compare__name', name)),
      h('div.chaos-compare__value.num', value),
      h('div.chaos-compare__bar', { title: 'Échelle logarithmique' }, h('span', { style: { width: `${Math.max(1.5, Math.min(100, share * 100))}%`, background: protocol(id).color } })),
      h('p.chaos-compare__sub', { title: sub }, sub),
    );
  }

  function render() {
    if (destroyed || !selection) return;
    if (local.state === 'loading') {
      clear(body, Skeleton({ variant: 'block', height: 56 }), Skeleton({ variant: 'block', height: 56 }), Skeleton({ lines: 2 }));
      return;
    }
    if (local.state === 'error') {
      clear(
        body,
        Callout({
          tone: local.offline ? 'warning' : 'danger',
          title: local.offline ? 'Laboratoire injoignable' : 'Mesure locale impossible',
          text: local.offline ? 'La comparaison reprendra dès que le laboratoire répondra.' : local.message,
          actions: Button({ label: 'Réessayer', icon: 'refresh-cw', size: 'sm', onClick: () => measureLocal() }),
        }),
      );
      return;
    }
    const usable = remote && remote.ok;
    const high = usable ? Math.max(remote.durationMs, local.ms) : local.ms;
    const low = Math.min(local.ms, usable ? remote.durationMs : local.ms) / 4;
    const share = (value) => (high > low ? Math.log(value / low) / Math.log(high / low) : 1);
    const rows = [
      row({ id: 'local', name: 'En mémoire', value: fmtUs(local.ms * 1000), sub: `Médiane de ${local.count} appels, sans réseau`, share: share(local.ms) }),
      remote
        ? row({
            id: remote.protocol,
            name: 'Via le réseau',
            value: remote.ok ? fmtMs(remote.durationMs) : remote.code,
            sub: remote.ok ? `Dernier appel · ${remote.conditions}${remote.attempts > 1 ? ` · ${remote.attempts} tentatives` : ''}` : `Échec après ${fmtMs(remote.durationMs)} · ${remote.conditions}`,
            share: remote.ok ? share(remote.durationMs) : 1,
            tone: remote.ok ? '' : 'danger',
          })
        : h('div.chaos-compare__row.chaos-compare__row--empty', h('div.chaos-compare__head', ProtocolChip(selection.protocol, { short: true, size: 'sm' }), h('span.chaos-compare__name', 'Via le réseau')), h('p.chaos-compare__sub', 'Aucun appel distant pour l’instant : « Mesurer » en lance un.')),
    ];
    let verdict;
    if (usable) {
      const ratio = remote.durationMs / local.ms;
      verdict = h('div.chaos-compare__verdict', h('span.chaos-compare__ratio.num', fmtTimes(ratio)), h('span.chaos-compare__phrase', ratio >= 1 ? 'plus lent qu’en mémoire' : 'du temps de l’appel en mémoire'));
    } else if (remote) {
      verdict = h('div.chaos-compare__verdict', { dataset: { tone: 'danger' } }, h('span.chaos-compare__ratio', 'Échec'), h('span.chaos-compare__phrase', `${remote.message || remote.code} — une issue qu’un appel local ne connaît pas.`));
    } else {
      verdict = null;
    }
    clear(body, rows, verdict);
  }

  function renderTotals(status) {
    const all = status?.totals;
    if (!all || !selection) {
      totals.hidden = true;
      return;
    }
    const parts = ['local', selection.protocol].filter((id) => all[id]?.calls > 0).map((id) => `${protocol(id).short.toLowerCase() === 'local' ? 'local' : protocol(id).short} ${fmtMs(all[id].avg_ms)}`);
    totals.hidden = !parts.length;
    totals.textContent = parts.length ? typo(`Moyennes du laboratoire depuis son démarrage : ${parts.join(' · ')}.`) : '';
    totals.title = ['local', selection.protocol].filter((id) => all[id]?.calls > 0).map((id) => `${protocol(id).label} : ${fmtNumber(all[id].calls)} appels`).join(' · ');
  }

  async function measureLocal() {
    if (!selection) return;
    const mine = (token += 1);
    if (local.state !== 'ready') {
      local = { state: 'loading' };
      render();
    }
    measure.setLoading(true);
    try {
      const samples = [];
      for (let index = 0; index < LOCAL_SAMPLES; index += 1) {
        const response = await api.post('/api/call', { protocol: 'local', method: selection.method, params: selection.params, mode: 'sync' }, { timeoutMs: 8000 });
        if (mine !== token || destroyed) return;
        if (!response.ok) throw new Error(errorText(response.error));
        samples.push(response.duration_ms);
      }
      local = { state: 'ready', ms: percentile(samples, 0.5), count: samples.length };
    } catch (error) {
      if (mine !== token || destroyed) return;
      local = { state: 'error', offline: Boolean(error?.offline), message: errorText(error) };
    } finally {
      if (mine === token && !destroyed) measure.setLoading(false);
    }
    render();
  }

  const offs = [
    store.subscribe('status', renderTotals, { immediate: true }),
    store.subscribe('api', (state, previous) => {
      if (state === 'online' && previous !== 'online' && local.state === 'error') measureLocal();
    }),
  ];

  return {
    el,
    setSelection(next) {
      const changed = !selection || selection.method !== next.method;
      const moved = !selection || selection.protocol !== next.protocol;
      selection = next;
      if (changed || moved) remote = null;
      renderTotals(store.get().status);
      if (changed) {
        local = { state: 'loading' };
        measureLocal();
      }
      render();
    },
    record(response) {
      if (!selection || response.mode === 'async' || response.method !== selection.method || response.protocol !== selection.protocol) return;
      remote = { ok: response.ok, protocol: response.protocol, durationMs: response.duration_ms, attempts: response.attempts?.length ?? 0, code: response.error?.code ?? 'OK', message: response.error?.message ?? '', conditions: conditionsSummary(store.get().network) };
      render();
    },
    setBusy(on) {
      measure.setDisabled(on);
    },
    destroy() {
      destroyed = true;
      token += 1;
      for (const off of offs) off();
    },
  };
}
