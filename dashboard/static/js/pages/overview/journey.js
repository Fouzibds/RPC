/**
 * Vue d'ensemble — « Parcours » : les quatre objectifs du sujet et les pages qui y répondent.
 */

import { h } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { href } from '../../core/router.js';
import { routeInfo } from '../../core/routes.js';
import { Section } from '../../components/ui.js';

const STEPS = [
  {
    verb: 'Comprendre',
    question: 'Que se passe-t-il entre l’appel et la réponse ?',
    text: 'Stub, marshalling, tramage, squelette, procédure : les douze étapes d’un appel, octet par octet.',
    pages: ['xray'],
  },
  {
    verb: 'Comparer',
    question: 'Combien pèse un message, combien coûte un appel ?',
    text: 'JSON face à Protobuf, local face à distant ; puis le même appel écrit de quatre façons.',
    pages: ['benchmark', 'compare'],
  },
  {
    verb: 'Éprouver',
    question: 'Que devient l’appel quand le réseau ou le contrat lâche ?',
    text: 'Latence, coupures, pannes et nouvelles tentatives ; puis un contrat qui évolue sans prévenir ses clients.',
    pages: ['chaos', 'contract'],
  },
  {
    verb: 'Conclure',
    question: 'Quel middleware choisir, et pourquoi ?',
    text: 'Avantages et limites de chaque approche, chiffrés par les mesures de votre propre laboratoire.',
    pages: ['learn'],
  },
];

function pageLink(id) {
  const route = routeInfo(id);
  return h(
    'a.ov-step__link',
    { href: href(id) },
    h('span.ov-step__link-icon', { 'aria-hidden': 'true' }, icon(route.icon, { size: 15 })),
    h('span.ov-step__link-label', route.label),
    h('span.ov-step__link-arrow', { 'aria-hidden': 'true' }, icon('arrow-right', { size: 14 })),
  );
}

/**
 * Section « Parcours » : quatre étapes numérotées, chacune renvoyant vers ses pages.
 * @returns {HTMLElement}
 */
export function createJourney() {
  return Section(
    { title: 'Parcours', description: 'Quatre étapes pour répondre aux objectifs du sujet, dans l’ordre où elles se construisent.' },
    h(
      'ol.ov-steps',
      STEPS.map((step, index) =>
        h(
          'li.ov-step.card',
          { style: { '--i': index + 8 } },
          h('div.ov-step__head', h('span.ov-step__number.num', { 'aria-hidden': 'true' }, String(index + 1)), h('h3.ov-step__verb', step.verb)),
          h('p.ov-step__question', step.question),
          h('p.ov-step__text', step.text),
          h('div.ov-step__links', step.pages.map(pageLink)),
        ),
      ),
    ),
  );
}
