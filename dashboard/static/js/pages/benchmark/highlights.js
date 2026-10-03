/**
 * Banc d'essai — « Ce que disent les mesures » : les faits marquants calculés par le serveur à
 * partir du rapport (`build_highlights`), présentés en cartes. Rien n'est réécrit ici : la
 * valeur, le détail et le ton viennent du rapport.
 */

import { h, clear } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { Button, EmptyState, Section, Skeleton } from '../../components/ui.js';

const TONE_ICONS = { success: 'circle-check', warning: 'triangle-alert', info: 'info', danger: 'circle-alert' };
const TONE_LABELS = { success: 'Avantage', warning: 'Coût', info: 'Constat', danger: 'Alerte' };

/** Sépare « 401 octets — gRPC / Protobuf » ou « 17,7 fois plus rapide » en nombre + précision. */
function splitValue(value) {
  const text = String(value ?? '');
  const match = text.match(/^([×+−-]?[\s  ]?\d[\d\s  .,]*(?:[\s  ]?%)?)(.*)$/u);
  if (!match) return { main: '', rest: text, tag: '' };
  const [rest, tag = ''] = match[2].trim().split(/\s+—\s+/u);
  // Même signe moins que les autres nombres de la page (« − », pas le trait d'union du rapport).
  return { main: match[1].trim().replace(/^-/, '−'), rest, tag };
}

function FactCard(fact, index) {
  const tone = TONE_ICONS[fact.tone] ? fact.tone : 'info';
  const { main, rest, tag } = splitValue(fact.value);
  const detail = h('span.bench-fact__detail', fact.detail);
  const el = h(
    'button.bench-fact',
    {
      type: 'button',
      dataset: { tone, fact: fact.id },
      style: { '--i': index },
      'aria-expanded': 'false',
      onClick: () => el.setExpanded(el.getAttribute('aria-expanded') !== 'true'),
    },
    h(
      'span.bench-fact__head',
      h('span.bench-fact__tone', { title: TONE_LABELS[tone] }, icon(TONE_ICONS[tone], { size: 13, stroke: 2 }), h('span.sr-only', TONE_LABELS[tone])),
      h('span.bench-fact__title', fact.title),
    ),
    h('span.bench-fact__value', main ? h('span.bench-fact__number.num', main) : null, rest ? h('span.bench-fact__unit', { class: { 'bench-fact__unit--solo': !main } }, rest) : null, tag ? h('span.bench-fact__tag', tag) : null),
    detail,
  );
  el.setExpanded = (on) => el.setAttribute('aria-expanded', String(Boolean(on)));
  return el;
}

function placeholders(count) {
  return Array.from({ length: count }, () =>
    h('div.bench-fact.bench-fact--loading', { 'aria-hidden': 'true' }, Skeleton({ width: '55%' }), Skeleton({ variant: 'block', width: '42%', height: 26 }), Skeleton({ lines: 2 })),
  );
}

/**
 * Section des faits marquants.
 * @returns {{el: HTMLElement, update: (view: Object|null, run: import('./model.js').RunState) => void}}
 */
export function createHighlights() {
  const grid = h('div.bench-facts', { 'aria-live': 'polite' });
  let expanded = false;
  const toggle = Button({
    label: 'Tout déplier',
    icon: 'list',
    variant: 'ghost',
    size: 'sm',
    onClick: () => {
      expanded = !expanded;
      for (const card of grid.querySelectorAll('button.bench-fact')) card.setExpanded(expanded);
      toggle.setLabel(expanded ? 'Tout replier' : 'Tout déplier');
    },
  });
  const el = Section(
    {
      id: 'bench-highlights',
      title: 'Ce que disent les mesures',
      description: 'Phrases calculées à partir de ce rapport : le protocole « le plus rapide » ou « le plus léger » est celui que cette exécution a mesuré comme tel.',
      actions: toggle,
    },
    grid,
  );
  let last;

  return {
    el,
    update(view, run) {
      const facts = view?.highlights ?? [];
      const signature = facts.length ? facts : run.running ? 'pending' : 'empty';
      if (signature === last) return;
      last = signature;
      toggle.hidden = !facts.length;
      if (facts.length) {
        clear(grid, facts.map(FactCard));
        if (expanded) for (const card of grid.querySelectorAll('button.bench-fact')) card.setExpanded(true);
      } else if (run.running) {
        clear(grid, placeholders(5), h('p.bench-facts__note', 'Les faits marquants sont calculés à la fin de la mesure, une fois toutes les suites terminées.'));
      } else {
        clear(grid, h('div.bench-facts__empty', EmptyState({ size: 'sm', icon: 'sparkles', title: 'Aucun fait marquant', text: 'Ce rapport ne contient pas assez de mesures pour en tirer une conclusion chiffrée.' })));
      }
    },
  };
}
