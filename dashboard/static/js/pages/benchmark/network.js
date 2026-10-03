/**
 * Banc d'essai — « Local vs distant » : temps moyen par appel selon la latence ajoutée par le
 * réseau simulé, et projection du coût d'une boucle d'appels séquentiels.
 */

import { h, clear } from '../../core/dom.js';
import { fmtDuration, fmtNumber } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';
import { LineChart } from '../../components/charts.js';
import { Card, Col, Field, Grid, NumberInput, Segmented } from '../../components/ui.js';
import { chartState, extreme, fmtDur, fmtFactor, fmtPct, suiteStatus } from './model.js';
import { Insight, SuiteSection, num } from './section.js';

const DEFAULT_LOOP = 1000;

const latencyLabel = (ms) => `${fmtNumber(ms, { maxDecimals: 1 })} ms`;
/** Durée d'une boucle, de la microseconde à la minute. */
const loopDuration = (ms) => (ms >= 60000 ? fmtDuration(ms / 1000) : fmtDur(ms));

/**
 * Section « Local vs distant ».
 * @param {{track: (chart: HTMLElement) => HTMLElement, onRequest: (suite: string) => void}} props
 * @returns {{el: HTMLElement, update: (view: Object|null, run: import('./model.js').RunState) => void}}
 */
export function createNetworkSection({ track, onRequest }) {
  let scale = 'linear';
  let data = null;
  let status = null;
  let loop = DEFAULT_LOOP;
  let selected = null;
  let picked = false;

  /* --- Courbes ---------------------------------------------------------------------------- */
  const chart = track(
    LineChart({
      ariaLabel: 'Temps moyen par appel selon la latence ajoutée, par protocole',
      xFormat: latencyLabel,
      yFormat: fmtDur,
      yTickFormat: (value) => (value === 0 ? '0' : fmtDur(value)),
      xLabel: 'Latence ajoutée par le réseau simulé (aller-retour)',
      height: 360,
      points: true,
    }),
  );
  const chartInsight = Insight();
  const chartCard = Card(
    {
      title: 'Temps moyen par appel selon la latence du réseau',
      subtitle: '',
      actions: Segmented({
        size: 'sm',
        ariaLabel: 'Échelle des durées',
        value: scale,
        options: [
          { value: 'linear', label: 'Linéaire', title: 'L’appel local reste collé à zéro' },
          { value: 'log', label: 'Log', title: 'Fait apparaître l’appel local et le point sans latence' },
        ],
        onChange: (value) => {
          scale = value;
          drawChart();
        },
      }),
    },
    chart,
    chartInsight,
  );

  const points = () => data?.points ?? [];
  const protocols = () => data?.protocols ?? Object.keys(points()[0]?.results ?? {});

  function drawChart() {
    const list = points();
    const log = scale === 'log';
    chartCard.setSubtitle(
      data ? `${data.method} · ${fmtNumber(data.iterations)} appels par point, à travers le proxy de chaos · ${log ? 'échelle logarithmique' : 'échelle linéaire'}` : 'La même boucle, de plus en plus loin',
    );
    const series = protocols().map((id) => ({
      id,
      label: protocol(id).short,
      protocol: id,
      points: list.filter((point) => point.results[id] > 0).map((point) => [point.latency_ms, point.results[id]]),
    }));
    const floor = list.filter((point) => point.latency_ms > 0).map((point) => [point.latency_ms, point.latency_ms]);
    if (!log && list.some((point) => point.latency_ms === 0)) floor.unshift([0, 0]);
    if (floor.length > 1) series.push({ id: 'floor', label: 'Latence ajoutée seule', color: 'var(--fg-3)', dashed: true, points: floor });
    chart.update({ ...chartState(status), yScale: log ? 'log' : 'linear', xTicks: list.map((point) => point.latency_ms), series });

    const farthest = extreme(
      list.filter((point) => point.latency_ms > 0),
      (point) => point.latency_ms,
      'max',
    );
    const remote = farthest ? protocols().filter((id) => id !== 'local' && farthest.results[id] > 0) : [];
    if (!remote.length) {
      chartInsight.set();
      return;
    }
    const mean = remote.reduce((sum, id) => sum + farthest.results[id], 0) / remote.length;
    const values = remote.map((id) => farthest.results[id]);
    const spread = Math.max(...values) - Math.min(...values);
    chartInsight.set(
      `Avec ${latencyLabel(farthest.latency_ms)} de latence, le réseau représente `,
      num(fmtPct(Math.min(farthest.latency_ms / mean, 1) * 100)),
      ' du temps d’un appel distant',
      remote.length > 1 ? [' ; l’écart entre le plus rapide et le plus lent des protocoles n’est plus que de ', num(fmtDur(spread)), ', soit ', num(fmtPct((spread / mean) * 100)), ' de ce temps'] : '',
      '. Le choix du protocole compte moins que le nombre d’allers-retours.',
    );
  }

  /* --- Projection ------------------------------------------------------------------------- */
  const rows = h('div.bench-projection__rows', { 'aria-live': 'polite' });
  const sentence = Insight('calculator');
  const loopInput = NumberInput({
    value: loop,
    min: 1,
    max: 1000000,
    step: 100,
    width: 148,
    ariaLabel: 'Nombre d’appels séquentiels',
    onChange: (value) => {
      loop = value;
      drawProjection();
    },
  });
  const pointSlot = h('div.bench-projection__points');
  const projectionCard = Card(
    { title: 'Projection : le coût d’une boucle', subtitle: 'Nombre d’appels × temps moyen mesuré au point choisi' },
    h('div.bench-projection__controls', Field({ label: 'Appels séquentiels', control: loopInput }), Field({ label: 'Latence ajoutée', control: pointSlot })),
    rows,
    sentence,
  );

  function drawPointPicker() {
    const list = points();
    // Tant que l'utilisateur n'a rien choisi, la projection suit le point le plus éloigné mesuré.
    if (!picked || !list.some((point) => point.latency_ms === selected)) {
      picked = picked && list.some((point) => point.latency_ms === selected);
      selected = list.length ? Math.max(...list.map((point) => point.latency_ms)) : null;
    }
    clear(
      pointSlot,
      list.length
        ? Segmented({
            size: 'sm',
            ariaLabel: 'Latence ajoutée',
            value: selected,
            options: list.map((point) => ({ value: point.latency_ms, label: latencyLabel(point.latency_ms) })),
            onChange: (value) => {
              selected = value;
              picked = true;
              drawProjection();
            },
          })
        : h('span.bench-projection__none', status === 'skipped' ? 'Aucun point mesuré' : 'En attente du premier point…'),
    );
  }

  function drawProjection() {
    const point = points().find((item) => item.latency_ms === selected);
    if (!point) {
      clear(rows);
      sentence.set();
      return;
    }
    const entries = protocols()
      .map((id) => ({ id, mean: point.results[id], errors: point.errors?.[id] ?? 0 }))
      .filter((entry) => entry.mean > 0);
    const longest = Math.max(...entries.map((entry) => entry.mean), 0);
    clear(
      rows,
      entries.map((entry) =>
        h(
          'div.bench-projection__row',
          { style: { '--c': protocol(entry.id).color, '--ratio': longest ? entry.mean / longest : 0 } },
          h('span.bench-projection__label', h('span.bench-projection__key', { 'aria-hidden': 'true' }), protocol(entry.id).short),
          h('span.bench-projection__bar', { 'aria-hidden': 'true' }, h('span.bench-projection__fill')),
          h('span.bench-projection__value.num', loopDuration(entry.mean * loop)),
          h('span.bench-projection__unit.num', `${fmtDur(entry.mean)} / appel${entry.errors ? ` · ${fmtNumber(entry.errors)} erreur${entry.errors > 1 ? 's' : ''}` : ''}`),
        ),
      ),
    );
    const local = entries.find((entry) => entry.id === 'local');
    const remote = entries.filter((entry) => entry.id !== 'local');
    const fastest = extreme(remote, (entry) => entry.mean, 'min');
    if (!fastest) {
      sentence.set();
      return;
    }
    sentence.set(
      num(fmtNumber(loop)),
      ` appel${loop > 1 ? 's' : ''} séquentiel${loop > 1 ? 's' : ''} avec ${latencyLabel(point.latency_ms)} de latence : `,
      num(loopDuration(fastest.mean * loop)),
      ` au mieux à distance (${protocol(fastest.id).label})`,
      local ? [', contre ', num(loopDuration(local.mean * loop)), ' en local — ', num(fmtFactor(fastest.mean / local.mean)), '.'] : '.',
      ' La boucle « innocente » devient le premier poste de dépense.',
    );
  }

  const section = SuiteSection(
    {
      suite: 'network',
      description: 'Que devient la même boucle d’appels quand le serveur s’éloigne ? L’appel local n’emprunte aucun réseau ; les trois autres traversent un proxy qui ajoute la latence demandée.',
      onRequest,
    },
    Grid(Col({ span: 8, md: 12 }, chartCard), Col({ span: 4, md: 12 }, projectionCard)),
  );

  return {
    el: section.el,
    update(view, run) {
      const nextStatus = suiteStatus(view, run, 'network');
      const nextData = view?.network ?? null;
      if (nextStatus === status && nextData === data) return;
      status = nextStatus;
      data = nextData;
      section.setStatus(status);
      drawChart();
      drawPointPicker();
      drawProjection();
    },
  };
}
