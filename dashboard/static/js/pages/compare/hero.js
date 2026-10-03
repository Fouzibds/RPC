/**
 * Transparence — bandeau « opération comparée » : la phrase métier, l'appel qui la réalise,
 * le produit visé et son stock en direct.
 */

import { h, clear } from '../../core/dom.js';
import { fmtNumber } from '../../core/format.js';
import { CountUp, ProtocolChip } from '../../components/ui.js';

function cell(label, ...content) {
  return h('div.cmp-op__cell', h('span.t-label', label), content);
}

/**
 * Bandeau de l'opération comparée.
 * @param {Object} data Réponse de `GET /api/code-compare`.
 * @param {Object} env Environnement de la page (`product` : magasin `{stock, name}`, `track`).
 * @returns {HTMLElement}
 */
export function operationStrip(data, env) {
  const stockSlot = h('span.cmp-op__stock', '—');
  const nameSlot = h('span.cmp-op__product-name');
  let counter = null;

  env.track(
    env.product.subscribe(
      null,
      ({ stock, name }) => {
        clear(nameSlot, name || 'Produit du catalogue');
        if (stock === null) return;
        if (!counter) {
          counter = CountUp({ value: stock, format: (value) => fmtNumber(Math.round(value)) });
          clear(stockSlot, counter);
        } else if (counter.value !== stock) {
          counter.set(stock);
          stockSlot.dataset.changed = 'true';
          window.setTimeout(() => delete stockSlot.dataset.changed, 700);
        }
      },
      { immediate: true },
    ),
  );

  const signature = h(
    'code.cmp-op__signature',
    h('span.cmp-op__fn', data.method),
    '(',
    h('span.cmp-op__arg', JSON.stringify(data.product_id)),
    ', ',
    h('span.cmp-op__arg', `-${data.quantity}`),
    ')',
    h('span.cmp-op__arrow', { 'aria-hidden': 'true' }, ' → '),
    h('span.cmp-op__ret', 'new_stock'),
  );

  return h(
    'div.cmp-op',
    cell('Opération comparée', h('p.cmp-op__sentence', `${data.operation}.`), signature),
    cell('Produit', h('span.cmp-op__product.num', data.product_id), nameSlot),
    cell('Stock en direct', stockSlot, h('span.cmp-op__hint', 'rétabli après chaque exécution')),
    cell(
      `${data.snippets.length} écritures`,
      h('div.cmp-op__chips', data.snippets.map((snippet) => ProtocolChip(snippet.id, { short: true, size: 'sm' }))),
      h('span.cmp-op__hint', 'une seule procédure côté serveur'),
    ),
  );
}
