/**
 * Contrat & IDL — panneau de résultat d'un scénario : le verdict (issue, explication), les trois
 * colonnes « envoyé / attendu / observé » avec les écarts relevés par le laboratoire, puis les
 * octets échangés.
 */

import { h } from '../../core/dom.js';
import { fmtMs } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { Badge, CopyButton } from '../../components/ui.js';
import { flatten, literal } from './model.js';
import { wireView } from './wire.js';

function column({ title, subtitle, iconName, tone }, ...children) {
  return h(
    'section.contract-col',
    { dataset: { tone } },
    h(
      'header.contract-col__head',
      h('span.contract-col__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: 14 })),
      h('div.contract-col__heading', h('h5.contract-col__title', title), subtitle ? h('p.contract-col__sub', subtitle) : null),
    ),
    children,
  );
}

const label = (text) => h('p.contract-col__label.t-label', text);

/** Texte de code avec des points de coupure après `(`, `,`, `/` et `.` : jamais de mot scindé au hasard. */
function breakable(text) {
  return String(text ?? '')
    .split(/(?<=[(,/.])/)
    .flatMap((part, index) => (index ? [h('wbr'), part] : [part]));
}

/**
 * Tableau « chemin → valeur ». `marks` signale les valeurs qui s'écartent de la référence : elles
 * prennent le ton de l'issue (rouge pour une corruption), la valeur attendue est rappelée à côté.
 */
function values(data, { marks = new Map(), tone, rootName = 'résultat' } = {}) {
  const rows = flatten(data);
  if (!rows.length) return h('p.contract-col__none', 'Aucune donnée concernée.');
  return h(
    'dl.contract-vals',
    { dataset: { tone } },
    rows.map(([path, value]) => {
      const mark = marks.get(path);
      return h(
        'div.contract-vals__row',
        { class: { 'is-wrong': Boolean(mark) } },
        h('dt.contract-vals__key.mono', path || rootName),
        h(
          'dd.contract-vals__val',
          h('span.contract-vals__lit.mono', literal(value)),
          mark ? h('span.contract-vals__expected', 'attendu ', h('span.mono', literal(mark.expected))) : null,
        ),
      );
    }),
  );
}

function sentColumn(result) {
  const sent = result.sent ?? {};
  return column(
    { title: 'Ce que le client v1 envoie', subtitle: 'Stub généré depuis le contrat v1', iconName: 'send' },
    h('pre.contract-call', breakable(sent.call)),
    label('Destination'),
    h('p.contract-col__mono.mono', sent.target ? breakable(sent.target) : '—'),
    label('Arguments'),
    values(sent.params, { rootName: 'argument' }),
  );
}

function expectedColumn(result) {
  const expected = result.expected ?? {};
  const state = expected.server_state ?? {};
  return column(
    { title: 'Ce qu’il attend', subtitle: 'La réponse d’un serveur v1, rejouée en référence', iconName: 'target' },
    h('div.contract-col__status', Badge({ label: expected.status ?? 'OK', tone: 'success', mono: true }), h('span', 'succès')),
    h('p.contract-col__summary', expected.summary ?? ''),
    label('Ce que le client lirait'),
    values(expected.client_view),
    Object.keys(state).length ? [label('État du serveur après l’appel'), values(state)] : null,
  );
}

function observedColumn(result, outcome) {
  const observed = result.observed ?? {};
  const differences = observed.differences ?? [];
  const marksFor = (where) => new Map(differences.filter((difference) => difference.where === where).map((difference) => [difference.path, difference]));
  const truth = observed.server_truth ?? {};
  const failed = observed.ok === false;
  const statusNote = failed ? 'l’appel échoue' : observed.client_crash ? 'puis le client plante' : differences.length ? 'aucune erreur signalée' : 'succès';

  let read;
  if (failed) read = h('p.contract-col__none', 'Rien : l’appel a été rejeté avant toute réponse.');
  else if (observed.client_view === null || observed.client_view === undefined) read = h('p.contract-col__none', 'Rien d’exploitable : le code du client s’est arrêté en lisant la réponse.');
  else read = values(observed.client_view, { marks: marksFor('client'), tone: outcome.tone });

  return column(
    { title: 'Ce qui se passe réellement', subtitle: 'Le serveur « contrat v2 » du laboratoire', iconName: 'activity', tone: outcome.tone },
    h('div.contract-col__status', Badge({ label: observed.status ?? result.status ?? '—', tone: failed ? outcome.tone : 'success', mono: true }), h('span', statusNote)),
    observed.error || observed.client_crash ? null : h('p.contract-col__summary', observed.summary ?? ''),
    observed.error
      ? h(
          'div.contract-fault',
          { dataset: { tone: outcome.tone } },
          h('span.contract-fault__code.mono', observed.error.code ?? observed.error.type ?? 'ERREUR'),
          h('span.contract-fault__text', observed.error.message ?? ''),
        )
      : null,
    observed.client_crash
      ? h(
          'div.contract-fault',
          { dataset: { tone: outcome.tone } },
          h('span.contract-fault__code.mono', observed.client_crash.type ?? 'Exception'),
          h('span.contract-fault__text', 'levée dans le code du client : ', h('span.mono', observed.client_crash.message ?? '')),
        )
      : null,
    label('Ce que le client lit'),
    read,
    Object.keys(truth).length ? [label('Vérité côté serveur'), values(truth, { marks: marksFor('server'), tone: outcome.tone })] : null,
  );
}

function verdict(result, outcome) {
  const expected = result.matches !== false;
  return h(
    'div.contract-verdict',
    { dataset: { tone: outcome.tone, outcome: outcome.id } },
    h('span.contract-verdict__icon', { 'aria-hidden': 'true' }, icon(outcome.icon, { size: 20 })),
    h(
      'div.contract-verdict__body',
      h('div.contract-verdict__line', h('strong.contract-verdict__label', outcome.short), h('span.contract-verdict__meaning', outcome.text)),
      h('p.contract-verdict__why', result.explanation ?? ''),
    ),
    h(
      'dl.contract-verdict__facts',
      h('div', h('dt', 'Statut'), h('dd.mono', String(result.status ?? '—'))),
      h('div', h('dt', 'Durée'), h('dd.num', fmtMs(result.duration_ms))),
      result.call_id ? h('div', h('dt', 'Appel'), h('dd.mono', result.call_id, CopyButton({ text: result.call_id, label: 'Copier l’identifiant de l’appel' }))) : null,
      h(
        'div',
        h('dt', 'Prévision'),
        h('dd', { class: expected ? 'fg-1' : 'fg-warning' }, icon(expected ? 'check' : 'triangle-alert', { size: 12 }), expected ? 'conforme' : 'inattendue'),
      ),
    ),
  );
}

/**
 * Panneau de résultat d'un scénario joué.
 * @param {Object} result Un élément de `results` (`POST /api/contract/run`).
 * @param {{get: (id: string) => Object}} outcomes Catalogue des issues (`outcomeCatalog`).
 * @returns {{el: HTMLElement, destroy: () => void}}
 */
export function resultPanel(result, outcomes) {
  const outcome = outcomes.get(result.outcome);
  const wire = wireView(result);
  const el = h(
    'div.contract-result',
    { dataset: { tone: outcome.tone, outcome: outcome.id } },
    verdict(result, outcome),
    h('div.contract-cols', sentColumn(result), expectedColumn(result), observedColumn(result, outcome)),
    wire?.el ?? null,
  );
  return { el, destroy: () => wire?.destroy() };
}
