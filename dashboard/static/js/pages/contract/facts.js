/**
 * Contrat & IDL — les deux sections « de lecture » : le cadrage (trois cartes de faits, chacune
 * étayée par une donnée tirée des deux contrats) et les règles d'or de l'évolution d'un contrat.
 */

import { h } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { Badge, Card, Section } from '../../components/ui.js';
import { plural, protoStats, wireExample } from './model.js';

const fileName = (path) => String(path ?? '').split('/').pop();

function fact({ index, iconName, title, text, proof }) {
  return h(
    'article.contract-fact',
    { style: { '--i': index } },
    h('header.contract-fact__head', h('span.contract-fact__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: 16 })), h('h3.contract-fact__title', title)),
    h('p.contract-fact__text', text),
    proof ? h('div.contract-fact__proof', proof) : null,
  );
}

/** Deux lignes : ce que protoc tire de chacun des deux fichiers. */
function generatedProof(overview) {
  const row = (version, proto) => {
    const stats = protoStats(proto?.source);
    return h(
      'div.contract-proof__row',
      h('span.contract-tag', { dataset: { version } }, version),
      h('span.contract-proof__file.mono.truncate', { title: proto?.path }, fileName(proto?.path)),
      h('span.contract-proof__figures.num', `${stats.rpcs} rpc · ${stats.messages} messages`),
    );
  };
  return [h('p.contract-proof__label.t-label', 'Ce que protoc compile'), row('v1', overview.proto_v1), row('v2', overview.proto_v2)];
}

/** Anatomie d'un champ réel du contrat : le nom reste dans le code, seul le tag voyage. */
function wireProof(example) {
  if (!example) return null;
  const row = (version, field) =>
    h(
      'div.contract-wireline',
      h('span.contract-tag', { dataset: { version } }, version),
      h(
        'code.contract-wireline__decl',
        h('span.contract-wireline__type', field.type),
        ' ',
        h('span.contract-wireline__name', { title: 'Le nom ne quitte jamais le code généré' }, field.name),
        ' = ',
        h('span.contract-wireline__number', String(field.number)),
        ';',
      ),
      h(
        'span.contract-wireline__wire',
        icon('arrow-right', { size: 12 }),
        'tag',
        h('code.contract-wireline__tag', { title: `(${field.number} << 3) | ${field.wire}` }, `0x${field.tagHex}`),
        h('span.num', `n°${field.number} · ${field.wireName}`),
      ),
    );
  return [
    h('p.contract-proof__label.t-label', `${example.change.scope || 'Champ'} — même numéro, autre tag`),
    row('v1', example.v1),
    row('v2', example.v2),
  ];
}

/** Décompte des changements et échelle des issues, de l'inoffensive à la pire. */
function breakingProof(overview, outcomes) {
  const stats = overview.stats ?? {};
  return [
    h(
      'div.contract-proof__counts',
      h('span.contract-proof__count', { dataset: { tone: 'danger' } }, h('strong.num', String(stats.breaking ?? 0)), stats.breaking > 1 ? ' cassants' : ' cassant'),
      h('span.contract-proof__count', { dataset: { tone: 'success' } }, h('strong.num', String(stats.compatible ?? 0)), stats.compatible > 1 ? ' compatibles' : ' compatible'),
      h('span.contract-proof__hint', 'entre v1 et v2'),
    ),
    h(
      'div.contract-scale',
      { role: 'list', 'aria-label': 'Issues possibles, de la moins à la plus dangereuse' },
      outcomes.list.map((outcome, index) =>
        h(
          'span.contract-scale__step',
          { role: 'listitem', title: outcome.text },
          index ? h('span.contract-scale__sep', { 'aria-hidden': 'true' }, icon('chevron-right', { size: 12 })) : null,
          Badge({ label: outcome.short, tone: outcome.tone, size: 'sm', variant: index === outcomes.list.length - 1 ? 'solid' : 'soft' }),
        ),
      ),
    ),
  ];
}

/**
 * Section de cadrage : « Le contrat, c'est le fichier .proto ».
 * @param {Object} overview Réponse de `GET /api/contract`.
 * @param {{list: Array<Object>}} outcomes Catalogue des issues (`outcomeCatalog`).
 * @returns {HTMLElement}
 */
export function introSection(overview, outcomes) {
  const example = wireExample(overview.changes);
  return Section(
    {
      title: 'Le contrat, c’est le fichier .proto',
      description: 'Client et serveur ne partagent ni code ni mémoire : seulement une description de l’interface, compilée de chaque côté.',
    },
    h(
      'div.contract-facts',
      fact({
        index: 0,
        iconName: 'file-code',
        title: 'Le stub est généré depuis le contrat',
        text: 'protoc lit le fichier .proto et en tire le stub du client et le squelette du serveur. Si le fichier évolue d’un seul côté, le code généré de l’autre reste figé sur l’ancienne version.',
        proof: generatedProof(overview),
      }),
      fact({
        index: 1,
        iconName: 'binary',
        title: 'Sur le fil : des numéros, jamais des noms',
        text: [
          'Chaque champ voyage précédé d’un tag qui encode son numéro et son type de fil. Le nom n’existe que dans le code : ',
          example
            ? `deux contrats peuvent appeler « ${example.v1.name} » et « ${example.v2.name} » le même numéro sans que rien ne proteste.`
            : 'deux contrats peuvent donner deux noms au même numéro sans que rien ne proteste.',
        ],
        proof: wireProof(example),
      }),
      fact({
        index: 2,
        iconName: 'shield-alert',
        title: '« Cassant » : l’ancien client se trompe',
        text: 'Un changement est cassant quand un client compilé avec l’ancien contrat échoue face au nouveau serveur — ou, pire, réussit avec des données fausses. L’erreur franche est la moins grave des ruptures.',
        proof: breakingProof(overview, outcomes),
      }),
    ),
  );
}

function ruleList(rules, good) {
  return Card(
    {
      title: good ? 'À faire' : 'À ne jamais faire',
      subtitle: plural(rules.length, 'règle'),
      icon: good ? 'circle-check' : 'ban',
      accent: good ? 'success' : 'danger',
      class: 'contract-rules',
    },
    h(
      'ol.contract-rules__list',
      { dataset: { tone: good ? 'success' : 'danger' } },
      rules.map((rule) =>
        h(
          'li.contract-rule',
          h('span.contract-rule__mark', { 'aria-hidden': 'true' }, icon(good ? 'check' : 'x', { size: 12, stroke: 2.5 })),
          h('div.contract-rule__body', h('p.contract-rule__title', rule.title), h('p.contract-rule__text', rule.text)),
        ),
      ),
    ),
  );
}

/**
 * Section « Règles d'or de l'évolution d'un contrat » : à ne jamais faire / à faire.
 * @param {Array<{title: string, good: boolean, text: string}>} rules `rules` de `GET /api/contract`.
 * @returns {HTMLElement}
 */
export function rulesSection(rules) {
  const list = rules ?? [];
  const dont = list.filter((rule) => !rule.good);
  const dos = list.filter((rule) => rule.good);
  return Section(
    {
      title: 'Règles d’or de l’évolution d’un contrat',
      description: 'Ce que les scénarios ci-dessus démontrent, résumé en réflexes de conception.',
    },
    h('div.contract-rules-grid', dont.length ? ruleList(dont, false) : null, dos.length ? ruleList(dos, true) : null),
  );
}
