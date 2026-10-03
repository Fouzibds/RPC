/**
 * Banc d'essai — « Latence » : débit et stabilité par protocole, temps moyen par appel,
 * distribution, étendue (min · p50 · p95 · p99 · max) et tableau statistique complet.
 */

import { h, clear } from '../../core/dom.js';
import { fmtNumber } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';
import { BarChart, Histogram, RangeChart } from '../../components/charts.js';
import { Callout, Card, Col, Grid, ProtocolChip, Segmented, Table } from '../../components/ui.js';
import { chartState, extreme, fmtDur, fmtFactor, measuredResults, protocolLabel, suiteStatus } from './model.js';
import { Insight, SuiteSection, num } from './section.js';
import { ProtocolTile } from './tiles.js';

const count = (value) => (Number.isFinite(value) ? fmtNumber(value) : null);
const duration = (value) => (Number.isFinite(value) ? fmtDur(value) : null);

/**
 * Section « Latence ».
 * @param {{track: (chart: HTMLElement) => HTMLElement, onRequest: (suite: string) => void, onCleanup: (fn: () => void) => void}} props
 * @returns {{el: HTMLElement, update: (view: Object|null, run: import('./model.js').RunState) => void}}
 */
export function createLatencySection({ track, onRequest, onCleanup }) {
  let scale = 'log';
  let layout = 'overlay';
  let data = null;
  let status = null;
  let expected = [];

  /* --- Tuiles ----------------------------------------------------------------------------- */
  const tiles = Array.from({ length: 4 }, () => ProtocolTile(track));
  for (const tile of tiles) onCleanup(tile.dispose);
  const issues = h('div.bench-issues', { hidden: true });

  function drawTiles() {
    const results = data?.results ?? [];
    const slots = Math.max(results.length, status === 'ready' ? 0 : expected.length);
    const span = slots ? Math.max(3, Math.floor(12 / slots)) : 3;
    tiles.forEach((tile, index) => {
      tile.el.className = `col col-${span} col-md-6 col-sm-6`;
      tile.el.hidden = index >= slots;
      if (index < slots) tile.set(results[index] ?? null, Number.isFinite(results[index]?.mean_ms));
    });
    const failed = results.filter((result) => result.aborted || result.error);
    issues.hidden = !failed.length;
    clear(
      issues,
      failed.length
        ? Callout(
            { tone: 'warning', title: 'Des appels ont échoué pendant la mesure' },
            failed.map((result) =>
              h(
                'p',
                `${protocolLabel(result)} : ${fmtNumber(result.errors)} erreur${result.errors > 1 ? 's' : ''}`,
                result.aborted ? ', mesure interrompue après plusieurs échecs d’affilée' : '',
                result.error ? ` (${result.error.code} — ${result.error.message})` : '',
                '. Les statistiques ne portent que sur les appels réussis.',
              ),
            ),
          )
        : null,
    );
  }

  /* --- Temps moyen ------------------------------------------------------------------------ */
  const meanChart = track(BarChart({ ariaLabel: 'Temps moyen par appel et par protocole', seriesLabel: 'Temps moyen', orientation: 'horizontal', format: fmtDur, rowHeight: 58, barSize: 16 }));
  const meanInsight = Insight();
  const meanCard = Card(
    {
      title: 'Temps moyen par appel',
      subtitle: '',
      actions: Segmented({
        size: 'sm',
        ariaLabel: 'Échelle des durées',
        value: scale,
        options: [
          { value: 'linear', label: 'Linéaire', title: 'Les longueurs sont proportionnelles aux durées' },
          { value: 'log', label: 'Log', title: 'Chaque graduation multiplie par dix : l’appel local reste visible' },
        ],
        onChange: (value) => {
          scale = value;
          drawMean();
        },
      }),
    },
    meanChart,
    meanInsight,
  );

  function drawMean() {
    const results = measuredResults(data);
    meanCard.setSubtitle(scale === 'log' ? 'Échelle logarithmique : chaque graduation multiplie la durée par dix' : 'Échelle linéaire : les longueurs sont proportionnelles aux durées');
    meanChart.update({
      ...chartState(status),
      log: scale === 'log',
      data: results.map((result) => ({
        id: result.protocol,
        label: protocolLabel(result),
        short: protocol(result.protocol).short,
        protocol: result.protocol,
        value: result.mean_ms,
        hint: `médiane ${fmtDur(result.median_ms)} · ${fmtNumber(result.count)} appels`,
      })),
    });
    const local = results.find((result) => result.protocol === 'local');
    const remote = results.filter((result) => result.protocol !== 'local');
    const fastest = extreme(remote, (result) => result.mean_ms, 'min');
    const slowest = extreme(remote, (result) => result.mean_ms, 'max');
    if (!fastest) {
      meanInsight.set();
    } else if (local) {
      meanInsight.set(
        'L’appel local dure ',
        num(fmtDur(local.mean_ms)),
        ` ; le plus rapide des appels distants, ${protocolLabel(fastest)}, en demande `,
        num(fmtDur(fastest.mean_ms)),
        ' : ',
        num(fmtFactor(fastest.mean_ms / local.mean_ms)),
        '. Le code appelant, lui, est le même.',
      );
    } else if (slowest && slowest !== fastest) {
      meanInsight.set(
        `${protocolLabel(fastest)} est le plus rapide (`,
        num(fmtDur(fastest.mean_ms)),
        `), ${protocolLabel(slowest)} le plus lent (`,
        num(fmtDur(slowest.mean_ms)),
        '). Sans l’appel local dans la mesure, le surcoût du réseau ne peut pas être calculé.',
      );
    } else {
      meanInsight.set();
    }
  }

  /* --- Distribution ----------------------------------------------------------------------- */
  const histogram = track(Histogram({ ariaLabel: 'Distribution des latences par protocole', layout, format: fmtDur, height: 250, rowHeight: 76 }));
  const histogramInsight = Insight('info');
  const histogramCard = Card(
    {
      title: 'Distribution des latences',
      subtitle: 'Mêmes bornes pour toutes les séries ; hauteurs normalisées par série',
      actions: Segmented({
        size: 'sm',
        ariaLabel: 'Disposition des distributions',
        value: layout,
        options: [
          { value: 'overlay', label: 'Superposées' },
          { value: 'multiples', label: 'Séparées' },
        ],
        onChange: (value) => {
          layout = value;
          drawHistogram();
        },
      }),
    },
    histogram,
    histogramInsight,
  );

  function drawHistogram() {
    const results = measuredResults(data).filter((result) => result.histogram?.counts?.length);
    histogram.update({
      ...chartState(status),
      layout,
      // Superposées, les étiquettes « p50 » se chevauchent : la médiane passe dans la légende.
      markers: layout === 'overlay' ? [] : undefined,
      series: results.map((result) => ({
        id: result.protocol,
        label: layout === 'overlay' ? `${protocol(result.protocol).short} · p50 ${fmtDur(result.median_ms)}` : protocol(result.protocol).short,
        protocol: result.protocol,
        edges: result.histogram.edges_ms,
        counts: result.histogram.counts,
        markers: { p50: result.median_ms, p95: result.p95_ms, p99: result.p99_ms },
      })),
    });
    const clipped = results.reduce((sum, result) => sum + (result.histogram.clipped ?? 0), 0);
    const edges = results[0]?.histogram.edges_ms ?? [];
    if (!edges.length) {
      histogramInsight.set();
      return;
    }
    histogramInsight.set(
      `${fmtNumber(edges.length - 1)} intervalles de `,
      num(fmtDur(edges[0])),
      ' à ',
      num(fmtDur(edges[edges.length - 1])),
      clipped
        ? [' ; ', num(fmtNumber(clipped)), ` échantillon${clipped > 1 ? 's' : ''} plus lent${clipped > 1 ? 's' : ''} (au-delà du 99,5ᵉ centile) ${clipped > 1 ? 'sont' : 'est'} hors cadre pour ne pas écraser la distribution.`]
        : '. Aucun échantillon hors cadre.',
    );
  }

  /* --- Étendue ---------------------------------------------------------------------------- */
  const range = track(RangeChart({ ariaLabel: 'Étendue des latences par protocole', format: fmtDur, scale: 'log', rowHeight: 68 }));
  const rangeInsight = Insight();
  const rangeCard = Card(
    { title: 'Du plus rapide au plus lent', subtitle: 'Trait : du minimum au maximum · bande pleine : médiane → p95 · bande claire : jusqu’au p99 · axe logarithmique' },
    range,
    rangeInsight,
  );

  function drawRange() {
    const results = measuredResults(data);
    range.update({
      ...chartState(status),
      rows: results.map((result) => ({
        id: result.protocol,
        label: protocol(result.protocol).short,
        protocol: result.protocol,
        min: result.min_ms,
        p50: result.median_ms,
        p95: result.p95_ms,
        p99: result.p99_ms,
        max: result.max_ms,
        mean: result.mean_ms,
      })),
    });
    // Comme le fait marquant du rapport : la traîne se cherche d'abord parmi les appels distants.
    const usable = results.filter((result) => result.median_ms > 0);
    const remote = usable.filter((result) => result.protocol !== 'local');
    const worst = extreme(remote.length ? remote : usable, (result) => result.p99_ms / result.median_ms, 'max');
    if (!worst) {
      rangeInsight.set();
      return;
    }
    rangeInsight.set(
      `La moyenne cache la traîne. ${protocolLabel(worst)} : médiane `,
      num(fmtDur(worst.median_ms)),
      ', mais un appel sur cent dépasse ',
      num(fmtDur(worst.p99_ms)),
      ' (',
      num(fmtFactor(worst.p99_ms / worst.median_ms)),
      ') et le plus lent a duré ',
      num(fmtDur(worst.max_ms)),
      '.',
    );
  }

  /* --- Tableau ---------------------------------------------------------------------------- */
  // Une ligne par statistique, une colonne par protocole : chaque ligne se lit comme une comparaison.
  const STATISTICS = [
    ['Appels', (result) => count(result.count)],
    ['Erreurs', (result) => (result.errors ? h('span.bench-delta', { dataset: { tone: 'danger' } }, fmtNumber(result.errors)) : count(result.errors))],
    ['Moyenne', (result) => duration(result.mean_ms)],
    ['Médiane', (result) => duration(result.median_ms)],
    ['p90', (result) => duration(result.p90_ms)],
    ['p95', (result) => duration(result.p95_ms)],
    ['p99', (result) => duration(result.p99_ms)],
    ['Minimum', (result) => duration(result.min_ms)],
    ['Maximum', (result) => duration(result.max_ms)],
    ['Écart-type', (result) => duration(result.stdev_ms)],
    ['Débit (appels/s)', (result) => (Number.isFinite(result.rps) ? fmtNumber(result.rps, { maxDecimals: 0 }) : null)],
    ['× appel local', (result) => (Number.isFinite(result.overhead_vs_local_x) ? fmtFactor(result.overhead_vs_local_x) : null)],
  ];
  const tableSlot = h('div.bench-stats');
  const tableCard = Card({ title: 'Statistiques complètes', subtitle: 'Appels réussis uniquement · chaque appel chronométré séparément', padding: 'none' }, tableSlot);
  let tableSignature = null;

  function drawTable() {
    const results = data?.results ?? [];
    const loadingNow = status !== 'ready' && status !== 'skipped' && !results.length;
    const ids = loadingNow ? expected : results.map((result) => result.protocol);
    const signature = `${loadingNow}|${ids.join(',')}`;
    let table = tableSlot.firstElementChild;
    if (signature !== tableSignature) {
      tableSignature = signature;
      table = Table({
        density: 'compact',
        stickyHeader: false,
        caption: 'Statistiques de latence : une ligne par statistique, une colonne par protocole',
        empty: { icon: 'timer', title: 'Aucune latence mesurée', text: 'La suite « Latence » n’a pas encore produit de résultat.' },
        columns: [
          { key: 'label', label: 'Statistique' },
          ...ids.map((id) => ({ key: id, label: ProtocolChip(id, { variant: 'plain', short: true, size: 'sm' }), align: 'right', mono: true })),
        ],
        rowKey: (row) => row.label,
      });
      clear(tableSlot, table);
    }
    if (loadingNow) table.setLoading(true, STATISTICS.length);
    else table.setRows(results.length ? STATISTICS.map(([label, cell]) => ({ label, ...Object.fromEntries(results.map((result) => [result.protocol, cell(result)])) })) : []);
  }

  const section = SuiteSection(
    {
      suite: 'latency',
      description: 'Combien de temps dure un appel, et à quel point est-ce régulier ? La même boucle, protocole après protocole, sans rien mesurer d’autre que l’appel.',
      onRequest,
    },
    issues,
    Grid(
      tiles.map((tile) => tile.el),
      Col({ span: 5, md: 12 }, meanCard),
      Col({ span: 7, md: 12 }, histogramCard),
      Col({ span: 6, md: 12 }, rangeCard),
      Col({ span: 6, md: 12 }, tableCard),
    ),
  );

  function context() {
    if (!data) return [];
    const clients = data.concurrency ?? 1;
    return [
      h('span.mono', data.method),
      `${fmtNumber(data.iterations)} appels par protocole`,
      `${fmtNumber(data.warmup)} appels d’échauffement`,
      `${fmtNumber(clients)} client${clients > 1 ? 's en parallèle' : ''}`,
      data.via_proxy ? 'à travers le proxy de chaos' : 'accès direct, en boucle locale',
      'bus de traces coupé',
    ];
  }

  return {
    el: section.el,
    update(view, run) {
      const nextStatus = suiteStatus(view, run, 'latency');
      const nextData = view?.latency ?? null;
      if (nextStatus === status && nextData === data) return;
      status = nextStatus;
      data = nextData;
      expected = view?.config?.protocols ?? [];
      section.setStatus(status);
      section.setContext(context());
      drawTiles();
      drawMean();
      drawHistogram();
      drawRange();
      drawTable();
    },
  };
}
