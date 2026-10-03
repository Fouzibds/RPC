/**
 * Chaos réseau — flux compact des évènements du proxy (`network.*`) et du client résilient
 * (`resilience.*`), tels que le WebSocket les publie, et compteurs de pannes injectées.
 */

import { h, clear } from '../../core/dom.js';
import { fmtMs, fmtNumber, fmtTime } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { Card, EmptyState, IconButton, Toggle } from '../../components/ui.js';
import { BREAKER_STATES, STAGE_INFO, frameBatch } from './model.js';

const MAX_ROWS = 120;

const COUNTERS = [
  { key: 'resets', label: 'Coupures' },
  { key: 'refused', label: 'Refus' },
  { key: 'blackholed', label: 'Avalés' },
  { key: 'lost_replies', label: 'Réponses perdues' },
];

/** Évènements sans histoire, masqués par défaut : retard ordinaire, premier essai réussi. */
function isRoutine(event) {
  const detail = event.detail ?? {};
  if (event.stage === 'network.delay') return !detail.spike;
  return event.stage === 'resilience.attempt' && detail.outcome === 'ok' && Number(detail.n) === 1;
}

/** Texte, chiffre et ton d'un évènement. */
function describe(event) {
  const detail = event.detail ?? {};
  const info = STAGE_INFO[event.stage];
  switch (event.stage) {
    case 'network.delay':
      return {
        tone: detail.spike ? 'warning' : 'neutral',
        label: detail.spike ? 'Pic' : info.label,
        text: detail.spike ? 'Pic de latence : la requête est retenue par le réseau.' : `${detail.direction === 'up' ? 'Requête' : 'Réponse'} retardée par le réseau.`,
        figure: fmtMs(detail.delay_ms),
      };
    case 'resilience.attempt':
      return {
        tone: detail.outcome === 'ok' ? 'success' : detail.code === 'TIMEOUT' ? 'warning' : 'danger',
        label: `${info.label} ${detail.n}${detail.max_attempts ? `/${detail.max_attempts}` : ''}`,
        text: detail.outcome === 'ok' ? `${event.method} : le serveur a répondu.` : `${event.method} : ${detail.message || detail.code}`,
        figure: detail.outcome === 'ok' ? fmtMs(detail.duration_ms) : detail.code,
      };
    case 'resilience.backoff':
      return { tone: info.tone, label: info.label, text: `Avant la tentative n° ${detail.next_attempt}, après ${detail.code}.`, figure: fmtMs(detail.backoff_ms) };
    case 'resilience.breaker':
      return { tone: BREAKER_STATES[detail.to]?.tone ?? info.tone, label: info.label, text: detail.text, figure: BREAKER_STATES[detail.to]?.label ?? detail.to };
    case 'resilience.give_up':
      return { tone: info.tone, label: info.label, text: detail.text, figure: detail.code };
    default:
      return { tone: info.tone, label: info.label, text: detail.text ?? '', figure: detail.bytes ? `${fmtNumber(detail.bytes)}\u202Fo` : '' };
  }
}

/**
 * Carte « Évènements en direct ».
 * @param {{store: Object, ws: Object}} ctx Contexte de la page.
 * @returns {{el: HTMLElement, clear: () => void, destroy: () => void}}
 */
export function Feed(ctx) {
  const { store, ws } = ctx;
  let queue = [];
  let showAll = false;

  const list = h('div.chaos-feed__list', { role: 'log', 'aria-label': 'Évènements réseau et résilience', tabIndex: 0 });
  const empty = EmptyState({ icon: 'radio', size: 'sm', title: 'Aucun évènement', text: 'Coupures, réponses perdues, attentes et décisions du disjoncteur défileront ici.' });
  const counters = COUNTERS.map((counter) => {
    const value = h('span.chaos-feed__count.num', '—');
    return { ...counter, value, el: h('div.chaos-feed__counter', value, h('span.chaos-feed__counter-label', counter.label)) };
  });

  const el = Card(
    {
      title: 'Évènements en direct',
      subtitle: 'Pannes injectées par le proxy, décisions du client résilient',
      icon: 'radio',
      padding: 'none',
      class: 'chaos-card chaos-card--feed',
      actions: [
        h('span', { title: 'Affiche aussi les retards ordinaires et les premières tentatives réussies' }, Toggle({ size: 'sm', label: 'Tout', checked: showAll, onChange: (on) => (showAll = on) })),
        IconButton({ icon: 'trash-2', label: 'Vider le flux', size: 'sm', onClick: () => reset() }),
      ],
    },
    h('div.chaos-feed', h('div.chaos-feed__counters', { title: 'Pannes injectées par les trois proxys depuis le démarrage du laboratoire' }, counters.map((counter) => counter.el)), empty, list),
  );

  function row(event) {
    const view = describe(event);
    const info = STAGE_INFO[event.stage];
    return h(
      'div.chaos-feed__row',
      { dataset: { tone: view.tone } },
      h('span.chaos-feed__time.num', fmtTime(event.ts, { ms: true })),
      h('span.chaos-feed__proto', { style: { '--c': protocol(event.protocol).color }, title: protocol(event.protocol).label }, protocol(event.protocol).short),
      h('span.chaos-feed__kind', icon(info.icon, { size: 13 }), h('span', view.label)),
      h('span.chaos-feed__text', { title: view.text }, view.text),
      h('span.chaos-feed__figure.num', view.figure ?? ''),
    );
  }

  const batch = frameBatch(() => {
    const fresh = queue;
    queue = [];
    if (!fresh.length) return;
    const fragment = document.createDocumentFragment();
    for (let index = fresh.length - 1; index >= 0; index -= 1) fragment.append(row(fresh[index]));
    list.prepend(fragment);
    while (list.childElementCount > MAX_ROWS) list.lastElementChild.remove();
    empty.hidden = true;
    list.hidden = false;
  });

  function push(message) {
    const event = message.event;
    if (!event || !STAGE_INFO[event.stage]) return;
    if (!showAll && isRoutine(event)) return;
    queue.push(event);
    if (queue.length > MAX_ROWS) queue = queue.slice(-MAX_ROWS);
    batch.schedule();
  }

  function reset() {
    queue = [];
    clear(list);
    list.hidden = true;
    empty.hidden = false;
  }

  function renderCounters(proxies) {
    for (const counter of counters) {
      const total = proxies ? Object.values(proxies).reduce((sum, stats) => sum + Number(stats?.[counter.key] ?? 0), 0) : null;
      counter.value.textContent = total === null ? '—' : fmtNumber(total);
      counter.el.dataset.active = String(Boolean(total));
    }
  }

  const offs = [ws.on('network', push), ws.on('resilience', push), store.subscribe((state) => state.status?.proxies ?? null, renderCounters, { immediate: true })];
  reset();

  return {
    el,
    clear: reset,
    destroy() {
      batch.cancel();
      for (const off of offs) off();
    },
  };
}
