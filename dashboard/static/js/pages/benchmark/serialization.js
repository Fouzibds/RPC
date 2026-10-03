/**
 * Banc d'essai — « Sérialisation » : temps d'encodage et de décodage, JSON contre Protobuf,
 * pour une fiche produit et pour une liste de 100 produits, avec leurs tailles.
 */

import { h, clear } from '../../core/dom.js';
import { fmtNumber } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';
import { GroupedBarChart } from '../../components/charts.js';
import { Card, Col, Grid } from '../../components/ui.js';
import { chartState, fmtNs, fmtOctets, fmtPct, fmtTimes, suiteStatus } from './model.js';
import { Insight, SuiteSection, num } from './section.js';

const JSON_COLOR = protocol('custom').color;
const PROTOBUF_COLOR = protocol('grpc').color;
const CONVERSION_COLOR = `color-mix(in srgb, ${PROTOBUF_COLOR} 42%, transparent)`;

/** Regroupe les lignes du rapport par message : `{label, json, protobuf}`. */
function subjects(rows) {
  const map = new Map();
  for (const row of rows) {
    const key = row.message_type ?? row.message;
    if (!map.has(key)) map.set(key, { key, label: row.message, type: row.message_type, items: row.items, iterations: row.iterations });
    map.get(key)[row.format] = row;
  }
  return [...map.values()];
}

function SizePill(label, color, value) {
  return h('span.bench-size', h('span.bench-size__key', { style: { background: color }, 'aria-hidden': 'true' }), h('span.bench-size__label', label), h('span.bench-size__value.num', value));
}

/** Carte d'un message : tailles comparées, puis temps d'encodage et de décodage. */
function SubjectCard(track) {
  const chart = track(GroupedBarChart({ ariaLabel: 'Temps d’encodage et de décodage, JSON et Protobuf', format: fmtNs, height: 250, barSize: 48, valueLabels: true }));
  const sizes = h('div.bench-sizes');
  const insight = Insight();
  const card = Card({ title: '', subtitle: '' }, sizes, chart, insight);

  function set(subject, status) {
    const { json, protobuf } = subject ?? {};
    card.setTitle(subject?.label ?? 'Message');
    card.setSubtitle(subject ? `${subject.type ?? ''} · ${fmtNumber(subject.iterations)} tours par opération, meilleure de trois passes` : 'Encodage et décodage, par opération');
    const complete = Boolean(json && protobuf);
    sizes.hidden = !complete;
    if (complete) {
      const saving = ((protobuf.size_bytes - json.size_bytes) / json.size_bytes) * 100;
      clear(
        sizes,
        SizePill('JSON', JSON_COLOR, fmtOctets(json.size_bytes)),
        SizePill('Protobuf', PROTOBUF_COLOR, fmtOctets(protobuf.size_bytes)),
        h('span.bench-delta', { dataset: { tone: saving < 0 ? 'success' : 'warning' } }, fmtPct(saving, { signed: true })),
      );
    }
    chart.update({
      ...chartState(status),
      categories: [
        { id: 'encode', label: 'Encodage' },
        { id: 'decode', label: 'Décodage' },
      ],
      series: complete
        ? [
            { id: 'json', label: 'JSON', color: JSON_COLOR, values: [json.encode_ns, json.decode_ns] },
            { id: 'protobuf', label: 'Protobuf, codec seul', color: PROTOBUF_COLOR, values: [protobuf.encode_ns, protobuf.decode_ns] },
            { id: 'protobuf-dict', label: 'Protobuf + conversion dict ⇄ message', color: CONVERSION_COLOR, values: [protobuf.encode_from_dict_ns, protobuf.decode_to_dict_ns] },
          ]
        : [],
    });
    if (!complete) {
      insight.set();
      return;
    }
    const jsonTrip = json.encode_ns + json.decode_ns;
    const codecTrip = protobuf.encode_ns + protobuf.decode_ns;
    const fullTrip = protobuf.encode_from_dict_ns + protobuf.decode_to_dict_ns;
    const faster = codecTrip < jsonTrip;
    insight.set(
      'Aller-retour (encoder puis décoder) : ',
      num(fmtNs(codecTrip)),
      ' pour le codec Protobuf contre ',
      num(fmtNs(jsonTrip)),
      ' en JSON, soit ',
      num(fmtTimes(faster ? jsonTrip / codecTrip : codecTrip / jsonTrip)),
      faster ? ' plus rapide.' : ' plus lent.',
      ' Avec la conversion dictionnaire ⇄ message écrite en Python, Protobuf passe à ',
      num(fmtNs(fullTrip)),
      fullTrip > jsonTrip ? ' : plus que JSON — le typage du contrat se paie à la conversion.' : ' et reste devant JSON.',
    );
  }

  return { card, set };
}

/**
 * Section « Sérialisation ».
 * @param {{track: (chart: HTMLElement) => HTMLElement, onRequest: (suite: string) => void}} props
 * @returns {{el: HTMLElement, update: (view: Object|null, run: import('./model.js').RunState) => void}}
 */
export function createSerializationSection({ track, onRequest }) {
  const cards = [SubjectCard(track), SubjectCard(track)];
  let data = null;
  let status = null;

  const section = SuiteSection(
    {
      suite: 'serialization',
      description: 'Que coûte la traduction des données en octets, indépendamment du réseau ? Les mêmes données, encodées puis décodées par chaque format.',
      onRequest,
    },
    Grid(cards.map((item) => Col({ span: 6, md: 12 }, item.card))),
  );

  return {
    el: section.el,
    update(view, run) {
      const nextStatus = suiteStatus(view, run, 'serialization');
      const nextData = view?.serialization ?? null;
      if (nextStatus === status && nextData === data) return;
      status = nextStatus;
      data = nextData;
      section.setStatus(status);
      section.setContext(data ? [`${fmtNumber(data.iterations)} itérations demandées`, 'aucun réseau : le codec seul', 'durées par opération, en nanosecondes'] : []);
      const list = subjects(data?.rows ?? []);
      cards.forEach((item, index) => item.set(list[index] ?? null, status));
    },
  };
}
