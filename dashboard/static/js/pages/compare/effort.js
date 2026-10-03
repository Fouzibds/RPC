/**
 * Transparence — « Ce que le développeur écrit à la main » : lignes de code et préoccupations
 * par écriture (graphiques), puis la matrice « qui s'en occupe » avec ses phrases calculées.
 */

import { h } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { BarChart } from '../../components/charts.js';
import { Card, Col, Grid, ProtocolChip, Section } from '../../components/ui.js';
import { concernMatrix, concernsSentence, linesSentence, plural, shortTitle } from './model.js';

const CELL = {
  hand: { icon: 'pencil', text: 'Écrit par l’appelant' },
  middleware: { icon: 'check', text: 'Pris en charge par le stub' },
  na: { icon: 'minus', text: 'Sans objet' },
};

function insight(text) {
  return h('p.cmp-insight', h('span.cmp-insight__icon', { 'aria-hidden': 'true' }, icon('sparkles', { size: 14 })), h('span', text));
}

function bars(snippets, script, value, seriesLabel, unit) {
  const data = snippets.map((snippet) => ({ id: snippet.id, label: snippet.title, short: shortTitle(snippet), protocol: snippet.id, value: value(snippet) }));
  if (script) {
    data.push({
      id: 'fetch',
      label: script.title,
      short: 'fetch()',
      color: 'color-mix(in srgb, var(--proto-rest) 46%, transparent)',
      value: value(script),
      hint: 'Variante navigateur, sans stub',
    });
  }
  return BarChart({
    data,
    orientation: 'horizontal',
    rowHeight: 34,
    barSize: 10,
    format: (count) => String(count),
    seriesLabel,
    ariaLabel: `${seriesLabel} par écriture${unit ? ` (${unit})` : ''}`,
  });
}

function matrixTable(snippets, rows) {
  const head = h(
    'tr',
    h('th.cmp-matrix__corner', { scope: 'col' }, 'Préoccupation'),
    snippets.map((snippet) => h('th.cmp-matrix__col', { scope: 'col', title: snippet.title }, ProtocolChip(snippet.id, { short: true, size: 'sm', variant: 'plain' }))),
  );
  const body = rows.map((row) =>
    h(
      'tr',
      h('th.cmp-matrix__concern', { scope: 'row' }, row.concern),
      row.cells.map((cell) => {
        const info = protocol(cell.id);
        const spec = CELL[cell.state];
        return h(
          'td.cmp-matrix__cell',
          { dataset: { state: cell.state }, style: { '--proto': info.color, '--proto-fg': info.fg, '--proto-soft': info.soft, '--proto-line': info.line }, title: spec.text },
          h('span.cmp-matrix__mark', { 'aria-hidden': 'true' }, icon(spec.icon, { size: 13, stroke: 2 })),
          h('span.sr-only', spec.text),
        );
      }),
    ),
  );
  const totals = h(
    'tr.cmp-matrix__totals',
    h('th', { scope: 'row' }, 'Écrites par l’appelant'),
    snippets.map((snippet) => h('td.num', String(snippet.concerns.length))),
  );
  return h(
    'div.cmp-matrix__scroll.scroll-x',
    h('table.cmp-matrix', h('caption.sr-only', 'Préoccupations de protocole : qui s’en occupe, pour chaque écriture'), h('thead', head), h('tbody', body), h('tfoot', totals)),
  );
}

function legend() {
  return h(
    'ul.cmp-matrix__legend',
    Object.entries(CELL).map(([state, spec]) => h('li', h('span.cmp-matrix__mark', { dataset: { state }, 'aria-hidden': 'true' }, icon(spec.icon, { size: 12, stroke: 2 })), spec.text)),
  );
}

/**
 * Section « Ce que le développeur écrit à la main ».
 * @param {Object} data Réponse de `GET /api/code-compare`.
 * @param {Object} env Environnement de la page (`track` enregistre un nettoyage).
 * @returns {HTMLElement}
 */
export function effortSection(data, env) {
  const snippets = data.snippets;
  const script = data.javascript_fetch;
  const rows = concernMatrix(snippets);
  const linesChart = bars(snippets, script, (item) => item.lines, 'Lignes utiles', 'lignes');
  const concernsChart = bars(snippets, script, (item) => item.concerns.length, 'Préoccupations', 'à la charge de l’appelant');
  env.track(() => {
    linesChart.destroy();
    concernsChart.destroy();
  });

  return Section(
    {
      title: 'Ce que le développeur écrit à la main',
      description: 'Le travail ne disparaît pas : il change de mains. Un stub le prend en charge une fois pour toutes ; sans stub, il revient à chaque appelant, pour chaque route.',
      id: 'compare-effort',
    },
    Grid(
      { align: 'stretch' },
      Col(
        { span: 5, md: 12 },
        Card(
          { title: 'Volume de code', subtitle: 'Lignes utiles : ni vides, ni commentaires, ni docstring', icon: 'ruler', class: 'cmp-volume' },
          h('div.cmp-chart', h('span.t-label', 'Lignes utiles'), linesChart),
          h('div.cmp-chart', h('span.t-label', 'Préoccupations à la charge de l’appelant'), concernsChart),
          insight(linesSentence(snippets)),
        ),
      ),
      Col(
        { span: 7, md: 12 },
        Card(
          {
            title: 'Qui s’en occupe ?',
            subtitle: `${plural(rows.length, 'préoccupation')} de protocole, écriture par écriture`,
            icon: 'workflow',
            class: 'cmp-who',
          },
          rows.length ? matrixTable(snippets, rows) : null,
          rows.length ? legend() : null,
          insight(concernsSentence(snippets, rows.length)),
        ),
      ),
    ),
  );
}
