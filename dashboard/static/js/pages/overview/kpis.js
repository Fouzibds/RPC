/**
 * Vue d'ensemble — les quatre indicateurs en direct : appels, latence, octets, erreurs.
 * Les valeurs viennent des cumuls de `/api/status` (rafraîchis chaque seconde par le message
 * `stats`) ; les mini-courbes retracent les 60 dernières secondes de débit.
 */

import { h } from '../../core/dom.js';
import { fmtBytes, fmtMs, fmtNumber, fmtPercent, fmtRate, fmtUs } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { MiniBars, Sparkline } from '../../components/charts.js';
import { Card, Col, Grid, Skeleton, Stat } from '../../components/ui.js';
import { sumRates, sumTotals } from './model.js';

const WINDOW = 60;
const SPARK = { width: 88, height: 30 };

function plural(count, one, many) {
  return `${fmtNumber(count)} ${count > 1 ? many : one}`;
}

function bytesHint(out, incoming) {
  return h(
    'span.ov-kpi__flows',
    h('span.ov-kpi__flow', { title: 'Octets envoyés par les clients (requêtes)' }, icon('arrow-up-right', { size: 12, stroke: 2 }), h('span.num', fmtBytes(out))),
    h('span.ov-kpi__flow', { title: 'Octets reçus par les clients (réponses)' }, icon('arrow-down-left', { size: 12, stroke: 2 }), h('span.num', fmtBytes(incoming))),
  );
}

/**
 * Rangée d'indicateurs.
 * @returns {HTMLElement & {update: (state: {status: Object|null, mode: 'loading'|'ready'|'offline'}) => void,
 *   tick: (stats: Object) => void, destroy: () => void}}
 *   `update` applique les cumuls ; `tick` ajoute un point (une seconde) aux mini-courbes.
 */
export function createKpis() {
  const sparks = {
    calls: Sparkline({ ...SPARK, type: 'area', min: 0, tone: 'accent', format: (value) => fmtRate(value, 'appels/s'), ariaLabel: 'Appels par seconde, 60 dernières secondes' }),
    latency: Sparkline({ ...SPARK, min: 0, color: 'var(--fg-1)', format: fmtMs, ariaLabel: 'Durée moyenne des appels distants, seconde par seconde' }),
    bytes: Sparkline({ ...SPARK, type: 'area', min: 0, color: 'var(--fg-1)', format: (value) => `${fmtBytes(value)}/s`, ariaLabel: 'Octets échangés par seconde, 60 dernières secondes' }),
    errors: MiniBars({ ...SPARK, color: 'var(--fg-3)', highlight: 'max', format: (value) => fmtRate(value, 'erreurs/s'), ariaLabel: 'Erreurs par seconde, 60 dernières secondes' }),
  };

  const stats = {
    calls: Stat({ label: 'Appels', icon: 'activity', value: null, format: (value) => fmtNumber(Math.round(value)), trailing: sparks.calls }),
    latency: Stat({ label: 'Latence moyenne', icon: 'timer', value: null, format: fmtMs, trailing: sparks.latency }),
    bytes: Stat({ label: 'Octets échangés', icon: 'arrow-left-right', value: null, format: fmtBytes, trailing: sparks.bytes }),
    errors: Stat({ label: 'Taux d’erreur', icon: 'triangle-alert', value: null, format: (value) => fmtPercent(value, { decimals: 1 }), trailing: sparks.errors }),
  };

  const cards = Object.entries(stats).map(([key, stat], index) => {
    const skeleton = h('div.ov-kpi__skeleton', { 'aria-hidden': 'true' }, Skeleton({ width: '38%' }), Skeleton({ variant: 'block', width: '56%', height: 30 }), Skeleton({ width: '72%' }));
    const card = Card({ class: 'ov-kpi' }, stat, skeleton);
    card.dataset.kpi = key;
    card.style.setProperty('--i', String(index));
    return card;
  });

  const el = Grid({ class: 'ov-kpis' }, cards.map((card) => Col({ span: 3, md: 6 }, card)));
  el.setAttribute('aria-live', 'off');
  let rate = 0;
  let errorRates = [];
  let latencies = [];

  function paint(totals, mode) {
    const idle = totals.calls === 0;
    stats.calls.update({
      value: mode === 'ready' || totals.calls ? totals.calls : null,
      delta: rate > 0 ? { label: fmtRate(rate, '/s'), tone: 'success', direction: 'up' } : null,
      hint: idle ? 'aucun appel depuis le démarrage' : 'depuis le démarrage',
    });

    const ratio = totals.remoteAvgMs !== null && totals.localAvgMs ? totals.remoteAvgMs / totals.localAvgMs : null;
    stats.latency.update({
      value: totals.remoteAvgMs,
      hint:
        totals.remoteAvgMs === null
          ? 'aucun appel distant mesuré'
          : ratio !== null && ratio >= 1.5
            ? `${fmtNumber(ratio, { maxDecimals: ratio >= 10 ? 0 : 1 })} × l’appel local (${fmtUs(totals.localAvgMs * 1000)})`
            : `sur ${plural(totals.remoteCalls, 'appel distant', 'appels distants')}`,
    });

    const bytes = totals.bytesOut + totals.bytesIn;
    stats.bytes.update({ value: mode === 'ready' || bytes ? bytes : null, hint: bytes ? bytesHint(totals.bytesOut, totals.bytesIn) : 'rien n’a encore circulé' });

    stats.errors.update({
      value: totals.errorRate,
      hint: idle ? 'aucun appel à juger' : totals.errors ? `${plural(totals.errors, 'erreur', 'erreurs')} sur ${plural(totals.calls, 'appel', 'appels')}` : `aucune erreur sur ${plural(totals.calls, 'appel', 'appels')}`,
    });
    cards[3].toggleAttribute('data-alert', totals.errors > 0);
  }

  el.update = ({ status, mode }) => {
    const loading = mode === 'loading';
    for (const card of cards) card.toggleAttribute('data-loading', loading);
    if (loading) return;
    if (status) {
      paint(sumTotals(status), mode);
      return;
    }
    // Laboratoire jamais joint : aucun cumul à afficher, pas même un zéro.
    for (const stat of Object.values(stats)) stat.update({ value: null, delta: null, hint: 'en attente du laboratoire' });
    cards[3].removeAttribute('data-alert');
  };

  el.tick = (message) => {
    const rates = sumRates(message);
    rate = rates.calls;
    sparks.calls.push(rates.calls, WINDOW);
    sparks.bytes.push(rates.bytes, WINDOW);
    errorRates = [...errorRates, rates.errors].slice(-WINDOW);
    sparks.errors.update({ values: errorRates, color: errorRates.some((value) => value > 0) ? 'var(--danger)' : 'var(--fg-3)' });
    if (rates.avgMs !== null) {
      // Une seconde sans appel distant n'a pas de latence : la courbe ne relie que les secondes mesurées.
      latencies = [...latencies, rates.avgMs].slice(-WINDOW);
      if (latencies.length > 1) sparks.latency.update(latencies);
    }
  };

  el.destroy = () => {
    for (const spark of Object.values(sparks)) spark.destroy();
  };
  return el;
}
