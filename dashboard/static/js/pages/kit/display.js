/**
 * Guide de style — affichage : marqueurs, indicateurs, tableaux, retours d'état.
 */

import { h, svg } from '../../core/dom.js';
import { fmtBytes, fmtMs, fmtNumber, fmtPercent, fmtRate } from '../../core/format.js';
import { PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import {
  Badge,
  Button,
  Callout,
  Card,
  Chip,
  Col,
  CountUp,
  Divider,
  EmptyState,
  Grid,
  Kbd,
  KeyValue,
  Progress,
  ProtocolChip,
  Section,
  Skeleton,
  Stat,
  StatusDot,
  Table,
} from '../../components/ui.js';
import { DemoCard, Specimen, Usage } from './demo.js';
import { BENCH_ROWS, CALL_ROWS } from './samples.js';

const TONES = ['neutral', 'accent', 'success', 'warning', 'danger', 'info'];
const TONE_LABELS = { neutral: 'Neutre', accent: 'Accent', success: 'Succès', warning: 'Avertissement', danger: 'Danger', info: 'Info' };

/** États successifs d'un serveur et du réseau, pour la démonstration de `update()`. */
const SERVER_STATES = [
  { label: 'En service', tone: 'success', dot: true, pulse: true, icon: null },
  { label: 'Dégradé', tone: 'warning', dot: true, pulse: false, icon: null },
  { label: 'Arrêté', tone: 'danger', dot: false, icon: 'power-off' },
];
const NETWORK_STATES = [
  { label: 'Réseau idéal', tone: 'neutral', icon: 'wifi' },
  { label: 'Mobile 3G', tone: 'warning', icon: 'smartphone' },
  { label: 'Trou noir', tone: 'danger', icon: 'circle-off' },
];

function markers() {
  // update() : la même étiquette change de libellé, de ton, d'icône et de point sans être recréée.
  const server = Badge(SERVER_STATES[0]);
  const network = Chip({ ...NETWORK_STATES[0], selected: true, onClick: () => {} });
  let step = 0;
  const next = Button({
    label: 'État suivant',
    size: 'sm',
    iconRight: 'arrow-right',
    id: 'kit-marker-next',
    onClick: () => {
      step = (step + 1) % SERVER_STATES.length;
      server.update(SERVER_STATES[step]);
      network.update(NETWORK_STATES[step]);
    },
  });

  return Section(
    { title: 'Marqueurs', description: 'Badge (statique), Chip (interactive), ProtocolChip, StatusDot et Kbd.' },
    Grid(
      DemoCard(
        { title: 'Badge', subtitle: 'Six tons, trois variantes, point facultatif', span: 6 },
        Specimen('Soft', TONES.map((tone) => Badge({ label: TONE_LABELS[tone], tone }))),
        Specimen('Outline', TONES.map((tone) => Badge({ label: TONE_LABELS[tone], tone, variant: 'outline' }))),
        Specimen('Solid', TONES.map((tone) => Badge({ label: TONE_LABELS[tone], tone, variant: 'solid' }))),
        Specimen('Avec point', Badge({ label: 'En service', tone: 'success', dot: true, pulse: true }), Badge({ label: 'Dégradé', tone: 'warning', dot: true }), Badge({ label: 'Arrêté', tone: 'danger', dot: true }), Badge({ label: 'idempotent', tone: 'info', icon: 'shield-check' }), Badge({ label: 'breaking', tone: 'danger', icon: 'triangle-alert' })),
        Specimen('Codes', Badge({ label: 'TIMEOUT', tone: 'danger', size: 'sm', mono: true }), Badge({ label: '-32601', tone: 'warning', size: 'sm', mono: true }), Badge({ label: 'UNAVAILABLE', tone: 'danger', size: 'sm', mono: true, variant: 'outline' }), Badge({ label: '200', tone: 'success', size: 'sm', mono: true }), Badge({ label: 'v2', size: 'sm', mono: true })),
      ),
      DemoCard(
        { title: 'ProtocolChip, Chip, StatusDot, Kbd', span: 6 },
        Specimen('ProtocolChip', PROTOCOL_IDS.map((id) => ProtocolChip(id))),
        Specimen('Variantes', ProtocolChip('custom', { variant: 'outline' }), ProtocolChip('grpc', { variant: 'outline', short: true }), ProtocolChip('rest', { variant: 'plain' }), ProtocolChip('grpc', { short: true, size: 'sm' }), ProtocolChip('custom', { short: true, size: 'sm', icon: true }), ProtocolChip('local', { size: 'sm', variant: 'plain' })),
        Specimen(
          'Légende',
          PROTOCOL_IDS.map((id, index) => Chip({ label: protocol(id).short, color: protocol(id).color, selected: index !== 0, onToggle: () => {} })),
        ),
        Specimen('Filtres', Chip({ label: 'Erreurs seulement', icon: 'filter', tone: 'danger', selected: true, onToggle: () => {} }), Chip({ label: 'update_stock', onRemove: () => {} }), Chip({ label: 'via proxy', icon: 'shuffle', onClick: () => {} })),
        Specimen('StatusDot', StatusDot({ tone: 'success', pulse: true, label: 'En direct' }), StatusDot({ tone: 'warning', pulse: true, label: 'Connexion…' }), StatusDot({ tone: 'danger', label: 'Arrêté' }), StatusDot({ tone: 'muted', label: 'Hors ligne' }), StatusDot({ tone: 'info', size: 'sm', label: 'Semi-ouvert' })),
        Specimen('Kbd', Kbd('mod+k'), Kbd('g o'), Kbd('?'), Kbd('esc'), Kbd('enter'), Kbd('shift+up')),
        Specimen('update()', server, network, next),
        Usage("badge.update({ label: 'Arrêté', tone: 'danger', dot: false, icon: 'power-off' })  ·  chip.update({ label, tone, icon, dot })"),
      ),
    ),
  );
}

/** Mini-courbe d'exemple pour l'emplacement `trailing` d'une Stat (les vrais graphiques viennent de charts.js). */
function sparkline(values) {
  const max = Math.max(...values);
  const points = values.map((value, index) => `${(index / (values.length - 1)) * 72},${26 - (value / max) * 24}`).join(' ');
  return svg(
    'svg',
    { class: 'kit-spark', viewBox: '0 0 72 28', width: 72, height: 28, fill: 'none', 'aria-hidden': 'true' },
    svg('polyline', { points, stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }),
  );
}

function stats() {
  const calls = Stat({
    label: 'Appels',
    icon: 'activity',
    value: 12840,
    format: (v) => fmtNumber(Math.round(v)),
    delta: { label: '+128 /s', tone: 'success', direction: 'up' },
    hint: 'depuis le démarrage',
    trailing: sparkline([4, 6, 5, 9, 8, 12, 10, 14, 13, 18, 16, 21]),
  });
  const latency = Stat({ label: 'Latence médiane', protocol: 'grpc', value: 0.318, format: fmtMs, delta: { label: '−12 %', tone: 'success', direction: 'down' }, hint: 'vs JSON-RPC' });
  const payload = Stat({ label: 'Charge utile', protocol: 'custom', value: 318, format: fmtBytes, hint: 'get_product_details · réponse' });
  const errors = Stat({ label: 'Taux d’erreur', icon: 'triangle-alert', value: 0.021, format: (v) => fmtPercent(v, { decimals: 1 }), delta: { label: '+1,4 pt', tone: 'danger', direction: 'up' }, hint: 'réseau « Instable »' });
  const big = Stat({ label: 'Débit mesuré', size: 'lg', value: 6890, format: (v) => fmtRate(Math.round(v)), hint: 'JSON-RPC maison · 1 connexion · boucle locale' });
  const counter = CountUp({ value: 1284056 });

  const shuffle = Button({
    label: 'Simuler de nouvelles mesures',
    icon: 'refresh-cw',
    size: 'sm',
    id: 'kit-stats-shuffle',
    onClick: () => {
      calls.update({ value: 12840 + Math.round(Math.random() * 9000) });
      latency.update({ value: 0.2 + Math.random() * 0.4 });
      payload.update({ value: 100 + Math.round(Math.random() * 4000) });
      errors.update({ value: Math.random() * 0.06 });
      big.update({ value: 3000 + Math.random() * 6000 });
      counter.set(counter.value + Math.round(Math.random() * 50000));
    },
  });

  return Section(
    { title: 'Indicateurs', description: 'Stat : valeur tabulaire animée, unité séparée, variation, emplacement pour une mini-courbe.', actions: shuffle },
    Grid(
      Col({ span: 3, md: 6 }, Card(calls)),
      Col({ span: 3, md: 6 }, Card({ protocol: 'grpc' }, latency)),
      Col({ span: 3, md: 6 }, Card({ protocol: 'custom' }, payload)),
      Col({ span: 3, md: 6 }, Card({ accent: 'danger' }, errors)),
      DemoCard(
        { title: 'Stat (grand format) et CountUp', span: 5 },
        big,
        Divider(),
        Specimen('CountUp', h('span.t-heading', counter), h('span.fg-2', 'appels cumulés')),
        Usage('Stat({ label, value, format: fmtMs, delta: { label, tone, direction }, hint, protocol })  →  .update({ value })'),
      ),
      DemoCard(
        { title: 'KeyValue', subtitle: 'En ligne (avec copie au survol) ou empilée', span: 7 },
        h(
          'div.kit-split',
          KeyValue({
            items: [
              { label: 'Identifiant d’appel', value: 'grpc-000128', mono: true, copy: true },
              { label: 'Protocole', value: ProtocolChip('grpc', { size: 'sm' }) },
              { label: 'Procédure', value: 'update_stock', mono: true },
              { label: 'Durée totale', value: fmtMs(0.412), mono: true },
              { label: 'Statut', value: 'FAILED_PRECONDITION', mono: true, tone: 'danger' },
            ],
          }),
          KeyValue({
            layout: 'stacked',
            columns: 2,
            items: [
              { label: 'Requête', value: fmtBytes(15), mono: true },
              { label: 'Réponse', value: fmtBytes(121), mono: true },
              { label: 'Sérialisation', value: '1,8 µs', mono: true },
              { label: 'Transport', value: 'HTTP/2' },
            ],
          }),
        ),
      ),
    ),
  );
}

function tables() {
  const benchColumns = [
    { key: 'protocol', label: 'Protocole', sortable: true, format: (value) => ProtocolChip(value, { variant: 'plain' }), sortValue: (row) => protocol(row.protocol).label },
    { key: 'count', label: 'Appels', align: 'right', mono: true, sortable: true, format: (value) => fmtNumber(value) },
    { key: 'mean_ms', label: 'Moyenne', align: 'right', mono: true, sortable: true, format: (value) => fmtMs(value) },
    { key: 'p95_ms', label: 'p95', align: 'right', mono: true, sortable: true, format: (value) => fmtMs(value) },
    { key: 'p99_ms', label: 'p99', align: 'right', mono: true, sortable: true, format: (value) => fmtMs(value) },
    { key: 'request_bytes', label: 'Requête', align: 'right', mono: true, sortable: true, format: (value) => (value ? fmtBytes(value) : null) },
    { key: 'response_bytes', label: 'Réponse', align: 'right', mono: true, sortable: true, format: (value) => (value ? fmtBytes(value) : null) },
    { key: 'rps', label: 'Débit', align: 'right', mono: true, sortable: true, format: (value) => fmtRate(value) },
    { key: 'errors', label: 'Erreurs', align: 'right', mono: true, sortable: true, format: (value) => (value ? Badge({ label: value, tone: 'danger', size: 'sm', mono: true }) : '0') },
  ];
  const callColumns = [
    { key: 'call_id', label: 'Appel', mono: true },
    { key: 'protocol', label: 'Protocole', format: (value) => ProtocolChip(value, { short: true, size: 'sm' }) },
    { key: 'method', label: 'Procédure', mono: true },
    { key: 'status', label: 'Statut', format: (value) => Badge({ label: value === 'ok' ? 'OK' : 'TIMEOUT', tone: value === 'ok' ? 'success' : 'danger', size: 'sm', mono: true }) },
    { key: 'duration_ms', label: 'Durée', align: 'right', mono: true, sortable: true, format: (value) => fmtMs(value) },
    { key: 'bytes', label: 'Octets', align: 'right', mono: true, sortable: true, format: (value) => fmtBytes(value) },
  ];
  const loading = Table({ columns: callColumns.slice(0, 4), density: 'compact' });
  loading.setLoading(true, 3);

  // layout: 'fixed' + setColumns() : une colonne par protocole mesuré, ajoutée ou retirée sans recréer le tableau.
  const measured = new Set(['custom', 'grpc']);
  const statRows = [
    { metric: 'Moyenne', note: 'Somme des durées divisée par le nombre d’appels : sensible aux quelques appels très lents', key: 'mean_ms' },
    { metric: 'p95', note: '95 % des appels sont plus rapides', key: 'p95_ms' },
    { metric: 'p99', note: 'La traîne : un appel sur cent dépasse cette durée', key: 'p99_ms' },
  ].map((row) => ({ ...row, ...Object.fromEntries(BENCH_ROWS.map((bench) => [bench.protocol, bench[row.key]])) }));
  const statColumns = () => [
    { key: 'metric', label: 'Mesure', width: 96 },
    { key: 'note', label: 'Lecture' },
    ...PROTOCOL_IDS.filter((id) => measured.has(id)).map((id) => ({ key: id, label: protocol(id).short, align: 'right', mono: true, width: 104, format: (value) => fmtMs(value) })),
  ];
  const stats = Table({ columns: statColumns(), rows: statRows, layout: 'fixed', density: 'compact', rowKey: (row) => row.metric });
  const pickers = PROTOCOL_IDS.map((id) =>
    Chip({
      label: protocol(id).short,
      color: protocol(id).color,
      selected: measured.has(id),
      onToggle: (on) => {
        if (on) measured.add(id);
        else measured.delete(id);
        stats.setColumns(statColumns());
      },
    }),
  );

  return Section(
    { title: 'Tableaux', description: 'Lignes séparées par un filet, en-tête collant, tri par colonne, densité compacte, état vide et chargement.' },
    Grid(
      Col(
        { span: 12 },
        Card(
          { title: 'Latence par protocole', subtitle: 'get_product_details · 1 000 appels · boucle locale', icon: 'gauge', padding: 'none', actions: Button({ label: 'Exporter', icon: 'download', size: 'sm' }), footer: [h('span', 'Bus de traces coupé pendant la mesure'), h('span.num', 'rapport bench-20261003-1532')] },
          Table({ columns: benchColumns, rows: BENCH_ROWS, sort: { key: 'mean_ms', dir: 'asc' }, rowKey: (row) => row.protocol, caption: 'Latence par protocole' }),
        ),
      ),
      Col(
        { span: 7, md: 12 },
        Card(
          { title: 'Derniers appels', subtitle: 'Densité compacte, lignes cliquables, liseré d’état', padding: 'none' },
          Table({ columns: callColumns, rows: CALL_ROWS, density: 'compact', onRowClick: () => {}, rowTone: (row) => (row.status === 'error' ? 'danger' : null), rowKey: (row) => row.call_id }),
        ),
      ),
      Col(
        { span: 5, md: 12 },
        h(
          'div.kit-stack',
          Card({ title: 'Chargement', padding: 'none' }, loading),
          Card({ title: 'État vide', padding: 'none' }, Table({ columns: callColumns.slice(0, 4), rows: [], density: 'compact', empty: { icon: 'history', title: 'Aucun appel pour l’instant', text: 'Lancez un appel depuis la console.' } })),
        ),
      ),
      Col(
        { span: 12 },
        Card(
          {
            title: 'Largeur fixe, colonnes dynamiques',
            subtitle: 'layout: \'fixed\' — le tableau ne déborde jamais, le texte trop long est coupé (complet au survol) · setColumns() suit les protocoles mesurés',
            padding: 'none',
            actions: h('div.kit-chips', pickers),
          },
          stats,
        ),
      ),
    ),
  );
}

function feedback() {
  const bench = Progress({ value: 0.62, label: 'Benchmark — latence', detail: '620 / 1 000' });
  return Section(
    { title: 'Retours d’état', description: 'Callout, Progress, Skeleton et EmptyState.' },
    Grid(
      DemoCard(
        { title: 'Callout', span: 6 },
        Callout({ tone: 'info', title: 'Appel idempotent', text: 'Rejouer calculate_factorial ne change rien : la procédure n’a pas d’état.' }),
        Callout({ tone: 'success', title: 'Compatible', text: 'Un champ ajouté avec un nouveau numéro est ignoré par les anciens clients.' }),
        Callout({ tone: 'warning', title: 'Écriture non idempotente', text: 'Sans clé d’idempotence, une nouvelle tentative de update_stock peut débiter le stock deux fois.' }),
        Callout({ tone: 'danger', title: 'Corruption silencieuse', text: 'Le serveur v2 lit « delta » au champ n° 4 ; le client v1 l’envoie au n° 2. Aucun message d’erreur.', actions: Button({ label: 'Voir le scénario', size: 'sm', iconRight: 'arrow-right' }) }),
      ),
      DemoCard(
        { title: 'Progress et Skeleton', span: 6 },
        bench,
        Progress({ value: 1, tone: 'success', label: 'Taille des messages', detail: 'terminé' }),
        Progress({ indeterminate: true, label: 'Échauffement…', size: 'sm' }),
        h('div.kit-bars', PROTOCOL_IDS.map((id, index) => Progress({ value: [0.02, 0.27, 0.61, 1][index], color: protocol(id).color, label: protocol(id).label, detail: fmtMs([0.006, 0.142, 0.318, 0.521][index]), size: 'sm' }))),
        Divider({ label: 'Skeleton' }),
        h('div.kit-skeleton', Skeleton({ variant: 'circle' }), h('div.kit-skeleton__lines', Skeleton({ width: '40%' }), Skeleton({ lines: 2 }))),
        Skeleton({ variant: 'block', height: 56 }),
      ),
      DemoCard(
        { title: 'EmptyState', subtitle: 'Tailles sm · md, tons neutre · accent · danger', span: 12, padding: 'none' },
        h(
          'div.kit-empties',
          EmptyState({ size: 'sm', icon: 'history', title: 'Aucun rapport enregistré', text: 'Les benchmarks terminés apparaîtront ici.' }),
          EmptyState({ icon: 'gauge', tone: 'accent', title: 'Aucune mesure pour l’instant', text: 'Lancez le banc d’essai pour comparer les trois protocoles sur votre machine.', action: Button({ label: 'Lancer le benchmark', variant: 'primary', icon: 'play' }) }),
          EmptyState({ icon: 'unplug', tone: 'danger', title: 'Laboratoire hors ligne', text: 'Le serveur du laboratoire ne répond pas. Nouvelle tentative automatique…', action: Button({ label: 'Réessayer', icon: 'refresh-cw' }) }),
        ),
      ),
    ),
  );
}

/**
 * Sections « affichage » du guide de style.
 * @returns {HTMLElement[]}
 */
export function display() {
  return [markers(), stats(), tables(), feedback()];
}
