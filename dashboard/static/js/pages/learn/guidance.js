/**
 * Bilan — conseils : « Quand choisir quoi » (cartes de décision), les règles tirées du
 * laboratoire de pannes et les huit illusions de l'informatique répartie.
 */

import { h } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { Card, Col, Grid, ProtocolChip } from '../../components/ui.js';
import { DECISIONS, FALLACIES, RULES } from './content.js';
import { findItem, formatMetric, pageLink } from './data.js';

function figure(metric) {
  const { prefix, value, unit } = formatMetric(metric);
  return [prefix ? h('span.learn-figure__prefix', prefix) : null, h('span.num', value), unit ? h('span.learn-figure__unit', unit) : null];
}

/** Ligne de preuve d'une carte de décision : un chiffre du bilan, ou une case de la matrice. */
function proofLine(proof, summary) {
  if (!summary) return null;
  if (proof.item) {
    const item = findItem(summary, proof.item);
    if (!item?.metric) return h('p.learn-decision__proof.is-pending', h('span.learn-decision__proof-value.num', '—'), h('span.learn-decision__proof-label', 'Chiffre à mesurer'));
    return h('p.learn-decision__proof', h('span.learn-decision__proof-value', figure(item.metric)), h('span.learn-decision__proof-label', item.metric.label));
  }
  const row = (summary.comparison ?? []).find((entry) => entry.criterion === proof.criterion);
  const value = row?.[proof.protocol];
  if (!value) return null;
  return h('p.learn-decision__proof', h('span.learn-decision__proof-value.learn-decision__proof-value--text', value), h('span.learn-decision__proof-label', row.criterion));
}

function DecisionCard(decision, summary, index) {
  const info = decision.protocol ? protocol(decision.protocol) : null;
  const card = Card(
    { padding: 'none', class: 'learn-decision', protocol: decision.protocol ?? undefined, accent: decision.protocol ? undefined : 'danger' },
    h(
      'div.learn-decision__body',
      h('span.learn-decision__icon', { 'aria-hidden': 'true' }, icon(decision.icon, { size: 16 })),
      h('h3.learn-decision__situation', decision.situation),
      h(
        'p.learn-decision__choice',
        icon('arrow-right', { size: 13 }),
        info ? ProtocolChip(decision.protocol) : h('span.learn-decision__verdict', decision.verdict),
      ),
      h('p.learn-decision__reasoning', decision.reasoning),
    ),
    proofLine(decision.proof, summary),
  );
  card.style.setProperty('--i', String(index));
  return card;
}

/**
 * « Quand choisir quoi » : quatre cartes de décision, appuyées sur les chiffres du bilan quand ils existent.
 * @param {{summary: Object|null}} props `summary` absent : les cartes s'affichent sans ligne de preuve.
 * @returns {HTMLElement}
 */
export function Decisions({ summary }) {
  return Grid(DECISIONS.map((decision, index) => Col({ span: 3, md: 6 }, DecisionCard(decision, summary, index))));
}

function RuleRow(rule, summary) {
  const item = summary ? findItem(summary, rule.item) : null;
  const observed = Boolean(item?.source && item.source !== 'code' && item.metric);
  const link = pageLink(rule.page, rule.scenario ? { scenario: rule.scenario } : undefined);
  return h(
    'li.learn-rule',
    { dataset: { state: observed ? 'observed' : 'pending' } },
    h('span.learn-rule__check', { 'aria-hidden': 'true' }, icon(observed ? 'check' : 'minus', { size: 12, stroke: 2.5 })),
    h(
      'div.learn-rule__text',
      h('p.learn-rule__title', rule.title),
      h('p.learn-rule__reason', rule.text),
      link ? h('a.learn-link.learn-link--quiet.learn-noprint', { href: link.href }, h('span', `${observed ? 'Rejouer' : 'Observer'} dans ${link.label}`), icon('arrow-right', { size: 12 })) : null,
    ),
    item?.metric
      ? h('div.learn-rule__fact', h('p.learn-rule__value', figure(item.metric)), h('p.learn-rule__label', item.metric.label))
      : h('div.learn-rule__fact.is-pending', h('p.learn-rule__value.num', '—'), h('p.learn-rule__label', summary ? 'Pas encore observé' : 'Bilan indisponible')),
  );
}

/**
 * Les règles à suivre, chacune avec ce que le laboratoire a observé.
 * @param {{summary: Object|null}} props
 * @returns {HTMLElement}
 */
export function Rules({ summary }) {
  const observed = RULES.filter((rule) => {
    const item = summary ? findItem(summary, rule.item) : null;
    return item?.source && item.source !== 'code' && item.metric;
  }).length;
  return Card(
    {
      title: 'Six règles pour un appel distant',
      subtitle: summary ? `${observed} sur ${RULES.length} déjà observées dans ce laboratoire` : 'Tirées du laboratoire de pannes et du contrat',
      icon: 'shield-check',
      padding: 'none',
      class: 'learn-rules',
    },
    h('ul.learn-rules__list', RULES.map((rule) => RuleRow(rule, summary))),
  );
}

/**
 * Les huit illusions de l'informatique répartie, et ce qui les contredit dans ce laboratoire.
 * @returns {HTMLElement}
 */
export function Fallacies() {
  return Card(
    {
      title: 'Les huit illusions de l’informatique répartie',
      subtitle: 'L. Peter Deutsch et James Gosling, Sun Microsystems — toutes fausses',
      icon: 'eye-off',
      padding: 'none',
      class: 'learn-fallacies',
    },
    h(
      'ol.learn-fallacies__list',
      FALLACIES.map((fallacy, index) => {
        const link = pageLink(fallacy.page);
        return h(
          'li.learn-fallacy',
          h('span.learn-fallacy__number.num', { 'aria-hidden': 'true' }, String(index + 1)),
          h(
            'div.learn-fallacy__text',
            h('p.learn-fallacy__claim', `« ${fallacy.claim} »`),
            h('p.learn-fallacy__reality', fallacy.reality, link ? [' ', h('a.learn-fallacy__link.learn-noprint', { href: link.href, 'aria-label': `Voir ${link.label}` }, link.label, icon('arrow-up-right', { size: 11 }))] : null),
          ),
        );
      }),
    ),
  );
}
