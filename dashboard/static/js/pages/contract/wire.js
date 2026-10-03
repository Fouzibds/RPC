/**
 * Contrat & IDL — les octets d'un scénario. En gRPC : LES MÊMES OCTETS, montrés une fois dans
 * une vue hexadécimale et décodés deux fois, avec le contrat v1 du client et avec le contrat v2
 * du serveur. En JSON-RPC : la requête et la réponse, lisibles telles quelles.
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { CodeBlock } from '../../components/codeblock.js';
import { HexView, SegmentList } from '../../components/hexview.js';
import { JsonTree } from '../../components/jsontree.js';
import { Segmented } from '../../components/ui.js';
import { flatten, literal, plural } from './model.js';

const SIDES = { request: 'Requête', response: 'Réponse' };
const ROLES = { v1: 'client', v2: 'serveur' };
const MAX_GAPS = 8;

/** Rangs des segments qui recouvrent l'intervalle d'octets `[start, end[`. */
function overlapping(segments, start, end) {
  const out = [];
  segments.forEach((segment, index) => {
    if (segment.start < end && segment.end > start) out.push(index);
  });
  return out;
}

function lostNote(reading) {
  const lost = reading.lost ?? [];
  if (!lost.length) {
    return h('p.contract-reading__ok', icon('check', { size: 12 }), 'Tous les champs sont reconnus par ce contrat.');
  }
  return h(
    'ul.contract-reading__lost',
    lost.map((entry) => {
      const times = entry.count > 1 ? ` (${entry.count} fois)` : '';
      const text =
        entry.reason === 'wire_type_mismatch'
          ? [`type de fil ${entry.wire_type} reçu, ${entry.expected_wire_type} attendu : champ ignoré, valeur par défaut${times}`]
          : [`numéro inconnu de ce contrat (${entry.wire_type}) : champ sauté${times}`];
      return h('li', icon('triangle-alert', { size: 12 }), h('span', h('span.mono', `n°${entry.number} ${entry.path}`), ' — ', text));
    }),
  );
}

/** Une lecture des octets avec UN contrat : segments champ par champ, champs perdus, message obtenu. */
function readingPanel(version, reading, exchange, hexSegments, view, track) {
  const head = (subtitle) =>
    h(
      'header.contract-reading__head',
      h('span.contract-tag', { dataset: { version } }, version),
      h('div.contract-reading__heading', h('h6.contract-reading__title', `Lu avec le contrat ${version} — ${ROLES[version]}`), h('p.contract-reading__sub', subtitle)),
    );
  if (!reading) {
    return {
      el: h(
        'section.contract-reading.is-absent',
        { dataset: { version } },
        head('Aucune lecture possible'),
        h('p.contract-reading__absent', icon('ban', { size: 14 }), `Le contrat ${version} ne publie plus cette RPC : l’appel est rejeté avant que ces octets ne soient décodés.`),
      ),
      list: null,
    };
  }
  const list = track(
    SegmentList({
      hex: exchange.hex,
      segments: reading.segments,
      maxHeight: 248,
      onHover: (indices) => view.highlight(indices ? indices.flatMap((index) => overlapping(hexSegments, reading.segments[index].start, reading.segments[index].end)) : null),
    }),
  );
  const tree = track(JsonTree(reading.message, { collapsedDepth: 1, maxString: 40, copy: false, maxHeight: 168 }));
  return {
    el: h(
      'section.contract-reading',
      { dataset: { version, lost: String((reading.lost ?? []).length > 0) } },
      head([reading.label, ' · ', h('span.mono', reading.message_type)]),
      list,
      lostNote(reading),
      h('p.contract-reading__label.t-label', 'Ce que le code généré obtient'),
      tree,
    ),
    list,
    segments: reading.segments,
  };
}

/** Les champs que les deux contrats ne lisent pas de la même façon, côte à côte. */
function gaps(exchange) {
  if (!exchange.as_v1 || !exchange.as_v2) return null;
  const left = new Map(flatten(exchange.as_v1.message));
  const right = new Map(flatten(exchange.as_v2.message));
  const paths = [...new Set([...left.keys(), ...right.keys()])]
    .filter((path) => !left.has(path) || !right.has(path) || left.get(path) !== right.get(path))
    .sort((a, b) => a.localeCompare(b, 'fr', { numeric: true }));
  if (!paths.length) {
    return h('p.contract-gaps__same', icon('check', { size: 14 }), 'Les deux contrats tirent exactement le même message de ces octets.');
  }
  const cell = (map, path) => (map.has(path) ? h('span.mono', literal(map.get(path))) : h('span.contract-gaps__void', 'champ inexistant'));
  return h(
    'div.contract-gaps',
    h('p.contract-gaps__title', h('strong.num', String(paths.length)), paths.length > 1 ? ' champs ne sont pas lus de la même façon' : ' champ n’est pas lu de la même façon'),
    h(
      'table.contract-gaps__table',
      h('thead', h('tr', h('th', { scope: 'col' }, 'Champ'), h('th', { scope: 'col' }, 'Contrat v1 · client'), h('th', { scope: 'col' }, 'Contrat v2 · serveur'))),
      h(
        'tbody',
        paths.slice(0, MAX_GAPS).map((path) => h('tr', h('th.mono', { scope: 'row' }, path), h('td', cell(left, path)), h('td', cell(right, path)))),
      ),
    ),
    paths.length > MAX_GAPS ? h('p.contract-gaps__more', `… et ${plural(paths.length - MAX_GAPS, 'autre écart', 'autres écarts')} du même genre.`) : null,
  );
}

function exchangeView(exchange, side, track) {
  const writer = exchange.written_by === 'v2' ? 'v2' : 'v1';
  const hexSegments = (exchange[`as_${writer}`] ?? exchange.as_v1 ?? exchange.as_v2)?.segments ?? [];
  const panels = [];
  const view = track(
    HexView({
      hex: exchange.hex,
      segments: hexSegments,
      title: `${exchange.message} · ${SIDES[side].toLowerCase()} écrite par le ${ROLES[writer]} ${writer}`,
      maxBytes: 128,
      maxHeight: 208,
      legend: hexSegments.length <= 24,
      onSegmentHover: (segment) => {
        for (const panel of panels) {
          if (!panel.list) continue;
          panel.list.highlight(segment ? panel.segments.findIndex((candidate) => candidate.start <= segment.start && segment.start < candidate.end) : null);
        }
      },
    }),
  );
  panels.push(readingPanel('v1', exchange.as_v1, exchange, hexSegments, view, track), readingPanel('v2', exchange.as_v2, exchange, hexSegments, view, track));
  return [view, gaps(exchange), h('div.contract-readings', panels.map((panel) => panel.el))];
}

function protobufWire(wire) {
  const available = Object.keys(SIDES).filter((side) => wire[side]);
  if (!available.length) return null;
  let current = available.includes(wire.focus) ? wire.focus : available[0];
  let live = [];
  const stage = h('div.contract-wire__stage');
  const track = (component) => {
    live.push(component);
    return component;
  };
  const release = () => {
    live.forEach((component) => component.destroy());
    live = [];
  };
  const render = () => {
    release();
    clear(stage, exchangeView(wire[current], current, track));
  };
  const switcher = Segmented({
    size: 'sm',
    value: current,
    ariaLabel: 'Message affiché',
    options: Object.entries(SIDES).map(([side, text]) => ({
      value: side,
      label: wire[side] ? `${text} · ${fmtBytes(wire[side].size, { exact: true })}` : text,
      disabled: !wire[side],
      title: wire[side] ? undefined : 'Aucun message : l’appel a été rejeté',
    })),
    onChange: (value) => {
      current = value;
      render();
    },
  });
  render();
  return {
    el: h(
      'section.contract-wire',
      h(
        'header.contract-wire__head',
        h('div', h('h5.contract-wire__title', 'Les mêmes octets, lus deux fois'), h('p.contract-wire__sub', h('span.mono', wire.path), ' — survolez un champ pour retrouver ses octets.')),
        switcher,
      ),
      stage,
    ),
    destroy: release,
  };
}

function jsonWire(wire) {
  const blocks = [];
  const block = (side, who) => {
    const message = wire[side];
    if (!message) return h('div.contract-wire__void', icon('ban', { size: 14 }), `${SIDES[side]} : aucun message échangé.`);
    const code = CodeBlock({
      title: `${SIDES[side]} ${who} · ${fmtBytes(message.size, { exact: true })}`,
      language: 'json',
      code: JSON.stringify(message.document, null, 2),
      lineNumbers: false,
      maxHeight: 264,
    });
    blocks.push(code);
    return code;
  };
  return {
    el: h(
      'section.contract-wire',
      h(
        'header.contract-wire__head',
        h('div', h('h5.contract-wire__title', 'Les messages échangés'), h('p.contract-wire__sub', 'JSON-RPC transporte les noms en clair : la rupture se lit directement dans le texte.')),
      ),
      h('div.contract-wire__json', block('request', 'du client v1'), block('response', 'du serveur v2')),
    ),
    destroy: () => blocks.forEach((component) => component.destroy()),
  };
}

/**
 * Vue des octets échangés par un scénario.
 * @param {Object} result Résultat d'un scénario (`wire.format` vaut `protobuf` ou `json`).
 * @returns {{el: HTMLElement, destroy: () => void}|null} `null` si l'appel n'a pas été tracé.
 */
export function wireView(result) {
  const wire = result.wire;
  if (!wire) return null;
  return wire.format === 'protobuf' ? protobufWire(wire) : jsonWire(wire);
}
