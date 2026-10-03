/**
 * Transparence — la comparaison : une carte par écriture (source réelle, ligne d'intention
 * surlignée, lignes utiles, préoccupations à la charge de l'appelant) et la variante navigateur
 * `fetch()` dans un bloc repliable.
 */

import { h, qsa, uid } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { CodeBlock } from '../../components/codeblock.js';
import { Badge, Card, Chip, Section } from '../../components/ui.js';
import { ROLES, fetchSummary, intentRange } from './model.js';

/** Retrait supplémentaire (en caractères) des lignes de code repliées par le retour automatique. */
const HANG = 4;

function linesMetric(count) {
  return h(
    'div.cmp-lines',
    { title: 'Lignes utiles : ni vides, ni commentaires, ni docstring' },
    h('span.cmp-lines__value.num', String(count)),
    h('span.cmp-lines__unit', count > 1 ? 'lignes utiles' : 'ligne utile'),
  );
}

function protoStyle(info) {
  return { '--proto': info.color, '--proto-fg': info.fg, '--proto-soft': info.soft, '--proto-line': info.line };
}

/**
 * Bloc de code d'une écriture. `CodeBlock` replie les longues lignes au bord gauche : on donne ici
 * à chaque ligne un retrait suspendu (son indentation + `HANG`), pour que la suite d'une ligne ne
 * se confonde pas avec la ligne suivante.
 */
function sourceBlock(title, language, code, method) {
  const range = intentRange(code, method);
  const block = CodeBlock({ title, language, code, highlightLines: range ? [range] : [], wrap: true });
  const lines = String(code).split('\n');
  qsa('.code__text', block).forEach((text, index) => {
    const indent = (lines[index] ?? '').match(/^ */)[0].length + HANG;
    text.style.paddingLeft = `${indent}ch`;
    text.style.textIndent = `-${indent}ch`;
  });
  return block;
}

function concernsBlock(concerns, color, relief) {
  const count = concerns.length;
  return h(
    'div.cmp-concerns',
    h('div.cmp-concerns__head', h('span.t-label', 'À la charge de l’appelant'), Badge({ label: String(count), tone: count ? 'warning' : 'success', size: 'sm', mono: true })),
    count
      ? h('ul.cmp-chips', concerns.map((concern) => h('li', Chip({ label: concern, color }))))
      : h('p.cmp-concerns__none', icon('check', { size: 14, stroke: 2 }), h('span', relief)),
  );
}

function variantCard(snippet, method, blocks) {
  const info = protocol(snippet.id);
  const code = sourceBlock(`${snippet.function}()`, snippet.language, snippet.code, method);
  blocks.push(code);
  const card = Card(
    {
      title: snippet.title,
      subtitle: ROLES[snippet.id]?.label,
      icon: info.icon,
      protocol: snippet.id,
      padding: 'none',
      actions: linesMetric(snippet.lines),
      class: 'cmp-variant',
    },
    code,
    h(
      'div.cmp-variant__meta',
      h('div.cmp-setup', h('span.t-label', 'Préalable'), h('span.cmp-setup__code.mono', snippet.setup)),
      concernsBlock(snippet.concerns, info.color, ROLES[snippet.id]?.relief ?? 'Rien à gérer.'),
      h('p.cmp-variant__note', snippet.note),
    ),
  );
  Object.entries(protoStyle(info)).forEach(([name, value]) => card.style.setProperty(name, value));
  return card;
}

function fetchBlock(script, reference, method, blocks) {
  const info = protocol('rest');
  const panelId = uid('cmp-fetch');
  const code = sourceBlock('updateStock()', script.language, script.code, method);
  blocks.push(code);

  const panel = h(
    'div.cmp-fetch__panel',
    { id: panelId },
    code,
    h(
      'div.cmp-variant__meta',
      concernsBlock(script.concerns, info.color, 'Rien à gérer.'),
      h(
        'p.cmp-variant__note',
        'Le navigateur n’a pas de stub : la page écrit elle-même l’URL, le verbe, les en-têtes, le JSON et le test du statut — comme ce tableau de bord le fait pour parler au laboratoire.',
      ),
    ),
  );
  const toggle = h(
    'button.cmp-fetch__toggle',
    {
      type: 'button',
      'aria-expanded': 'true',
      'aria-controls': panelId,
      onClick: () => {
        const open = panel.hidden;
        panel.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
        el.dataset.open = String(open);
      },
    },
    h('span.cmp-fetch__chevron', { 'aria-hidden': 'true' }, icon('chevron-right', { size: 16 })),
    h(
      'span.cmp-fetch__heading',
      h('span.cmp-fetch__title', script.title, Badge({ label: 'JavaScript', size: 'sm', variant: 'outline' })),
      h('span.cmp-fetch__summary', fetchSummary(script, reference)),
    ),
    linesMetric(script.lines),
  );
  const el = h('div.cmp-fetch', { dataset: { open: 'true' }, style: protoStyle(info) }, toggle, panel);
  return el;
}

/**
 * Section « La comparaison » : les écritures deux par deux, puis la variante `fetch()` (repliable).
 * L'écriture la plus longue s'étend sur deux rangées, ce qui laisse à `fetch()` la place voisine.
 * @param {Object} data Réponse de `GET /api/code-compare`.
 * @param {Object} env Environnement de la page (`track` enregistre un nettoyage).
 * @returns {HTMLElement}
 */
export function snippetsSection(data, env) {
  const blocks = [];
  const snippets = data.snippets;
  const height = (snippet) => String(snippet.code).split('\n').length;
  const tallest = snippets.reduce((best, snippet) => (height(snippet) > height(best) ? snippet : best), snippets[0]);
  const script = data.javascript_fetch;
  const cards = snippets.map((snippet) => {
    const card = variantCard(snippet, data.method, blocks);
    if (snippet === tallest && script && snippets.length > 1) card.classList.add('cmp-variant--tall');
    return card;
  });
  env.track(() => blocks.forEach((block) => block.destroy()));

  return Section(
    {
      title: 'La comparaison',
      description: 'Le code affiché est celui que le laboratoire exécute. Dans chaque écriture, une seule ligne dit ce que l’on veut faire ; tout le reste est du protocole.',
      actions: h('span.cmp-legend', h('span.cmp-legend__mark', { 'aria-hidden': 'true' }), 'Ligne qui porte l’intention métier'),
      id: 'compare-code',
    },
    h('div.cmp-variants', cards, script ? fetchBlock(script, snippets.find((snippet) => snippet.id === 'rest'), data.method, blocks) : null),
  );
}
