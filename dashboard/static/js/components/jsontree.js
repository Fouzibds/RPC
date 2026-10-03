/**
 * Arbre JSON repliable : valeurs colorées par type, nombre d'éléments, longues chaînes
 * tronquées, copie de l'ensemble ou d'un sous-arbre.
 */

import { h, clear } from '../core/dom.js';
import { fmtNumber } from '../core/format.js';
import { icon } from '../core/icons.js';
import { CopyButton } from './ui.js';

const PAGE = 100;

function typeOf(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function summary(value, type) {
  const size = type === 'array' ? value.length : Object.keys(value).length;
  if (type === 'array') return `${fmtNumber(size)} élément${size > 1 ? 's' : ''}`;
  return `${fmtNumber(size)} clé${size > 1 ? 's' : ''}`;
}

/**
 * Arbre JSON interactif.
 *
 *     JsonTree(result, { collapsedDepth: 2, rootLabel: 'result' })
 *
 * @param {*} value Valeur JSON (objet, tableau, chaîne, nombre, booléen, `null`).
 * @param {Object} [options]
 * @param {number} [options.collapsedDepth=2] Profondeur à partir de laquelle les nœuds sont repliés (0 = tout replié).
 * @param {string} [options.rootLabel] Nom affiché devant la racine.
 * @param {number} [options.maxString=96] Longueur au-delà de laquelle une chaîne est tronquée (clic pour la déplier).
 * @param {boolean} [options.copy=true] Bouton de copie de l'ensemble, et de chaque sous-arbre au survol.
 * @param {number|string} [options.maxHeight] Hauteur maximale (défilement au-delà).
 * @returns {HTMLElement & {update: (value: *) => void, expandAll: () => void, collapseAll: () => void, destroy: () => void}}
 */
export function JsonTree(value, { collapsedDepth = 2, rootLabel, maxString = 96, copy = true, maxHeight } = {}) {
  const tools = h('div.jtree__tools');
  const rootSlot = h('div.jtree__root', { role: 'tree' });
  const el = h('div.jtree.mono.scroll-x.scroll-y', tools, rootSlot);
  if (maxHeight !== undefined) el.style.maxHeight = typeof maxHeight === 'number' ? `${maxHeight}px` : maxHeight;
  let current = value;
  let forced = null;

  function scalar(item, type) {
    if (type === 'string') {
      const long = item.length > maxString;
      const node = h('span.jtree__value.jtree__value--string', { textContent: JSON.stringify(long ? item.slice(0, maxString) : item) });
      if (!long) return node;
      const rest = h('button.jtree__more', { type: 'button', textContent: `… +${fmtNumber(item.length - maxString)} car.` });
      rest.addEventListener('click', () => {
        node.textContent = JSON.stringify(item);
        rest.remove();
      });
      node.textContent = node.textContent.slice(0, -1);
      return [node, rest];
    }
    return h('span.jtree__value', { class: `jtree__value--${type}`, textContent: type === 'undefined' ? 'undefined' : String(item) });
  }

  function keyNode(key, isIndex) {
    if (key === undefined) return null;
    return [h('span', { class: isIndex ? 'jtree__index' : 'jtree__key', textContent: isIndex ? String(key) : JSON.stringify(key) }), h('span.jtree__colon', { textContent: ': ' })];
  }

  function node(item, key, depth, isIndex, last) {
    const type = typeOf(item);
    const comma = last ? null : h('span.jtree__comma', { textContent: ',' });
    if (type !== 'object' && type !== 'array') {
      return h('div.jtree__node', { role: 'treeitem' }, h('div.jtree__line', h('span.jtree__gap'), keyNode(key, isIndex), scalar(item, type), comma));
    }

    const entries = type === 'array' ? item.map((child, index) => [index, child]) : Object.entries(item);
    const open = type === 'array' ? '[' : '{';
    const close = type === 'array' ? ']' : '}';
    if (entries.length === 0) {
      return h('div.jtree__node', { role: 'treeitem' }, h('div.jtree__line', h('span.jtree__gap'), keyNode(key, isIndex), h('span.jtree__brace', { textContent: open + close }), comma));
    }

    let expanded = forced ?? depth < collapsedDepth;
    let rendered = 0;
    const children = h('div.jtree__children', { role: 'group' });
    const preview = h('span.jtree__preview');
    const tail = h('div.jtree__line.jtree__line--close', h('span.jtree__gap'), h('span.jtree__brace', { textContent: close }), last ? null : h('span.jtree__comma', { textContent: ',' }));
    const toggle = h('button.jtree__toggle', { type: 'button', 'aria-label': 'Déplier ou replier' }, icon('chevron-right', { size: 12, stroke: 2.25 }));
    const copyButton = copy ? CopyButton({ text: () => JSON.stringify(item, null, 2), label: 'Copier ce sous-arbre' }) : null;
    const head = h(
      'div.jtree__line.jtree__line--branch',
      toggle,
      keyNode(key, isIndex),
      h('span.jtree__brace', { textContent: open }),
      preview,
      copyButton ? h('span.jtree__copy', copyButton) : null,
    );
    const container = h('div.jtree__node', { role: 'treeitem' }, head, children, tail);

    function renderMore() {
      const upTo = Math.min(entries.length, rendered + PAGE);
      const fragment = [];
      for (let index = rendered; index < upTo; index += 1) {
        const [childKey, child] = entries[index];
        fragment.push(node(child, childKey, depth + 1, type === 'array', index === entries.length - 1));
      }
      children.querySelector(':scope > .jtree__page')?.remove();
      children.append(...fragment);
      rendered = upTo;
      if (rendered < entries.length) {
        const rest = entries.length - rendered;
        const pager = h('button.jtree__more.jtree__page', { type: 'button', textContent: `… ${fmtNumber(rest)} de plus` });
        pager.addEventListener('click', renderMore);
        children.append(pager);
      }
    }

    function apply() {
      container.setAttribute('aria-expanded', String(expanded));
      if (expanded && rendered === 0) renderMore();
      children.hidden = !expanded;
      tail.hidden = !expanded;
      clear(preview, expanded ? h('span.jtree__count', summary(item, type)) : [h('span.jtree__ellipsis', { textContent: '…' }), h('span.jtree__brace', { textContent: close }), comma, h('span.jtree__count', summary(item, type))]);
    }

    const flip = () => {
      expanded = !expanded;
      apply();
    };
    toggle.addEventListener('click', flip);
    head.addEventListener('dblclick', (event) => {
      if (!event.target.closest('button')) flip();
    });
    apply();
    return container;
  }

  function render() {
    const type = typeOf(current);
    clear(tools, copy && (type === 'object' || type === 'array') ? null : copy ? CopyButton({ text: () => JSON.stringify(current, null, 2), label: 'Copier' }) : null);
    clear(rootSlot, node(current, rootLabel, 0, false, true));
  }

  el.update = (next) => {
    current = next;
    forced = null;
    render();
  };
  el.expandAll = () => {
    forced = true;
    render();
    forced = null;
  };
  el.collapseAll = () => {
    forced = false;
    render();
    forced = null;
  };
  el.destroy = () => {};
  render();
  return el;
}
