/**
 * Banc d'essai — « Taille des messages » : octets par procédure et par protocole, octets
 * réellement relayés sur TCP, tableau exact et montée en charge de `list_products`.
 */

import { h } from '../../core/dom.js';
import { fmtNumber } from '../../core/format.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { GroupedBarChart, LineChart } from '../../components/charts.js';
import { Badge, Card, Col, Grid, ProtocolChip, Segmented, Table } from '../../components/ui.js';
import { chartState, extreme, fmtOctets, fmtOctetsTick, fmtPct, fmtTimes, protocolLabel, suiteStatus } from './model.js';
import { Insight, SuiteSection, num } from './section.js';

/** Colonne du rapport lue pour chaque protocole, selon ce que l'on compare. */
const FIELDS = {
  wire: { custom: 'json_rpc_bytes', grpc: 'grpc_bytes', rest: 'rest_bytes' },
  message: { custom: 'json_bytes', grpc: 'protobuf_bytes', rest: 'rest_body_bytes' },
};
const SERIES_LABELS = {
  wire: { custom: 'JSON-RPC', grpc: 'gRPC', rest: 'REST' },
  message: { custom: 'JSON (JSON-RPC)', grpc: 'Protobuf', rest: 'JSON (corps REST)' },
};
const DIRECTIONS = { request: 'requête', response: 'réponse' };
const LOG_RATIO = 100;

const exact = (value) => (Number.isFinite(value) ? fmtNumber(value, { maxDecimals: 1 }) : null);
const shortName = (method) => method.split('_').slice(1).join('_') || method;

function deltaCell(percent) {
  if (!Number.isFinite(percent)) return null;
  return h('span.bench-delta', { dataset: { tone: percent < 0 ? 'success' : percent > 0 ? 'warning' : 'neutral' } }, fmtPct(percent, { signed: true }));
}

/** Octets confiés à la socket par l'application pour un appel complet (requête + réponse). */
function applicationBytes(rows, method, protocolId) {
  const field = FIELDS.wire[protocolId];
  const pair = rows.filter((row) => row.method === method);
  if (pair.length !== 2 || !field || pair.some((row) => !Number.isFinite(row[field]))) return null;
  return pair[0][field] + pair[1][field];
}

/**
 * Section « Taille des messages ».
 * @param {{track: (chart: HTMLElement) => HTMLElement, onRequest: (suite: string) => void}} props
 * @returns {{el: HTMLElement, update: (view: Object|null, run: import('./model.js').RunState) => void}}
 */
export function createPayloadSection({ track, onRequest }) {
  let direction = 'response';
  let framing = 'wire';
  let data = null;
  let status = null;

  /* --- Par procédure -------------------------------------------------------------------- */
  const sizesChart = track(GroupedBarChart({ ariaLabel: 'Taille des messages par procédure et par protocole', format: fmtOctets, tickFormat: fmtOctetsTick, monoLabels: true, height: 300 }));
  const sizesInsight = Insight();
  const sizesCard = Card(
    {
      title: 'Par procédure',
      subtitle: '',
      actions: [
        Segmented({
          size: 'sm',
          ariaLabel: 'Ce qui est compté',
          value: framing,
          options: [
            { value: 'wire', label: 'Sur la socket', title: 'Ce que le client écrit : préfixe de tramage ou message HTTP complet' },
            { value: 'message', label: 'Message seul', title: 'Le message sérialisé, sans tramage ni en-têtes' },
          ],
          onChange: (value) => {
            framing = value;
            drawSizes();
          },
        }),
        Segmented({
          size: 'sm',
          ariaLabel: 'Sens du message',
          value: direction,
          options: [
            { value: 'request', label: 'Requête' },
            { value: 'response', label: 'Réponse' },
          ],
          onChange: (value) => {
            direction = value;
            drawSizes();
          },
        }),
      ],
    },
    sizesChart,
    sizesInsight,
  );

  function drawSizes() {
    const rows = (data?.rows ?? []).filter((row) => row.direction === direction);
    const fields = FIELDS[framing];
    const values = REMOTE_PROTOCOL_IDS.map((id) => rows.map((row) => (Number.isFinite(row[fields[id]]) ? row[fields[id]] : null)));
    const positive = values.flat().filter((value) => value > 0);
    const log = positive.length > 1 && Math.max(...positive) / Math.min(...positive) > LOG_RATIO;
    sizesCard.setSubtitle(
      `${framing === 'wire' ? 'Octets écrits sur la socket' : 'Message sérialisé seul'} · ${DIRECTIONS[direction]}${log ? ' · échelle logarithmique' : ''}`,
    );
    sizesChart.update({
      ...chartState(status),
      log,
      categories: rows.map((row) => ({ id: row.method, label: row.method, short: shortName(row.method) })),
      series: REMOTE_PROTOCOL_IDS.map((id, index) => ({
        id,
        label: SERIES_LABELS[framing][id],
        protocol: id,
        values: log ? values[index].map((value) => (value > 0 ? value : null)) : values[index],
      })),
    });

    const totals = REMOTE_PROTOCOL_IDS.map((id, index) => ({ id, total: values[index].reduce((sum, value) => sum + (value ?? 0), 0), complete: values[index].every((value) => value !== null) })).filter(
      (entry) => entry.complete && entry.total > 0,
    );
    const lightest = extreme(totals, (entry) => entry.total, 'min');
    const heaviest = extreme(totals, (entry) => entry.total, 'max');
    if (!lightest || !heaviest || lightest === heaviest) {
      sizesInsight.set();
      return;
    }
    const emptyBodies = framing === 'message' && values[2].some((value) => value === 0);
    sizesInsight.set(
      `Somme des ${rows.length} ${DIRECTIONS[direction]}s : `,
      totals.flatMap((entry, index) => [index ? ', ' : '', `${SERIES_LABELS[framing][entry.id]} `, num(fmtOctets(entry.total))]),
      '. ',
      `${SERIES_LABELS[framing][lightest.id]} pèse `,
      num(fmtTimes(heaviest.total / lightest.total)),
      ` moins que ${SERIES_LABELS[framing][heaviest.id]}.`,
      emptyBodies ? ' Une lecture REST est un GET : sa requête n’a pas de corps, tout est dans l’URL.' : '',
    );
  }

  /* --- Sur le fil ----------------------------------------------------------------------- */
  const wireChart = track(GroupedBarChart({ ariaLabel: 'Octets réellement relayés sur TCP par appel', format: fmtOctets, tickFormat: fmtOctetsTick, colorBy: 'category', valueLabels: true, barSize: 34, height: 224 }));
  const wireTable = Table({
    density: 'compact',
    stickyHeader: false,
    caption: 'Octets relayés par les proxys, par appel',
    empty: { icon: 'cable', title: 'Aucun relevé', text: 'Les compteurs des proxys n’ont pas encore été lus.' },
    columns: [
      { key: 'protocol', label: 'Protocole', format: (value) => ProtocolChip(value, { variant: 'plain', short: true }) },
      { key: 'bytes_up_per_call', label: 'Envoyés', align: 'right', mono: true, format: exact, title: 'Client → serveur : octets montants relayés par le proxy, par appel' },
      { key: 'bytes_down_per_call', label: 'Reçus', align: 'right', mono: true, format: exact, title: 'Serveur → client : octets descendants relayés par le proxy, par appel' },
      { key: 'total_per_call', label: 'Total TCP', align: 'right', mono: true, format: exact },
      { key: 'application', label: 'Vus par l’appli', align: 'right', mono: true, format: exact, title: 'Octets que le client a écrits sur la socket (requête + réponse), d’après la trace de l’appel' },
      {
        key: 'transport',
        label: 'Transport',
        align: 'right',
        mono: true,
        title: 'Différence : ce que le transport ajoute sans que l’application le voie',
        format: (value) => (Number.isFinite(value) ? h('span.bench-delta', { dataset: { tone: value > 0 ? 'warning' : 'neutral' } }, `${value > 0 ? '+' : ''}${fmtNumber(value, { maxDecimals: 1 })}`) : null),
      },
    ],
    rowKey: (row) => row.protocol,
  });
  const wireInsight = Insight();
  const wireCard = Card({ title: 'Octets réels sur le fil par appel', subtitle: '' }, h('div.bench-wire', h('div.bench-wire__chart', wireChart), h('div.bench-wire__detail', wireTable, wireInsight)));

  function drawWire() {
    const wire = data?.wire ?? [];
    const measured = wire.filter((row) => Number.isFinite(row.total_per_call));
    const method = wire[0]?.method;
    wireCard.setSubtitle(method ? `Ce qui a traversé TCP pour ${method} — compteurs des proxys, ${fmtNumber(wire[0].calls)} appels, connexion déjà ouverte` : 'Ce qui a vraiment traversé TCP, relevé par les proxys');
    wireChart.update({
      ...chartState(status),
      categories: measured.map((row) => ({ id: row.protocol, label: protocolLabel(row), short: protocol(row.protocol).short, protocol: row.protocol })),
      series: [
        { id: 'up', label: 'Client → serveur', values: measured.map((row) => row.bytes_up_per_call) },
        { id: 'down', label: 'Serveur → client', values: measured.map((row) => row.bytes_down_per_call) },
      ],
    });
    const rows = wire.map((row) => {
      const application = applicationBytes(data?.rows ?? [], row.method, row.protocol);
      const transport = application !== null && Number.isFinite(row.total_per_call) ? Math.round((row.total_per_call - application) * 10) / 10 : null;
      return { ...row, application, transport };
    });
    if (status !== 'ready' && !rows.length) wireTable.setLoading(status !== 'skipped', 3);
    else wireTable.setRows(rows);

    const lightest = extreme(measured, (row) => row.total_per_call, 'min');
    const heaviest = extreme(measured, (row) => row.total_per_call, 'max');
    const hidden = extreme(
      rows.filter((row) => row.transport > 0),
      (row) => row.transport,
      'max',
    );
    if (!lightest || !heaviest || lightest === heaviest) {
      wireInsight.set();
      return;
    }
    wireInsight.set(
      `${protocolLabel(lightest)} échange `,
      num(fmtTimes(heaviest.total_per_call / lightest.total_per_call)),
      ` moins d’octets que ${protocolLabel(heaviest)}.`,
      hidden
        ? [
            ` ${protocolLabel(hidden)} : `,
            num(fmtOctets(hidden.transport)),
            ' de trames et d’en-têtes que l’application ne voit jamais — seul un compteur placé sur TCP les révèle.',
          ]
        : '',
    );
  }

  /* --- Montée en charge ------------------------------------------------------------------- */
  const scalingChart = track(
    LineChart({
      ariaLabel: 'Taille de la réponse list_products selon le nombre de produits',
      xScale: 'log',
      yScale: 'log',
      xFormat: (value) => fmtNumber(value),
      yFormat: fmtOctets,
      yTickFormat: fmtOctetsTick,
      xLabel: 'Produits renvoyés',
      height: 300,
    }),
  );
  const scalingInsight = Insight();
  const scalingCard = Card({ title: 'Montée en charge', subtitle: 'Réponse de list_products, de 1 à 1 000 produits — axes logarithmiques' }, scalingChart, scalingInsight);

  function drawScaling() {
    const scaling = data?.scaling ?? [];
    scalingChart.update({
      ...chartState(status),
      xTicks: scaling.map((row) => row.items),
      series: REMOTE_PROTOCOL_IDS.map((id) => ({
        id,
        label: protocol(id).short,
        protocol: id,
        points: scaling.filter((row) => row[FIELDS.wire[id]] > 0).map((row) => [row.items, row[FIELDS.wire[id]]]),
      })),
    });
    const usable = scaling.filter((row) => row.json_bytes > 0 && row.protobuf_bytes > 0);
    const largest = extreme(usable, (row) => row.items, 'max');
    const smallest = extreme(usable, (row) => row.items, 'min');
    if (!largest) {
      scalingInsight.set();
      return;
    }
    const envelope = (row) => (row.rest_bytes > 0 && Number.isFinite(row.rest_body_bytes) ? (row.rest_bytes - row.rest_body_bytes) / row.rest_bytes : null);
    const parts = [
      `À ${fmtNumber(largest.items)} produits : `,
      num(fmtOctets(Math.round(largest.protobuf_bytes / largest.items))),
      ' par produit en Protobuf contre ',
      num(fmtOctets(Math.round(largest.json_bytes / largest.items))),
      ' en JSON (',
      num(fmtPct(largest.protobuf_vs_json_pct, { signed: true })),
      ').',
    ];
    if (smallest && smallest !== largest && envelope(smallest) !== null && envelope(largest) !== null) {
      parts.push(
        ' L’enveloppe HTTP de REST pèse ',
        num(fmtPct(envelope(smallest) * 100)),
        ` de la réponse pour ${fmtNumber(smallest.items)} produit${smallest.items > 1 ? 's' : ''}, `,
        num(fmtPct(envelope(largest) * 100, { decimals: 2 })),
        ` pour ${fmtNumber(largest.items)} : les deux courbes JSON finissent par se confondre.`,
      );
    }
    scalingInsight.set(parts);
  }

  /* --- Tableau exact ---------------------------------------------------------------------- */
  const table = Table({
    density: 'compact',
    stickyHeader: false,
    caption: 'Taille exacte des messages, en octets',
    empty: { icon: 'ruler', title: 'Aucune taille mesurée', text: 'La suite « Tailles » n’a pas encore produit de ligne.' },
    columns: [
      { key: 'method', label: 'Procédure', mono: true, title: 'Procédure appelée avec ses paramètres par défaut' },
      { key: 'direction', label: 'Sens', format: (value) => Badge({ label: DIRECTIONS[value] ?? value, size: 'sm', tone: value === 'request' ? 'neutral' : 'accent', variant: 'outline' }) },
      { key: 'json_bytes', label: 'JSON', align: 'right', mono: true, format: exact, title: 'Message JSON-RPC sérialisé, sans préfixe' },
      { key: 'protobuf_bytes', label: 'Protobuf', align: 'right', mono: true, format: exact, title: 'Message Protobuf sérialisé, sans préfixe' },
      { key: 'protobuf_vs_json_pct', label: 'Écart', align: 'right', mono: true, format: deltaCell, title: 'Protobuf par rapport à JSON, messages seuls : négatif quand Protobuf est plus petit' },
      { key: 'json_rpc_bytes', label: 'JSON-RPC', align: 'right', mono: true, format: exact, title: 'Le message JSON précédé de son préfixe de longueur (4 octets)' },
      { key: 'grpc_bytes', label: 'gRPC', align: 'right', mono: true, format: exact, title: 'Le message Protobuf précédé du préfixe gRPC (5 octets)' },
      { key: 'rest_bytes', label: 'REST', align: 'right', mono: true, format: exact, title: 'Message HTTP complet : ligne de départ, en-têtes et corps' },
      { key: 'rest_body_bytes', label: 'Corps', align: 'right', mono: true, format: exact, title: 'Dont corps JSON du message HTTP REST' },
    ],
    rowKey: (row) => `${row.method}/${row.direction}`,
  });
  const tableCard = Card(
    {
      title: 'Octets exacts',
      subtitle: 'Un appel tracé par procédure et par protocole, paramètres par défaut',
      padding: 'none',
      footer: h(
        'span',
        'JSON, Protobuf : message sérialisé seul, et l’écart entre les deux. JSON-RPC, gRPC : le même message précédé de son préfixe de tramage (4 et 5 octets). REST : message HTTP complet, ligne de départ et en-têtes compris.',
      ),
    },
    table,
  );

  function drawTable() {
    const rows = data?.rows ?? [];
    if (status !== 'ready' && !rows.length) table.setLoading(status !== 'skipped', 8);
    else table.setRows(rows);
  }

  const section = SuiteSection(
    {
      suite: 'payload',
      description: 'Combien d’octets faut-il pour dire la même chose ? Le même appel, sérialisé par chaque protocole, puis compté là où il passe vraiment : sur TCP.',
      onRequest,
    },
    Grid(
      Col({ span: 7, md: 12 }, sizesCard),
      Col({ span: 5, md: 12 }, scalingCard),
      Col({ span: 12 }, wireCard),
      Col({ span: 12 }, tableCard),
    ),
  );

  return {
    el: section.el,
    update(view, run) {
      const nextStatus = suiteStatus(view, run, 'payload');
      const nextData = view?.payload ?? null;
      if (nextStatus === status && nextData === data) return;
      status = nextStatus;
      data = nextData;
      section.setStatus(status);
      drawSizes();
      drawWire();
      drawScaling();
      drawTable();
    },
  };
}
