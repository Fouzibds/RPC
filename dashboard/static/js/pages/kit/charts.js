/**
 * Guide de style — graphiques : chaque composant de `components/charts.js` avec des données
 * réalistes du laboratoire.
 */

import { h } from '../../core/dom.js';
import { fmtBytes, fmtMs, fmtNumber, fmtPercent, fmtRate, fmtTime, fmtUs } from '../../core/format.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import {
  BarChart,
  Donut,
  GroupedBarChart,
  Histogram,
  LANE_COLORS,
  LineChart,
  MiniBars,
  RangeChart,
  Sparkline,
  StackedBar,
  Timeline,
  Waterfall,
} from '../../components/charts.js';
import { Button, Card, Col, Divider, Grid, ProtocolChip, Section, Stat, Table } from '../../components/ui.js';
import { closeMediansCard, responsiveSparkCard, segmentedDonutCard, thinToneCard } from './chartsmore.js';
import { DemoCard, Specimen, Usage } from './demo.js';
import { CALL_STAGES, CLIENT_CODE_LINES, LATENCY, OUTAGE_ITEMS, PAYLOADS, SCALING, THROUGHPUT, throughputWalk } from './vizdata.js';

const REMOTE_LATENCY = LATENCY.filter((row) => row.protocol !== 'local');

function bars(track) {
  const sizes = track(
    BarChart({
      ariaLabel: 'Taille de la réponse get_product_details par protocole',
      seriesLabel: 'Réponse',
      format: fmtBytes,
      height: 336,
      data: REMOTE_PROTOCOL_IDS.map((id) => ({ id, label: protocol(id).label, short: protocol(id).short, protocol: id, value: PAYLOADS.response[id][1], hint: protocol(id).transport })),
    }),
  );
  const latency = track(
    BarChart({
      ariaLabel: 'Latence moyenne par protocole',
      seriesLabel: 'Latence moyenne',
      orientation: 'horizontal',
      format: fmtMs,
      log: true,
      data: LATENCY.map((row) => ({ id: row.protocol, label: protocol(row.protocol).label, protocol: row.protocol, value: row.mean_ms })),
    }),
  );
  const effort = track(
    BarChart({
      ariaLabel: 'Lignes de code côté client pour le même appel',
      seriesLabel: 'Lignes de code',
      orientation: 'horizontal',
      sorted: 'asc',
      axis: true,
      format: (value) => fmtNumber(value),
      data: CLIENT_CODE_LINES.map((row) => ({ id: row.protocol, label: row.label, protocol: row.protocol, value: row.lines, hint: row.hint })),
    }),
  );
  const grouped = track(
    GroupedBarChart({
      ariaLabel: 'Taille des requêtes par procédure et par protocole',
      format: fmtBytes,
      monoLabels: true,
      categories: PAYLOADS.procedures.map((name) => ({ id: name, label: name })),
      series: REMOTE_PROTOCOL_IDS.map((id) => ({ id, label: protocol(id).short, protocol: id, values: PAYLOADS.request[id] })),
    }),
  );
  const pairs = track(
    GroupedBarChart({
      ariaLabel: 'Requête et réponse par protocole',
      format: fmtBytes,
      colorBy: 'category',
      categories: REMOTE_PROTOCOL_IDS.map((id) => ({ id, label: protocol(id).label, short: protocol(id).short, protocol: id })),
      series: [
        { id: 'request', label: 'Requête', values: REMOTE_PROTOCOL_IDS.map((id) => PAYLOADS.request[id][1]) },
        { id: 'response', label: 'Réponse', values: REMOTE_PROTOCOL_IDS.map((id) => PAYLOADS.response[id][1]) },
      ],
    }),
  );
  const loading = track(BarChart({ loading: true, height: 150 }));
  const empty = track(BarChart({ height: 150, empty: { icon: 'gauge', title: 'Aucune mesure', text: 'Lancez le banc d’essai pour remplir ce graphique.' } }));

  return Section(
    {
      title: 'Barres',
      description: 'BarChart (une série, une couleur par barre) et GroupedBarChart (catégories × séries). Sommets arrondis, valeurs écrites au bout des barres, info-bulle au survol, flèches du clavier.',
    },
    Grid(
      DemoCard({ title: 'BarChart', subtitle: 'Réponse get_product_details · octets sur le fil', span: 5 }, sizes, Usage('BarChart({ data: [{ label, value, protocol }], format: fmtBytes })')),
      DemoCard(
        { title: 'BarChart horizontal', subtitle: 'Latence moyenne (échelle logarithmique), puis lignes de code côté client pour le même appel', span: 7 },
        latency,
        Divider({ label: 'Trié, avec axe' }),
        effort,
        Usage("BarChart({ orientation: 'horizontal', log: true, format: fmtMs, data })  ·  { sorted: 'asc', axis: true }"),
      ),
      DemoCard(
        { title: 'GroupedBarChart', subtitle: 'Taille des requêtes par procédure — la légende masque une série', span: 7 },
        grouped,
        Usage('GroupedBarChart({ categories, series: [{ id, label, protocol, values }], format: fmtBytes })'),
      ),
      DemoCard(
        { title: 'GroupedBarChart, couleur par catégorie', subtitle: 'Requête et réponse : même teinte, deux intensités', span: 5 },
        pairs,
        Usage("GroupedBarChart({ colorBy: 'category', categories: [{ id, label, protocol }], series })"),
      ),
      DemoCard({ title: 'Chargement', subtitle: 'loading: true — squelette, puis voile sur le tracé précédent', span: 6 }, loading),
      DemoCard({ title: 'État vide', subtitle: 'Aucune donnée : icône, titre et texte personnalisables', span: 6 }, empty),
    ),
  );
}

function lines(track, cleanups) {
  const scaling = track(
    LineChart({
      ariaLabel: 'Taille de la réponse list_products selon le nombre de produits',
      xScale: 'log',
      yScale: 'log',
      xFormat: (value) => fmtNumber(value),
      yFormat: fmtBytes,
      yTickBase: 1024,
      xLabel: 'Produits renvoyés',
      height: 280,
      series: REMOTE_PROTOCOL_IDS.map((id) => ({ id, label: protocol(id).short, protocol: id, points: SCALING.sizes.map((size, index) => [size, SCALING[id][index]]) })),
    }),
  );

  const walk = throughputWalk(7);
  const start = Date.now() - 39_000;
  const history = Object.fromEntries(REMOTE_PROTOCOL_IDS.map((id) => [id, [THROUGHPUT[id]]]));
  for (let tick = 1; tick < 40; tick += 1) {
    for (const id of REMOTE_PROTOCOL_IDS) history[id].push(walk(THROUGHPUT[id], history[id][tick - 1]));
  }
  const live = track(
    LineChart({
      ariaLabel: 'Débit en direct par protocole',
      area: true,
      curve: 'monotone',
      points: false,
      xFormat: (value) => fmtTime(value),
      yFormat: (value) => fmtRate(value),
      yTickFormat: (value) => fmtNumber(value, { compact: true }),
      yLabel: 'req/s',
      height: 280,
      maxPoints: 40,
      series: REMOTE_PROTOCOL_IDS.map((id) => ({ id, label: protocol(id).short, protocol: id, points: history[id].map((value, index) => [start + index * 1000, value]) })),
    }),
  );
  const last = Object.fromEntries(REMOTE_PROTOCOL_IDS.map((id) => [id, history[id][39]]));
  let clock = start + 39_000;
  let timer = 0;
  const step = () => {
    clock += 1000;
    for (const id of REMOTE_PROTOCOL_IDS) last[id] = walk(THROUGHPUT[id], last[id]);
    live.push(clock, last);
  };
  const toggle = Button({
    label: 'Démarrer le direct',
    icon: 'play',
    size: 'sm',
    id: 'kit-live-toggle',
    onClick: () => {
      if (timer) {
        window.clearInterval(timer);
        timer = 0;
      } else {
        step();
        timer = window.setInterval(step, 700);
      }
      toggle.setLabel(timer ? 'Mettre en pause' : 'Démarrer le direct');
      toggle.setIcon(timer ? 'pause' : 'play');
    },
  });
  cleanups.push(() => window.clearInterval(timer));

  return Section(
    {
      title: 'Courbes',
      description: 'LineChart : plusieurs séries, aire facultative, réticule accroché à l’abscisse la plus proche, info-bulle listant chaque série, échelles logarithmiques, ajout de points en direct.',
    },
    Grid(
      DemoCard(
        { title: 'LineChart, échelles logarithmiques', subtitle: 'Réponse list_products — JSON grossit comme Protobuf, mais 2,6 fois plus gros', span: 6 },
        scaling,
        Usage("LineChart({ series: [{ id, label, protocol, points: [[x, y]] }], xScale: 'log', yScale: 'log', yFormat: fmtBytes, yTickBase: 1024 })"),
      ),
      DemoCard(
        { title: 'LineChart en direct', subtitle: 'Débit par protocole — fenêtre glissante de 40 points', span: 6, actions: toggle },
        live,
        Usage("chart.push(Date.now(), { custom: 6890, grpc: 3092, rest: 1894 })   // area: true, curve: 'monotone', maxPoints: 40"),
      ),
    ),
  );
}

function distributions(track) {
  const series = REMOTE_LATENCY.map((row) => ({
    id: row.protocol,
    label: protocol(row.protocol).short,
    protocol: row.protocol,
    edges: row.histogram.edges_ms,
    counts: row.histogram.counts,
    markers: { p50: row.median_ms, p95: row.p95_ms, p99: row.p99_ms },
  }));
  const multiples = track(Histogram({ ariaLabel: 'Distribution des latences par protocole', format: fmtMs, xScale: 'log', series }));
  const overlay = track(Histogram({ ariaLabel: 'Distributions superposées', layout: 'overlay', format: fmtMs, xScale: 'log', height: 230, series }));
  const range = track(
    RangeChart({
      ariaLabel: 'Étendue des latences par protocole',
      format: fmtMs,
      scale: 'log',
      rows: LATENCY.map((row) => ({
        id: row.protocol,
        label: protocol(row.protocol).label,
        protocol: row.protocol,
        min: row.min_ms,
        p50: row.median_ms,
        p95: row.p95_ms,
        p99: row.p99_ms,
        max: row.max_ms,
        mean: row.mean_ms,
      })),
    }),
  );

  return Section(
    {
      title: 'Distributions',
      description: 'Histogram : effectifs par intervalle à partir de { edges, counts }, repères p50 / p95 / p99. RangeChart : min · médiane · p95 · p99 · max sur un axe partagé.',
    },
    Grid(
      DemoCard(
        { title: 'Histogram', subtitle: 'get_product_details · 1 000 appels par protocole · axe logarithmique', span: 7 },
        multiples,
        Usage("Histogram({ series: [{ id, label, protocol, edges, counts, markers: { p50, p95, p99 } }], format: fmtMs, xScale: 'log' })"),
      ),
      DemoCard(
        { title: 'Histogram superposé', subtitle: 'layout: \'overlay\' — formes comparées, médianes repérées', span: 5 },
        overlay,
        Usage("Histogram({ layout: 'overlay', series, format: fmtMs })"),
      ),
      closeMediansCard(track),
      DemoCard(
        { title: 'RangeChart', subtitle: 'Où tombent les appels typiques, et jusqu’où va la traîne', span: 12 },
        range,
        Usage("RangeChart({ rows: [{ id, label, protocol, min, p50, p95, p99, max }], format: fmtMs, scale: 'log' })"),
      ),
    ),
  );
}

function minis(track) {
  const trend = [4, 6, 5, 9, 8, 12, 10, 14, 13, 18, 16, 21];
  const calls = Stat({
    label: 'Appels',
    icon: 'activity',
    value: 12840,
    format: (value) => fmtNumber(Math.round(value)),
    delta: { label: '+128 /s', tone: 'success', direction: 'up' },
    hint: 'depuis le démarrage',
    trailing: track(Sparkline({ values: trend, type: 'area', format: (value) => `${fmtNumber(value)} k` })),
  });
  const latency = Stat({
    label: 'Latence médiane',
    protocol: 'grpc',
    value: 0.296,
    format: fmtMs,
    hint: '30 dernières secondes',
    trailing: track(Sparkline({ values: [0.31, 0.29, 0.3, 0.33, 0.29, 0.28, 0.3, 0.42, 0.31, 0.29, 0.3, 0.296], protocol: 'grpc', format: fmtMs })),
  });
  const errors = Stat({
    label: 'Erreurs',
    icon: 'triangle-alert',
    value: 14,
    format: (value) => fmtNumber(Math.round(value)),
    hint: 'par tranche de 5 s',
    trailing: track(MiniBars({ values: [0, 0, 1, 0, 0, 2, 5, 3, 1, 0, 0, 2], tone: 'danger', width: 84, height: 28, highlight: 'max', format: (value) => fmtNumber(value) })),
  });

  const history = { local: [6, 6, 6, 7, 6, 6, 6, 6], custom: [140, 132, 151, 138, 129, 144, 136, 132], grpc: [320, 301, 296, 342, 310, 288, 305, 296], rest: [530, 498, 512, 560, 471, 505, 488, 490] };
  const table = Table({
    density: 'compact',
    columns: [
      { key: 'protocol', label: 'Protocole', format: (value) => ProtocolChip(value, { variant: 'plain' }) },
      { key: 'median_ms', label: 'Médiane', align: 'right', mono: true, format: (value) => fmtMs(value) },
      { key: 'protocol', label: 'Tendance', format: (value) => track(Sparkline({ values: history[value], protocol: value, width: 72, height: 20 })) },
      { key: 'protocol', label: 'Distribution', format: (value, row) => track(MiniBars({ values: row.histogram.counts.filter((_, index) => index % 2 === 0), protocol: value, highlight: 'max', width: 72 })) },
    ],
    rows: LATENCY,
    rowKey: (row) => row.protocol,
  });

  const savings = 1 - PAYLOADS.response.grpc[1] / PAYLOADS.response.custom[1];
  const success = track(Donut({ value: 0.979, label: 'succès', tone: 'success', format: (value) => fmtPercent(value, { decimals: 1 }) }));
  const bytes = track(Donut({ value: savings, label: 'd’octets en moins', protocol: 'grpc' }));
  const budget = track(Donut({ value: 0.34, label: 'tentatives', variant: 'gauge', tone: 'warning' }));
  const small = track(Donut({ value: 0.72, size: 64, thickness: 6 }));

  const breakdown = track(
    StackedBar({
      ariaLabel: 'Décomposition du temps d’un appel',
      format: fmtUs,
      segments: [
        { id: 'marshal', label: 'Sérialisation', value: 30, color: LANE_COLORS.client, hint: 'client.marshal + client.send' },
        { id: 'up', label: 'Réseau · aller', value: 62, color: LANE_COLORS.network },
        { id: 'server', label: 'Serveur', value: 49.4, color: LANE_COLORS.server, hint: 'Désérialisation, dispatch, exécution, sérialisation, envoi' },
        { id: 'down', label: 'Réseau · retour', value: 58, color: 'color-mix(in srgb, var(--lane-network) 62%, transparent)' },
        { id: 'unmarshal', label: 'Désérialisation', value: 17, color: 'color-mix(in srgb, var(--lane-client) 58%, transparent)', hint: 'client.unmarshal' },
      ],
    }),
  );
  const wire = track(
    StackedBar({
      ariaLabel: 'Composition d’une requête REST',
      format: (value) => fmtBytes(value),
      thickness: 10,
      segments: [
        { id: 'line', label: 'Ligne de requête', value: 37 },
        { id: 'headers', label: 'En-têtes HTTP', value: 98 },
        { id: 'body', label: 'Corps JSON', value: 37 },
      ],
    }),
  );

  return Section(
    {
      title: 'Petits formats',
      description: 'Sparkline et MiniBars (indicateurs, cellules de tableau), Donut (ratio avec valeur centrée) et StackedBar (une barre découpée en segments chiffrés).',
    },
    Grid(
      Col({ span: 4, md: 4, sm: 12 }, Card(calls)),
      Col({ span: 4, md: 4, sm: 12 }, Card({ protocol: 'grpc' }, latency)),
      Col({ span: 4, md: 4, sm: 12 }, Card({ accent: 'danger' }, errors)),
      DemoCard(
        { title: 'Sparkline et MiniBars', subtitle: 'Dans un tableau : tendance et distribution par ligne', span: 7, padding: 'none' },
        table,
      ),
      DemoCard(
        { title: 'Variantes', subtitle: 'line · area · bars, point final facultatif', span: 5 },
        Specimen('line', track(Sparkline({ values: trend, protocol: 'custom' })), track(Sparkline({ values: trend, dot: false, tone: 'neutral' }))),
        Specimen('area', track(Sparkline({ values: trend, type: 'area', protocol: 'grpc' })), track(Sparkline({ values: [...trend].reverse(), type: 'area', tone: 'danger' }))),
        Specimen('bars', track(Sparkline({ values: trend, type: 'bars', protocol: 'rest' })), track(MiniBars({ values: trend, highlight: 'max' }))),
        Specimen('vide', track(Sparkline({ values: [] }))),
        Usage("Sparkline({ values, type: 'area', protocol: 'grpc' })  ·  MiniBars({ values, highlight: 'max' })"),
      ),
      responsiveSparkCard(track),
      segmentedDonutCard(track),
      DemoCard(
        { title: 'Donut', subtitle: 'Anneau ou jauge ; la piste est une teinte plus claire de la même couleur', span: 6 },
        h('div', { style: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '20px 28px' } }, success, bytes, budget, small),
        Usage("Donut({ value: 0.62, label: 'd’octets en moins', protocol: 'grpc' })  →  .set(0.7)"),
      ),
      DemoCard(
        { title: 'StackedBar', subtitle: 'Où passe le temps d’un appel JSON-RPC — survolez un segment ou sa légende', span: 6 },
        breakdown,
        wire,
        Usage('StackedBar({ segments: [{ id, label, value, color }], format: fmtUs })'),
      ),
    ),
  );
}

function sequences(track, cleanups) {
  const lanes = [
    { id: 'naive', label: 'Client naïf', sublabel: 'aucune protection' },
    { id: 'resilient', label: 'Client résilient', sublabel: 'échéance · 3 essais · disjoncteur' },
  ];
  const timeline = track(Timeline({ ariaLabel: 'Panne serveur : client naïf et client résilient', lanes, items: OUTAGE_ITEMS }));
  let replayTimer = 0;
  const replay = Button({
    label: 'Rejouer en direct',
    icon: 'play',
    size: 'sm',
    id: 'kit-timeline-replay',
    onClick: () => {
      window.clearInterval(replayTimer);
      timeline.clear();
      const ordered = [...OUTAGE_ITEMS].sort((a, b) => a.start - b.start);
      let now = 0;
      replayTimer = window.setInterval(() => {
        now += 100;
        while (ordered.length && ordered[0].start <= now) {
          const item = ordered.shift();
          timeline.push(item.end !== undefined && item.end > now ? { ...item, end: now, kind: 'pending' } : item);
        }
        const running = timeline.state.items.filter((item) => item.kind === 'pending');
        for (const item of running) {
          const source = OUTAGE_ITEMS.find((candidate) => candidate.id === item.id);
          timeline.push(source.end <= now ? source : { ...item, end: now });
        }
        timeline.setNow(now);
        if (now >= 5800) {
          window.clearInterval(replayTimer);
          timeline.setNow(null);
        }
      }, 60);
    },
  });
  cleanups.push(() => window.clearInterval(replayTimer));

  const waterfall = track(Waterfall({ ariaLabel: 'Étapes d’un appel update_stock', stages: CALL_STAGES, onSelect: (stage) => waterfall.select(stage.id), selected: 'server.execute' }));
  let frame = 0;
  const play = Button({
    label: 'Rejouer l’appel',
    icon: 'play',
    size: 'sm',
    id: 'kit-waterfall-play',
    onClick: () => {
      cancelAnimationFrame(frame);
      const startedAt = performance.now();
      const tick = (time) => {
        const position = ((time - startedAt) / 2600) * 221;
        waterfall.setPlayhead(Math.min(221, position));
        if (position < 221) frame = requestAnimationFrame(tick);
      };
      frame = requestAnimationFrame(tick);
    },
  });
  cleanups.push(() => cancelAnimationFrame(frame));

  return Section(
    {
      title: 'Chronologies',
      description: 'Timeline : couloirs de plages et d’évènements dans le temps, ajout en direct. Waterfall : les étapes d’un appel dans l’ordre, avec tête de lecture.',
    },
    Grid(
      DemoCard(
        { title: 'Timeline', subtitle: 'Scénario « panne serveur » — le même réseau, deux clients', span: 12, actions: replay },
        timeline,
        Usage("timeline.push({ id, lane: 'resilient', start: 600, end: 1100, kind: 'timeout', label: '2', detail })   // kinds : ok · retry · timeout · error · breaker · backoff"),
      ),
      thinToneCard(track),
      DemoCard(
        { title: 'Waterfall', subtitle: 'update_stock via JSON-RPC maison · 221 µs de bout en bout', span: 12, actions: play },
        waterfall,
        Usage('Waterfall({ stages: [{ id, label, lane, start, duration, detail }], onSelect })  →  .setPlayhead(µs)  ·  .select(id)'),
      ),
    ),
  );
}

/**
 * Sections « graphiques » du guide de style.
 * @param {Array<() => void>} cleanups Reçoit le `destroy` de chaque graphique créé et l'arrêt des minuteries.
 * @returns {HTMLElement[]}
 */
export function charts(cleanups) {
  const track = (chart) => {
    cleanups.push(() => chart.destroy());
    return chart;
  };
  return [bars(track), lines(track, cleanups), distributions(track), minis(track), sequences(track, cleanups)];
}
