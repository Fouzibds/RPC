/**
 * Sous le capot — « Trois protocoles, un même appel » : les traces côte à côte. Tailles, durée,
 * lisibilité, et une bande d'octets à l'échelle (un carré par octet, teinté selon la nature du
 * segment) pour que l'écart de taille se voie avant de se lire.
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtNumber, fmtPercent, fmtUs } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { href } from '../../core/router.js';
import { protocol } from '../../core/protocols.js';
import { GroupedBarChart } from '../../components/charts.js';
import { Badge, Button, Callout, Card, Col, Grid, ProtocolChip } from '../../components/ui.js';
import { byteKinds } from './model.js';
import { readability } from './wire.js';

/** Nombre maximal de carrés d'une bande : au-delà, un carré représente plusieurs octets. */
const MAX_SQUARES = 640;

const KIND_LABELS = [
  ['frame', 'Tramage'],
  ['header', 'En-têtes'],
  ['tag', 'Tag de champ'],
  ['len', 'Longueur'],
  ['value', 'Valeur'],
  ['body', 'Corps'],
];

const times = (ratio) => `${fmtNumber(ratio, { maxDecimals: ratio >= 10 ? 0 : 1 })}×`;
const roundTrip = (model) => (model.request?.size ?? 0) + (model.response?.size ?? 0);

/** Bande d'octets à l'échelle : `unit` octets par carré, teinte du segment majoritaire. */
function strip(message, unit) {
  const kinds = byteKinds(message);
  const squares = [];
  for (let i = 0; i < kinds.length; i += unit) {
    const tally = new Map();
    for (const kind of kinds.slice(i, i + unit)) tally.set(kind, (tally.get(kind) ?? 0) + 1);
    const [kind] = [...tally.entries()].sort((a, b) => b[1] - a[1])[0];
    squares.push(h('i', { dataset: { kind } }));
  }
  return h('div.xr-strip', { role: 'img', 'aria-label': `${fmtBytes(message.size, { exact: true })}` }, squares);
}

/** Phrases calculées à partir des traces affichées. */
function findings(models) {
  const lines = [];
  const grpc = models.find((model) => model.protocol === 'grpc');
  const others = models.filter((model) => model !== grpc);
  const complete = models.every((model) => model.request && model.response);
  const sizeOf = complete ? roundTrip : (model) => model.request?.size ?? 0;
  const scope = complete ? 'aller-retour' : 'à l’aller';

  if (grpc && others.length && sizeOf(grpc) > 0) {
    const versus = others
      .filter((model) => sizeOf(model) > 0)
      .map((model) => {
        const ratio = sizeOf(model) / sizeOf(grpc);
        const verdict = ratio >= 1 ? `${times(ratio)} moins que` : `${times(1 / ratio)} plus que`;
        return `${verdict} ${protocol(model.protocol).short} (${fmtBytes(sizeOf(model), { exact: true })})`;
      });
    lines.push({
      icon: 'ruler',
      text: `Pour cet appel, Protobuf (gRPC) échange ${fmtBytes(sizeOf(grpc), { exact: true })} ${scope} : ${versus.join(' et ')}.${complete ? '' : ' Seules les requêtes sont comparées : toutes les réponses ne portent pas de message.'}`,
    });
  } else if (models.length > 1) {
    const sorted = [...models].filter((model) => sizeOf(model) > 0).sort((a, b) => sizeOf(a) - sizeOf(b));
    if (sorted.length > 1) {
      const [smallest, ...rest] = sorted;
      lines.push({
        icon: 'ruler',
        text: `${protocol(smallest.protocol).short} est le plus compact ${scope} (${fmtBytes(sizeOf(smallest), { exact: true })}) : ${rest
          .map((model) => `${times(sizeOf(model) / sizeOf(smallest))} moins que ${protocol(model.protocol).short}`)
          .join(', ')}.`,
      });
    }
  }

  const overhead = models
    .map((model) => {
      const kinds = byteKinds(model.request);
      const envelope = kinds.filter((kind) => kind === 'header' || kind === 'frame').length;
      return { model, envelope, share: kinds.length ? envelope / kinds.length : 0 };
    })
    .filter((item) => item.envelope > 0);
  if (overhead.length) {
    lines.push({
      icon: 'package',
      text: `Enveloppe de la requête (tramage et en-têtes) : ${overhead
        .map((item) => `${protocol(item.model.protocol).short} ${fmtBytes(item.envelope, { exact: true })}, soit ${fmtPercent(item.share, { decimals: 0 })}`)
        .join(' ; ')}.`,
    });
  }

  const readable = models.filter((model) => model.request).map((model) => ({ model, ...readability(model.request) }));
  if (readable.length) {
    lines.push({
      icon: 'eye',
      text: `Lisibilité de la requête : ${readable
        .map((item) => `${protocol(item.model.protocol).short} ${fmtPercent(item.share, { decimals: 0 })} d’octets imprimables (${item.text ? 'texte' : 'binaire'})`)
        .join(' ; ')}.`,
    });
  }

  const timed = models.filter((model) => model.totalUs > 0).sort((a, b) => a.totalUs - b.totalUs);
  if (timed.length > 1) {
    lines.push({
      icon: 'timer',
      text: `Durée de cet appel précis : ${timed.map((model) => `${protocol(model.protocol).short} ${fmtUs(model.totalUs)}`).join(', ')}. Un seul appel ne fait pas une mesure : le premier paie l’ouverture de la connexion.`,
      action: Button({ label: 'Mesurer sur des milliers d’appels', size: 'sm', variant: 'ghost', iconRight: 'arrow-right', href: href('benchmark') }),
    });
  }
  return lines;
}

/**
 * Section « Trois protocoles, un même appel ».
 * @param {{onPick: (protocolId: string) => void}} props `onPick` : ouvrir un protocole dans le schéma.
 * @returns {HTMLElement & {update: (models: Object[], active: string) => void, destroy: () => void}}
 */
export function Compare({ onPick }) {
  const cards = h('div.xr-compare');
  const legend = h('div.xr-legend');
  const notes = h('ul.xr-findings', { 'aria-live': 'polite' });
  const chart = GroupedBarChart({
    colorBy: 'category',
    format: (value) => fmtBytes(value, { exact: true }),
    tickBase: 1024,
    height: 232,
    ariaLabel: 'Taille de la requête et de la réponse par protocole',
    empty: { icon: 'chart-column', title: 'Rien à comparer', text: 'Aucun message n’a circulé sur le fil.' },
  });
  const el = h(
    'div.xr-compare-section',
    cards,
    legend,
    Grid(
      Col({ span: 6, md: 12 }, Card({ title: 'Octets échangés', subtitle: 'Requête (teinte pleine) et réponse (teinte claire), tramage et en-têtes compris.', class: 'xr-fill' }, chart)),
      Col({ span: 6, md: 12 }, Card({ title: 'Ce que montre cet appel', subtitle: 'Phrases calculées à partir des traces ci-dessus.', class: 'xr-fill' }, notes)),
    ),
  );

  function card(model, unit, active) {
    const info = protocol(model.protocol);
    const read = model.request ? readability(model.request) : null;
    const metric = (label, value, hint) => h('div.xr-metric', h('span.t-label', label), h('span.xr-metric__value.num', value), hint ? h('span.xr-metric__hint', hint) : null);
    const band = (label, message) =>
      h(
        'div.xr-band',
        h('span.xr-band__label', label, h('span.num', message ? fmtBytes(message.size, { exact: true }) : '—')),
        message ? strip(message, unit) : h('span.xr-band__none', 'aucun message'),
      );
    return Card(
      { protocol: model.protocol, class: 'xr-proto-card' },
      h(
        'header.xr-proto-card__head',
        ProtocolChip(model.protocol, { size: 'md' }),
        Button({
          label: active ? 'Affiché ci-dessus' : 'Disséquer',
          title: active ? 'Ce protocole est celui du schéma ci-dessus' : 'Ouvrir ce protocole dans le schéma',
          size: 'sm',
          variant: 'ghost',
          disabled: active,
          icon: active ? 'check' : 'scan-search',
          class: 'xr-proto-card__pick',
          onClick: () => onPick(model.protocol),
        }),
      ),
      h(
        'p.xr-proto-card__transport',
        model.ok ? Badge({ label: 'OK', tone: 'success', size: 'sm', dot: true }) : Badge({ label: model.failure?.code || 'Erreur', tone: 'danger', size: 'sm', dot: true, mono: true }),
        h('span', info.transport),
      ),
      h(
        'div.xr-metrics',
        metric('Requête', model.request ? fmtBytes(model.request.size, { exact: true }) : '—'),
        metric('Réponse', model.response ? fmtBytes(model.response.size, { exact: true }) : '—'),
        metric('Durée totale', fmtUs(model.totalUs)),
        metric('Lisibilité', read ? (read.text ? 'Texte' : 'Binaire') : '—', read ? `${fmtPercent(read.share, { decimals: 0 })} imprimable` : null),
      ),
      band('Requête', model.request),
      band('Réponse', model.response),
    );
  }

  el.update = (models, active) => {
    const largest = Math.max(1, ...models.flatMap((model) => [model.request?.size ?? 0, model.response?.size ?? 0]));
    const unit = Math.max(1, Math.ceil(largest / MAX_SQUARES));
    cards.dataset.count = String(models.length);
    clear(cards, models.map((model) => card(model, unit, model.protocol === active)));

    const used = new Set(models.flatMap((model) => [...byteKinds(model.request), ...byteKinds(model.response)]));
    clear(
      legend,
      h('span.xr-legend__scale.num', unit === 1 ? '1 carré = 1 octet' : `1 carré = ${fmtNumber(unit)} octets`),
      KIND_LABELS.filter(([kind]) => used.has(kind)).map(([kind, label]) => h('span.xr-legend__item', h('i', { dataset: { kind } }), label)),
    );

    chart.update({
      categories: models.map((model) => ({ id: model.protocol, label: protocol(model.protocol).label, short: protocol(model.protocol).short, protocol: model.protocol })),
      series: [
        { id: 'request', label: 'Requête', values: models.map((model) => model.request?.size ?? null) },
        { id: 'response', label: 'Réponse', values: models.map((model) => model.response?.size ?? null) },
      ],
    });

    const lines = findings(models);
    clear(
      notes,
      lines.map((line) =>
        h(
          'li.xr-finding',
          h('span.xr-finding__icon', { 'aria-hidden': 'true' }, icon(line.icon, { size: 14 })),
          h('div.xr-finding__body', h('span.xr-finding__text', line.text), line.action ?? null),
        ),
      ),
      models.length < 2
        ? h('li.xr-finding', Callout({ tone: 'info', text: 'Une seule trace est affichée. Cochez plusieurs protocoles puis relancez l’inspection pour les comparer sur le même appel.' }))
        : null,
    );
  };

  el.destroy = () => chart.destroy();
  return el;
}
