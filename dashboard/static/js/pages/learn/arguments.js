/**
 * Bilan — « Avantages » et « Inconvénients » : deux colonnes de cartes, chacune avec son texte,
 * son chiffre mesuré, sa preuve et le lien vers la page où la reproduire. La colonne la plus
 * courte se termine par une carte de lecture (légende des états, couverture des mesures).
 */

import { h } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { Badge, Card, Skeleton } from '../../components/ui.js';
import { SOURCE_LABELS, coverage, findItem, formatMetric, stateOf, targetOf } from './data.js';

const KINDS = Object.freeze({
  advantages: { title: 'Avantages', icon: 'trending-up', tone: 'success', lead: 'Ce que le RPC apporte' },
  drawbacks: { title: 'Inconvénients', icon: 'triangle-alert', tone: 'danger', lead: 'Ce qu’il coûte, et ce qu’il cache' },
});

const LINK_VERBS = Object.freeze({ measured: 'Reproduire dans', pending: 'Mesurer dans', qualitative: 'Observer dans' });
const STATE_LABELS = Object.freeze({ measured: 'chiffré', pending: 'à mesurer', qualitative: 'constat' });

function metricBlock(item, state) {
  if (state === 'qualitative') return null;
  if (state === 'pending') {
    return h('div.learn-arg__metric', h('p.learn-arg__figure', h('span.learn-arg__value.num', '—')), h('p.learn-arg__label', 'Pas encore mesuré'));
  }
  const { prefix, value, unit } = formatMetric(item.metric);
  return h(
    'div.learn-arg__metric',
    h('p.learn-arg__figure', prefix ? h('span.learn-arg__prefix', prefix) : null, h('span.learn-arg__value.num', value), unit ? h('span.learn-arg__unit', unit) : null),
    h('p.learn-arg__label', item.metric.label),
  );
}

function provenance(item, state) {
  if (state === 'pending') return Badge({ label: 'À mesurer', variant: 'outline', size: 'sm' });
  if (state === 'qualitative') return Badge({ label: 'Constat', size: 'sm' });
  return Badge({ label: SOURCE_LABELS[item.source] ?? 'Mesuré', tone: 'success', dot: true, size: 'sm' });
}

function ArgumentCard(item, index, fresh) {
  const state = stateOf(item);
  const link = targetOf(item);
  const card = Card(
    { padding: 'none', class: 'learn-arg' },
    h('div.learn-arg__main', h('div.learn-arg__copy', h('h4.learn-arg__title', item.title), h('p.learn-arg__text', item.text)), metricBlock(item, state)),
    h('div.learn-arg__proof', h('span.learn-arg__proof-label.t-label', state === 'pending' ? 'À faire' : 'Preuve'), h('p.learn-arg__evidence', item.evidence)),
    h(
      'footer.learn-arg__foot',
      provenance(item, state),
      link
        ? h('a.learn-link.learn-noprint', { href: link.href }, icon(link.icon, { size: 13 }), h('span', h('span.learn-link__verb', `${LINK_VERBS[state]} `), link.label), icon('arrow-right', { size: 13 }))
        : null,
    ),
  );
  card.dataset.state = state;
  card.classList.toggle('is-fresh', fresh);
  card.style.setProperty('--i', String(index));
  return card;
}

/** Carte de lecture : ce que signifient les pastilles, et où en est la couverture des mesures. */
function ReadingCard(summary) {
  const legend = [
    [Badge({ label: 'Mesuré', tone: 'success', dot: true, size: 'sm' }), 'Chiffre produit par une expérience de ce laboratoire ; la pastille nomme laquelle.'],
    [Badge({ label: 'À mesurer', variant: 'outline', size: 'sm' }), 'Expérience pas encore jouée : la carte dit laquelle lancer.'],
    [Badge({ label: 'Constat', size: 'sm' }), 'Argument qualitatif, à observer plutôt qu’à chiffrer.'],
  ];
  const track = (kind) => {
    const items = summary[kind];
    const { measured, total } = coverage(items);
    return h(
      'div.learn-reading__row',
      { dataset: { tone: KINDS[kind].tone } },
      h('span.learn-reading__kind', KINDS[kind].title),
      h(
        'span.learn-reading__track',
        { role: 'img', 'aria-label': `${measured} sur ${total} chiffrés` },
        items.map((item) => h('span.learn-reading__segment', { dataset: { state: stateOf(item) }, title: `${item.title} — ${STATE_LABELS[stateOf(item)]}` })),
      ),
      h('span.learn-reading__count.num', `${measured}/${total}`),
    );
  };
  return Card(
    { padding: 'none', class: 'learn-reading' },
    h(
      'div.learn-reading__body',
      h('div.learn-reading__block', h('h4.learn-reading__title', 'Lire ce bilan'), h('ul.learn-reading__legend', legend.map(([badge, text]) => h('li', badge, h('span', text))))),
      h('div.learn-reading__block', h('h4.learn-reading__title', 'Couverture des mesures'), track('advantages'), track('drawbacks')),
      h(
        'p.learn-reading__closing',
        'Les avantages tiennent tant que le réseau se fait oublier ; les inconvénients apparaissent dès qu’il se rappelle à vous. D’où les règles plus bas.',
      ),
    ),
  );
}

function Column(kind, summary, previous, closing) {
  const info = KINDS[kind];
  const items = summary[kind];
  const { measured, total } = coverage(items);
  return h(
    'div.learn-column',
    { dataset: { tone: info.tone, kind } },
    h(
      'header.learn-column__head',
      h('span.learn-column__icon', { 'aria-hidden': 'true' }, icon(info.icon, { size: 16 })),
      h('div.learn-column__heading', h('h3.learn-column__title', info.title, h('span.learn-column__count.num', String(total))), h('p.learn-column__lead', info.lead)),
      h('span.learn-column__coverage', h('span.num', `${measured}/${total}`), measured > 1 ? ' chiffrés' : ' chiffré'),
    ),
    items.map((item, index) => {
      const before = previous ? findItem(previous, item.id) : null;
      const fresh = Boolean(previous && item.metric && before?.metric?.value !== item.metric.value);
      return ArgumentCard(item, index, fresh);
    }),
    closing ? ReadingCard(summary) : null,
  );
}

/**
 * Les deux colonnes du bilan.
 * @param {Object} props
 * @param {Object} props.summary `GET /api/summary`.
 * @param {Object|null} [props.previous] Bilan affiché juste avant : les chiffres qui ont changé sont mis en avant.
 * @returns {HTMLElement}
 */
export function ArgumentColumns({ summary, previous = null }) {
  const shorter = summary.advantages.length <= summary.drawbacks.length ? 'advantages' : 'drawbacks';
  return h(
    'div.learn-columns',
    Column('advantages', summary, previous, shorter === 'advantages'),
    Column('drawbacks', summary, previous, shorter === 'drawbacks'),
  );
}

/**
 * Squelette de chargement des deux colonnes.
 * @returns {HTMLElement}
 */
export function ArgumentsSkeleton() {
  const card = () =>
    Card(
      { padding: 'none', class: 'learn-arg learn-arg--skeleton' },
      h('div.learn-arg__main', h('div.learn-arg__copy', Skeleton({ width: '46%', height: 16 }), Skeleton({ lines: 3 })), h('div.learn-arg__metric', Skeleton({ variant: 'block', width: 96, height: 30 }))),
      h('div.learn-arg__proof', Skeleton({ lines: 2 })),
    );
  const column = () => h('div.learn-column', h('header.learn-column__head', Skeleton({ variant: 'block', width: 32, height: 32 }), Skeleton({ width: 140, height: 16 })), card(), card(), card());
  return h('div.learn-columns', { 'aria-hidden': 'true' }, column(), column());
}
