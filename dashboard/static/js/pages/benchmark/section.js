/**
 * Banc d'essai — briques communes aux sections de résultats : section d'une suite (état
 * « en attente », « mesure en cours », « non mesurée »), ligne de lecture sous un graphique.
 */

import { h, clear } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { Badge, Button, Card, EmptyState, Section } from '../../components/ui.js';
import { suiteInfo } from './model.js';

const STATUS_BADGES = {
  measuring: { label: 'Mesure en cours', tone: 'accent', dot: true, pulse: true },
  pending: { label: 'En attente', tone: 'neutral', dot: true },
};

/**
 * Section de résultats d'une suite. Son contenu est remplacé par un état vide avec appel à
 * l'action quand la suite ne fait pas partie du rapport affiché.
 * @param {Object} props
 * @param {'payload'|'serialization'|'latency'|'network'} props.suite
 * @param {string} props.description La question à laquelle la section répond.
 * @param {(suite: string) => void} props.onRequest Demande de mesurer cette suite.
 * @param {...Node} content Cartes de la section.
 * @returns {{el: HTMLElement, setStatus: (status: string) => void, setContext: (items: Array<string|Node>) => void}}
 */
export function SuiteSection({ suite, description, onRequest }, ...content) {
  const info = suiteInfo(suite);
  const badge = h('span.bench-suite__status', { role: 'status' });
  const context = h('div.bench-context', { hidden: true });
  const body = h('div.bench-suite__content', content);
  const skipped = Card(
    { padding: 'md', flush: true, class: 'bench-skipped' },
    EmptyState({
      icon: info.icon,
      size: 'sm',
      title: `« ${info.title} » ne fait pas partie de ce rapport`,
      text: `${info.question} ${info.method}`,
      action: Button({ label: 'Mesurer cette suite', icon: 'play', size: 'sm', onClick: () => onRequest(suite) }),
    }),
  );
  skipped.hidden = true;

  const el = Section({ id: info.anchor, title: info.title, description, actions: badge }, context, body, skipped);
  el.classList.add('bench-suite');
  let current = null;

  return {
    el,
    setStatus(status) {
      if (status === current) return;
      current = status;
      el.dataset.status = status;
      body.hidden = status === 'skipped';
      skipped.hidden = status !== 'skipped';
      if (status === 'skipped') context.hidden = true;
      clear(badge, STATUS_BADGES[status] ? Badge({ size: 'sm', ...STATUS_BADGES[status] }) : null);
    },
    setContext(items) {
      const list = items.filter(Boolean);
      clear(
        context,
        list.map((item) => h('span.bench-context__item', item)),
      );
      context.hidden = !list.length || current === 'skipped';
    },
  };
}

/**
 * Nombre mis en avant dans une phrase de lecture (chasse fixe, sans typographie automatique).
 * @param {string} text
 * @returns {HTMLElement}
 */
export function num(text) {
  return h('b.bench-num.num', text);
}

/**
 * Ligne de lecture sous un graphique : une phrase calculée à partir des mesures affichées.
 * @param {string} [iconName='lightbulb']
 * @returns {HTMLElement & {set: (...parts: Array<string|Node|null>) => void}} `set()` sans
 *   argument masque la ligne.
 */
export function Insight(iconName = 'lightbulb') {
  const text = h('p.bench-insight__text');
  const el = h('div.bench-insight', { hidden: true }, h('span.bench-insight__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: 14 })), text);
  el.set = (...parts) => {
    const content = parts.flat().filter((part) => part !== null && part !== undefined && part !== '');
    clear(text, content);
    el.hidden = !content.length;
  };
  return el;
}
