/**
 * Guide de style — graphiques, compléments : mini-courbe adaptative, anneau multi-segments,
 * histogramme superposé aux médianes proches, attentes colorées d'une chronologie.
 */

import { h } from '../../core/dom.js';
import { fmtBytes, fmtMs, fmtRate } from '../../core/format.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { Donut, Histogram, Sparkline, Timeline } from '../../components/charts.js';
import { Button } from '../../components/ui.js';
import { DemoCard, Usage } from './demo.js';
import { LATENCY, THROUGHPUT, throughputWalk } from './vizdata.js';

/**
 * Mini-courbes qui remplissent la largeur de leur tuile (`responsive: true`).
 * @param {(chart: HTMLElement) => HTMLElement} track Enregistre le `destroy` du graphique.
 * @returns {HTMLElement}
 */
export function responsiveSparkCard(track) {
  const walk = throughputWalk(19);
  const tiles = REMOTE_PROTOCOL_IDS.map((id) => {
    const values = [THROUGHPUT[id]];
    for (let tick = 1; tick < 48; tick += 1) values.push(walk(THROUGHPUT[id], values[tick - 1]));
    return h(
      'div.kit-tile',
      h('span.kit-tile__label', protocol(id).short),
      h('span.kit-tile__value.num', fmtRate(Math.round(values[values.length - 1]))),
      track(Sparkline({ values, type: 'area', protocol: id, responsive: true, height: 36, format: (value) => fmtRate(Math.round(value)), ariaLabel: `Débit ${protocol(id).short} sur 48 secondes` })),
    );
  });
  return DemoCard(
    { title: 'Sparkline adaptative', subtitle: 'responsive: true — la courbe suit la largeur de sa tuile (ResizeObserver)', span: 6 },
    h('div.kit-tiles', tiles),
    Usage("Sparkline({ values, type: 'area', responsive: true, height: 36 })   // penser à destroy()"),
  );
}

/** Issues d'une série de scénarios de contrat, avant et après correction du contrat v2. */
const OUTCOMES = [
  [
    { id: 'ok', label: 'Compatibles', value: 5, tone: 'success', hint: 'Le client v1 obtient le bon résultat' },
    { id: 'rejected', label: 'Rejetés', value: 3, tone: 'info', hint: 'Erreur explicite : UNIMPLEMENTED, INVALID_ARGUMENT' },
    { id: 'crashed', label: 'Plantages', value: 2, tone: 'warning', hint: 'Exception côté client en lisant la réponse' },
    { id: 'silent', label: 'Silencieux', value: 3, tone: 'danger', hint: 'Aucune erreur, mais une valeur fausse' },
    { id: 'skipped', label: 'Non joués', value: 0, tone: 'neutral' },
  ],
  [
    { id: 'ok', label: 'Compatibles', value: 9, tone: 'success', hint: 'Le client v1 obtient le bon résultat' },
    { id: 'rejected', label: 'Rejetés', value: 2, tone: 'info', hint: 'Erreur explicite : UNIMPLEMENTED, INVALID_ARGUMENT' },
    { id: 'crashed', label: 'Plantages', value: 0, tone: 'warning' },
    { id: 'silent', label: 'Silencieux', value: 1, tone: 'danger', hint: 'Aucune erreur, mais une valeur fausse' },
    { id: 'skipped', label: 'Non joués', value: 1, tone: 'neutral' },
  ],
];

/**
 * Anneau découpé en plusieurs parts, avec légende chiffrée et info-bulles.
 * @param {(chart: HTMLElement) => HTMLElement} track
 * @returns {HTMLElement}
 */
export function segmentedDonutCard(track) {
  const outcomes = track(Donut({ label: 'scénarios joués', size: 132, thickness: 12, segments: OUTCOMES[0] }));
  const request = track(
    Donut({
      label: 'requête REST',
      size: 104,
      legend: 'bottom',
      format: fmtBytes,
      segments: [
        { label: 'Ligne de requête', value: 37, color: 'color-mix(in srgb, var(--proto-rest) 45%, transparent)' },
        { label: 'En-têtes HTTP', value: 98, protocol: 'rest' },
        { label: 'Corps JSON', value: 37, tone: 'neutral' },
      ],
    }),
  );
  let round = 0;
  const replay = Button({
    label: 'Corriger le contrat',
    icon: 'refresh-cw',
    size: 'sm',
    id: 'kit-donut-replay',
    onClick: () => {
      round = (round + 1) % OUTCOMES.length;
      outcomes.set(OUTCOMES[round]);
      replay.setLabel(round ? 'Revenir au contrat v2' : 'Corriger le contrat');
    },
  });
  return DemoCard(
    { title: 'Donut multi-segments', subtitle: 'segments: […] — total au centre, légende chiffrée, survol d’une part ou de sa ligne', span: 6, actions: replay },
    h('div.kit-donuts', outcomes, request),
    Usage("Donut({ label: 'scénarios joués', segments: [{ id, label, value, tone }], legend: 'right' })  →  .set(segments)"),
  );
}

/**
 * Histogramme superposé dont les médianes se touchent : les libellés fusionnent ou s'étagent.
 * @param {(chart: HTMLElement) => HTMLElement} track
 * @returns {HTMLElement}
 */
export function closeMediansCard(track) {
  const base = LATENCY.find((row) => row.protocol === 'custom');
  // Sur un lien à 40 ms la latence du réseau domine : les trois distributions se rejoignent presque.
  const shifted = (id, factor) => ({
    id,
    label: protocol(id).short,
    protocol: id,
    edges: base.histogram.edges_ms.map((edge) => edge * factor),
    counts: base.histogram.counts,
    markers: { p50: base.median_ms * factor, p95: base.p95_ms * factor },
  });
  const chart = track(
    Histogram({
      ariaLabel: 'Distributions superposées sur un réseau à 40 ms',
      layout: 'overlay',
      format: fmtMs,
      xScale: 'log',
      height: 230,
      markers: ['p50', 'p95'],
      series: [shifted('custom', 303), shifted('grpc', 321), shifted('rest', 439)],
    }),
  );
  return DemoCard(
    { title: 'Histogram superposé, médianes proches', subtitle: 'Réseau WAN simulé : les « p50 » de JSON-RPC et de gRPC fusionnent, les libellés voisins s’étagent', span: 12 },
    chart,
    Usage("Histogram({ layout: 'overlay', markers: ['p50', 'p95'], series })   // libellés fusionnés ou étagés, jamais superposés"),
  );
}

/**
 * Chronologie dont les traits fins (attentes) prennent la couleur de leur ton.
 * @param {(chart: HTMLElement) => HTMLElement} track
 * @returns {HTMLElement}
 */
export function thinToneCard(track) {
  const timeline = track(
    Timeline({
      ariaLabel: 'Attentes d’un client résilient, colorées par leur cause',
      minSpan: 3200,
      kinds: {
        cooldown: { label: 'Refroidissement du disjoncteur', tone: 'danger', style: 'thin' },
        probe: { label: 'Attente de l’essai en semi-ouvert', tone: 'warning', style: 'thin' },
      },
      lanes: [{ id: 'client', label: 'Client résilient', sublabel: 'échéance 500 ms · disjoncteur', protocol: 'grpc' }],
      items: [
        { id: 'a1', lane: 'client', start: 0, end: 500, kind: 'timeout', label: '1', title: 'Tentative 1', detail: 'TIMEOUT — échéance de 500 ms' },
        { id: 'w1', lane: 'client', start: 500, end: 700, kind: 'backoff', title: 'Attente avant nouvel essai', detail: '200 ms' },
        { id: 'a2', lane: 'client', start: 700, end: 760, kind: 'error', label: '2', title: 'Tentative 2', detail: 'UNAVAILABLE — connexion refusée' },
        { id: 'w2', lane: 'client', start: 760, end: 2260, kind: 'cooldown', title: 'Disjoncteur ouvert', detail: 'Aucun appel ne part pendant 1,5 s' },
        { id: 'w3', lane: 'client', start: 2260, end: 2700, kind: 'probe', title: 'Semi-ouvert', detail: 'Le prochain appel sert d’essai' },
        { id: 'a3', lane: 'client', start: 2700, end: 2760, kind: 'retry', label: '3', title: 'Essai en semi-ouvert', detail: 'Succès : le disjoncteur se referme' },
      ],
    }),
  );
  return DemoCard(
    { title: 'Timeline, attentes colorées', subtitle: 'style: \'thin\' — gris pour le ton neutre, sinon la couleur du ton (danger, warning…)', span: 12 },
    timeline,
    Usage("Timeline({ kinds: { cooldown: { label: 'Refroidissement du disjoncteur', tone: 'danger', style: 'thin' } }, lanes, items })"),
  );
}
