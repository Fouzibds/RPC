/**
 * Console RPC — résultat d'un appel en flux : les éléments apparaissent à mesure que le
 * WebSocket les relaie (`stream`), puis vient le bilan (`stream_end`). Flux serveur : courbes en
 * direct ; flux bidirectionnel : une réponse en face de chaque envoi ; flux client : N messages,
 * un seul bilan.
 */

import { h, clear } from '../../core/dom.js';
import { fmtMs, fmtNumber } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { LineChart } from '../../components/charts.js';
import { JsonTree } from '../../components/jsontree.js';
import { Badge, Button, Stat } from '../../components/ui.js';
import { AttemptList, Banner, CallId, Insight, errorBadges, strong } from './parts.js';

const FIELD_LABELS = {
  orders_per_min: 'Commandes par minute',
  total_units: 'Unités en stock',
  inventory_value: 'Valeur du stock',
  low_stock_count: 'Produits en stock bas',
  operations: 'Opérations',
  stock: 'Stock',
};
const PREFERRED_FIELDS = ['orders_per_min', 'total_units'];
const MAX_ROWS = 500;
/** Silence toléré, au-delà du délai de l'appel, avant de déclarer le flux muet. */
const STALL_MARGIN_MS = 4000;

/** Champs numériques d'un élément qui méritent une courbe (ni rang, ni horodatage). */
function chartFields(item) {
  if (!item || typeof item !== 'object') return [];
  const numeric = Object.keys(item).filter((key) => typeof item[key] === 'number' && !/^(seq|timestamp.*|.*_at_ms)$/.test(key));
  const preferred = PREFERRED_FIELDS.filter((key) => numeric.includes(key));
  return (preferred.length ? preferred : numeric).slice(0, 2);
}

function preview(item) {
  const text = typeof item === 'string' ? item : JSON.stringify(item);
  return text.length > 220 ? `${text.slice(0, 220)}…` : text;
}

/** Aperçu d'un élément reçu : ses champs, sans le rang ni l'horodatage (déjà affichés). */
function fieldsPreview(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return preview(item);
  const rank = (key) => (PREFERRED_FIELDS.includes(key) ? PREFERRED_FIELDS.indexOf(key) : PREFERRED_FIELDS.length);
  return Object.entries(item)
    .filter(([key]) => !/^(seq|timestamp.*)$/.test(key))
    .sort(([a], [b]) => rank(a) - rank(b))
    .map(([key, value]) => h('span.console-feed__field', h('span.console-feed__key', key), typeof value === 'number' ? fmtNumber(value) : typeof value === 'string' ? value : JSON.stringify(value)));
}

/**
 * Vue d'un appel en flux. Les messages du WebSocket lui sont remis par `handle`.
 * @param {Object} props
 * @param {Object} props.request Corps envoyé à `POST /api/call`.
 * @param {Object} props.spec `MethodSpec` de la procédure.
 * @param {(node: T) => T} props.track Enregistre un composant à détruire.
 * @param {(summary: {ok: boolean, count: number, durationMs: number, code: string, callId: string, detached?: boolean}) => void} props.onDone
 *   Appelé une fois, quand le flux se termine, se tait ou est abandonné.
 * @returns {HTMLElement & {handle: (message: Object) => void, interrupt: (reason: string) => void, destroy: () => void}}
 * @template T
 */
export function StreamResult({ request, spec, track, onDone }) {
  const info = protocol(request.protocol);
  const params = request.params ?? {};
  const kind = spec.kind;
  const sentList = kind === 'bidi_stream' ? (params.product_ids ?? []) : kind === 'client_stream' ? (params.updates ?? []) : [];
  const expected = kind === 'server_stream' ? (params.samples ?? null) : kind === 'bidi_stream' ? sentList.length : null;
  const requestedInterval = params.interval_ms ?? null;

  const startedAt = performance.now();
  let lastActivity = startedAt;
  let count = 0;
  let firstElapsed = null;
  let lastElapsed = null;
  let finished = false;
  let charts = null;

  const bannerSlot = h('div', { 'aria-live': 'polite' });
  const whole = (v) => fmtNumber(v, { decimals: 0 });
  const upload = kind === 'client_stream';
  const sentStat = sentList.length ? Stat({ label: upload ? 'Messages envoyés' : 'Références envoyées', icon: 'arrow-up-right', value: sentList.length, format: whole, hint: 'par le client, sur un seul flux' }) : null;
  const itemsStat = upload
    ? Stat({ label: 'Réponses', icon: 'arrow-down-left', value: 0, format: whole, hint: 'un flux client ne renvoie qu’un bilan' })
    : Stat({ label: 'Éléments reçus', icon: 'inbox', value: 0, format: whole, hint: expected !== null ? `sur ${fmtNumber(expected)} attendus` : '' });
  const elapsedStat = Stat({ label: 'Écoulé', icon: 'timer', value: 0, format: fmtMs, hint: 'depuis l’ouverture du flux' });
  const intervalStat = Stat({ label: 'Intervalle moyen', icon: 'ruler', value: '—', format: fmtMs, hint: requestedInterval !== null ? `demandé : ${fmtMs(requestedInterval)}` : 'entre deux éléments' });
  const firstStat = Stat({ label: 'Premier élément', icon: 'zap', value: '—', format: fmtMs, hint: 'délai avant la première réponse' });
  const chartsSlot = h('div.console-stream__charts');
  const feed = h('ol.console-feed', { 'aria-label': 'Éléments reçus' });
  const feedEmpty = h('p.console-feed__empty', 'En attente du premier élément…');
  const feedWrap = h('div.console-feed-wrap.scroll-y', feedEmpty, feed);
  const body = h('div.console-stream__body');
  const finalSlot = h('div.console-stream__final');

  /* --- Corps selon la forme du flux -------------------------------------------------- */
  const pairRows = [];
  if (kind === 'bidi_stream') {
    const rows = sentList.map((productId, index) => {
      const answer = h('div.console-pair__answer', h('span.console-pair__pending', 'en attente…'));
      pairRows.push(answer);
      return h('li.console-pair', h('span.console-pair__n.num', String(index + 1)), h('span.console-pair__sent.mono', String(productId)), h('span.console-pair__arrow', { 'aria-hidden': 'true' }, icon('arrow-right', { size: 14 })), answer);
    });
    body.append(h('div.console-pairs__head', h('span.t-label', 'Envoyé par le client'), h('span.t-label', 'Répondu par le serveur, sur le même flux')), h('ol.console-pairs', rows));
  } else if (kind === 'client_stream') {
    body.append(
      h(
        'div.console-upload',
        h('div.console-upload__side', h('span.t-label', `${fmtNumber(sentList.length)} messages envoyés par le client`), h('ol.console-upload__list', sentList.map((update) => h('li.mono', preview(update))))),
        h('span.console-upload__arrow', { 'aria-hidden': 'true' }, icon('arrow-right', { size: 16 })),
        h('div.console-upload__side', h('span.t-label', 'Une seule réponse du serveur'), finalSlot),
      ),
    );
  } else {
    body.append(chartsSlot, h('div.console-feed__head', h('span.t-label', 'Éléments reçus'), h('span.console-feed__legend', 'rang · arrivée · écart avec le précédent · champs')), feedWrap);
  }

  const stats = upload ? [sentStat, itemsStat, elapsedStat] : kind === 'bidi_stream' ? [sentStat, itemsStat, elapsedStat, intervalStat] : [itemsStat, elapsedStat, intervalStat, firstStat];
  const el = h('div.console-result.console-stream', bannerSlot, h('div.console-stats', stats), body, upload ? null : finalSlot);

  /* --- Bandeau ------------------------------------------------------------------------ */
  function running() {
    clear(
      bannerSlot,
      Banner({
        tone: 'info',
        busy: true,
        title: 'Flux en cours',
        text: [h('span.mono', request.method), ` par ${info.label} : chaque élément est relayé par le WebSocket dès que le client le reçoit.`],
        aside: Button({ label: 'Ne plus suivre', size: 'sm', variant: 'ghost', icon: 'eye-off', onClick: () => stop({ tone: 'neutral', title: 'Flux détaché', text: 'La console ne suit plus ce flux ; il se termine côté serveur.' }, { detached: true }) }),
      }),
    );
  }

  /* --- Horloge ------------------------------------------------------------------------- */
  const stallLimit = (request.timeout_ms ?? 5000) + (requestedInterval ?? 0) + STALL_MARGIN_MS;
  const ticker = window.setInterval(() => {
    const now = performance.now();
    elapsedStat.update({ value: now - startedAt });
    if (now - lastActivity > stallLimit) {
      stop({ tone: 'warning', title: 'Le flux ne répond plus', text: `Aucun message depuis ${fmtMs(now - lastActivity)} : le WebSocket a pu perdre la fin du flux. Relancez l’appel.` }, { code: 'STALLED' });
    }
  }, 100);

  function stop(banner, { detached = false, code = 'DETACHED' } = {}) {
    if (finished) return;
    finished = true;
    window.clearInterval(ticker);
    clear(bannerSlot, Banner(banner));
    onDone({ ok: false, count, durationMs: performance.now() - startedAt, code, callId: '', detached });
  }

  /* --- Éléments ------------------------------------------------------------------------- */
  function ensureCharts(item) {
    if (charts !== null || kind !== 'server_stream') return;
    charts = chartFields(item).map((field) => {
      const chart = track(
        LineChart({
          series: [{ id: field, label: FIELD_LABELS[field] ?? field, protocol: request.protocol, points: [] }],
          xFormat: (x) => fmtMs(x, { decimals: x >= 1000 ? 1 : 0 }),
          yFormat: (y) => fmtNumber(y),
          area: true,
          yZero: false,
          height: 168,
          maxPoints: Math.max(120, expected ?? 0),
          slideMs: Math.min(320, Math.max(0, requestedInterval ?? 320)),
          ariaLabel: `${FIELD_LABELS[field] ?? field} au fil du flux`,
        }),
      );
      chartsSlot.append(h('figure.console-stream__chart', h('figcaption', h('span.console-stream__chart-title', FIELD_LABELS[field] ?? field), h('span.mono.console-stream__chart-key', field)), chart));
      return { field, chart };
    });
  }

  function onItem(message) {
    const item = message.item;
    const elapsed = message.elapsed_ms;
    const gap = lastElapsed === null ? null : elapsed - lastElapsed;
    count += 1;
    lastActivity = performance.now();
    if (firstElapsed === null) {
      firstElapsed = elapsed;
      firstStat.update({ value: elapsed });
    }
    lastElapsed = elapsed;
    itemsStat.update({ value: count });
    if (count > 1) intervalStat.update({ value: (lastElapsed - firstElapsed) / (count - 1) });

    if (kind === 'bidi_stream') {
      const slot = pairRows[count - 1];
      if (slot) {
        clear(
          slot,
          h('span.mono.console-pair__id', item?.product_id ?? ''),
          item?.available === false ? Badge({ label: 'introuvable', tone: 'danger', size: 'sm' }) : Badge({ label: `${fmtNumber(item?.stock)} en stock`, tone: 'success', size: 'sm', mono: true }),
          item?.warehouse ? h('span.console-pair__where.mono', item.warehouse) : null,
          h('span.console-pair__time.num', `+${fmtMs(elapsed)}`),
        );
        slot.dataset.done = 'true';
      }
      return;
    }

    ensureCharts(item);
    for (const { field, chart } of charts ?? []) if (Number.isFinite(item?.[field])) chart.push(elapsed, { [field]: item[field] });

    feedEmpty.hidden = true;
    const pinned = feedWrap.scrollTop + feedWrap.clientHeight >= feedWrap.scrollHeight - 24;
    feed.append(
      h('li.console-feed__row', h('span.console-feed__seq.num', `#${message.seq ?? count}`), h('span.console-feed__time.num', `+${fmtMs(elapsed)}`), h('span.console-feed__gap.num', gap === null ? '' : `Δ ${fmtMs(gap)}`), h('span.console-feed__item.mono', { title: preview(item) }, fieldsPreview(item))),
    );
    while (feed.childElementCount > MAX_ROWS) feed.firstElementChild.remove();
    if (pinned) feedWrap.scrollTop = feedWrap.scrollHeight;
  }

  /* --- Fin du flux ------------------------------------------------------------------------ */
  function summary(message) {
    const duration = message.duration_ms;
    if (kind === 'client_stream') {
      const result = message.result ?? {};
      return [strong(`${fmtNumber(sentList.length)} messages`), ' envoyés sur un seul flux, ', strong('1 réponse'), ` en ${fmtMs(duration)}`, Number.isFinite(result.applied) ? [' : ', strong(`${result.applied} appliqué${result.applied > 1 ? 's' : ''}`), ', ', strong(`${result.rejected ?? 0} rejeté${(result.rejected ?? 0) > 1 ? 's' : ''}`), '.'] : '.'];
    }
    if (kind === 'bidi_stream') {
      return [strong(`${fmtNumber(sentList.length)} références`), ' envoyées, ', strong(`${fmtNumber(count)} réponses`), ' reçues sur le même flux en ', strong(fmtMs(duration)), ' : une seule connexion, au lieu d’un aller-retour par référence.'];
    }
    const mean = count > 1 ? (lastElapsed - firstElapsed) / (count - 1) : null;
    return [strong(`${fmtNumber(count)} éléments`), ' pour ', strong('une seule requête'), ', en ', strong(fmtMs(duration)), mean !== null ? [' : un élément toutes les ', strong(fmtMs(mean)), requestedInterval !== null ? ` en moyenne (${fmtMs(requestedInterval)} demandés).` : ' en moyenne.'] : '.', ` En unaire, il aurait fallu ${fmtNumber(count)} allers-retours.`];
  }

  function onEnd(message) {
    if (finished) return;
    finished = true;
    window.clearInterval(ticker);
    const duration = fmtMs(message.duration_ms);
    elapsedStat.update({ value: message.duration_ms, hint: 'durée totale du flux' });
    feedEmpty.hidden = count > 0 || kind !== 'server_stream';
    if (upload && !message.error) itemsStat.update({ value: 1 });
    const error = message.error;
    clear(
      bannerSlot,
      error
        ? Banner({ tone: 'danger', title: count ? `Flux interrompu après ${fmtNumber(count)} élément${count > 1 ? 's' : ''}` : 'Le flux n’a pas pu s’ouvrir', text: error.message, badges: errorBadges(error), aside: CallId(message.call_id) })
        : Banner({ tone: 'success', title: 'Flux terminé', text: [h('span.mono', request.method), ` par ${info.label} : ${upload ? `${fmtNumber(sentList.length)} messages envoyés, 1 bilan reçu` : `${fmtNumber(count)} élément${count > 1 ? 's' : ''}`} en ${duration}.`], aside: CallId(message.call_id) }),
    );
    for (const slot of pairRows) {
      if (!slot.dataset.done) clear(slot, h('span.console-pair__pending', 'aucune réponse'));
    }
    clear(
      finalSlot,
      message.result !== null && message.result !== undefined ? track(JsonTree(message.result, { rootLabel: 'bilan', collapsedDepth: 2, maxHeight: 260 })) : null,
      message.attempts?.length > 1 ? AttemptList(message.attempts) : null,
    );
    if (!error) el.append(Insight(summary(message)));
    onDone({ ok: !error, count: upload ? sentList.length : count, durationMs: message.duration_ms, code: error?.code ?? 'OK', callId: message.call_id ?? '' });
  }

  el.handle = (message) => {
    if (finished) return;
    if (message.type === 'stream') onItem(message);
    else if (message.type === 'stream_end') onEnd(message);
  };
  el.interrupt = (reason) => stop({ tone: 'warning', title: 'Flux interrompu', text: reason }, { code: 'INTERRUPTED' });
  el.destroy = () => {
    finished = true;
    window.clearInterval(ticker);
  };

  running();
  return el;
}
