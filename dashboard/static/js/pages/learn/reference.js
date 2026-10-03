/**
 * Bilan — références : glossaire et correspondance avec le cahier des charges (où chaque phase
 * vit dans le code, dans l'application et en ligne de commande).
 */

import { h } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { Card, CopyButton } from '../../components/ui.js';
import { GLOSSARY, PHASES } from './content.js';
import { pageLink } from './data.js';

/**
 * Glossaire : liste de définitions sur deux colonnes.
 * @returns {HTMLElement}
 */
export function Glossary() {
  return Card(
    { padding: 'none', class: 'learn-glossary' },
    h(
      'dl.learn-glossary__list',
      GLOSSARY.map((entry) =>
        h(
          'div.learn-term',
          h('dt.learn-term__name', entry.term, entry.alias ? h('span.learn-term__alias', entry.alias) : null),
          h('dd.learn-term__definition', entry.definition),
        ),
      ),
    ),
  );
}

function PhaseRow(phase) {
  return h(
    'li.learn-phase',
    h(
      'div.learn-phase__intro',
      h('span.learn-phase__number.num', { 'aria-hidden': 'true' }, String(phase.number)),
      h('div', h('h3.learn-phase__title', h('span.sr-only', `Phase ${phase.number} : `), phase.title), h('p.learn-phase__requirement', phase.requirement)),
    ),
    h('div.learn-phase__cell', h('span.learn-phase__label.t-label', 'Dans le code'), h('ul.learn-phase__paths', phase.code.map((path) => h('li', h('code.learn-path', path))))),
    h(
      'div.learn-phase__cell',
      h('span.learn-phase__label.t-label', 'Dans l’application'),
      h(
        'ul.learn-phase__pages',
        phase.pages.map((page) => {
          const link = pageLink(page);
          return link ? h('li', h('a.learn-page-link', { href: link.href }, icon(link.icon, { size: 13 }), link.label)) : null;
        }),
      ),
    ),
    h(
      'div.learn-phase__cell',
      h('span.learn-phase__label.t-label', 'En ligne de commande'),
      h('ul.learn-phase__commands', phase.cli.map((command) => h('li.learn-command', h('code.learn-command__text', command), CopyButton({ text: command, label: 'Copier la commande' })))),
    ),
  );
}

/**
 * Correspondance avec le cahier des charges : les quatre phases du projet.
 * @returns {HTMLElement}
 */
export function Phases() {
  return Card(
    { padding: 'none', class: 'learn-phases' },
    h('div.learn-phases__head', { 'aria-hidden': 'true' }, ['Phase du sujet', 'Dans le code', 'Dans l’application', 'En ligne de commande'].map((label) => h('span.t-label', label))),
    h('ol.learn-phases__list', PHASES.map(PhaseRow)),
  );
}
