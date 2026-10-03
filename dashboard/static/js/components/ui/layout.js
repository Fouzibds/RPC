/**
 * Structure des pages : PageHeader, Card, Section, Grid/Col, Stack, Divider.
 */

import { h, clear, cx, splitArgs } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';

const TONES = new Set(['accent', 'success', 'warning', 'danger', 'info']);

function edgeColor(accent, protocolId) {
  if (protocolId) return protocol(protocolId).color;
  if (!accent) return null;
  return TONES.has(accent) ? `var(--${accent})` : accent;
}

/**
 * En-tête de page : surtitre, grand titre, description, actions à droite.
 * @param {Object} props
 * @param {string} props.title
 * @param {string} [props.eyebrow] Surtitre en petites capitales (ex. le groupe de navigation).
 * @param {string|Node} [props.description]
 * @param {string} [props.icon] Icône dans une tuile, à gauche du titre.
 * @param {Node|Node[]} [props.actions] Boutons alignés à droite.
 * @param {Node|Node[]} [props.meta] Ligne de puces / métadonnées sous la description.
 * @returns {HTMLElement & {update: (next: {title?: string, description?: string|Node}) => void}}
 */
export function PageHeader({ title, eyebrow, description, icon: iconName, actions, meta } = {}) {
  const titleEl = h('h1.page-header__title', title);
  const descriptionEl = h('p.page-header__description', description ?? null);
  descriptionEl.hidden = !description;
  const el = h(
    'header.page-header',
    iconName ? h('div.page-header__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: 20 })) : null,
    h(
      'div.page-header__text',
      eyebrow ? h('div.page-header__eyebrow.t-label', eyebrow) : null,
      titleEl,
      descriptionEl,
      meta ? h('div.page-header__meta', meta) : null,
    ),
    actions ? h('div.page-header__actions', actions) : null,
  );
  el.update = (next) => {
    if (next.title !== undefined) clear(titleEl, next.title);
    if (next.description !== undefined) {
      clear(descriptionEl, next.description);
      descriptionEl.hidden = !next.description;
    }
  };
  return el;
}

/**
 * Carte : surface de base de toutes les pages.
 * @param {Object} [props]
 * @param {string|Node} [props.title]
 * @param {string|Node} [props.subtitle]
 * @param {string} [props.icon] Icône devant le titre.
 * @param {Node|Node[]} [props.actions] Contrôles à droite de l'en-tête.
 * @param {Node|Node[]} [props.footer] Pied de carte (séparé par un filet).
 * @param {'none'|'sm'|'md'|'lg'} [props.padding='md'] Marge intérieure du corps (0 / 12 / 20 / 28 px).
 * @param {'accent'|'success'|'warning'|'danger'|'info'|string} [props.accent] Liseré supérieur coloré (ton ou couleur CSS).
 * @param {'local'|'custom'|'grpc'|'rest'} [props.protocol] Liseré à la couleur d'un protocole.
 * @param {boolean} [props.interactive] Réagit au survol (carte cliquable).
 * @param {boolean} [props.flush] Sans ombre ni fond : simple cadre.
 * @param {string} [props.class]
 * @param {...*} children Contenu du corps.
 * @returns {HTMLElement & {body: HTMLElement, setTitle: (title: string|Node) => void, setSubtitle: (subtitle: string|Node) => void}}
 *   `body` est le conteneur du contenu (pour le remplacer plus tard).
 */
export function Card(props, ...children) {
  const [options, content] = splitArgs(props, children);
  const { title, subtitle, icon: iconName, actions, footer, padding = 'md', accent, protocol: protocolId, interactive = false, flush = false, class: className } = options;

  const titleEl = h('h3.card__title');
  const subtitleEl = h('p.card__subtitle');
  const hasHeader = title !== undefined || subtitle !== undefined || actions || iconName;
  const body = h('div.card__body', content);
  const edge = edgeColor(accent, protocolId);

  const el = h(
    'section.card',
    {
      class: [`card--pad-${padding}`, { 'card--interactive': interactive, 'card--flush': flush, 'card--edge': Boolean(edge) }, className],
      style: edge ? { '--edge': edge } : null,
      dataset: { protocol: protocolId },
    },
    hasHeader
      ? h(
          'header.card__header',
          iconName ? h('span.card__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: 16 })) : null,
          h('div.card__heading', titleEl, subtitleEl),
          actions ? h('div.card__actions', actions) : null,
        )
      : null,
    body,
    footer ? h('footer.card__footer', footer) : null,
  );

  el.body = body;
  el.setTitle = (next) => {
    clear(titleEl, next);
    titleEl.hidden = next === undefined || next === null || next === '';
  };
  el.setSubtitle = (next) => {
    clear(subtitleEl, next);
    subtitleEl.hidden = next === undefined || next === null || next === '';
  };
  el.setTitle(title);
  el.setSubtitle(subtitle);
  return el;
}

/**
 * Section de page : libellé en petites capitales, description, actions, puis le contenu.
 * @param {Object} props
 * @param {string} [props.title]
 * @param {string|Node} [props.description]
 * @param {Node|Node[]} [props.actions]
 * @param {string} [props.id] Ancre.
 * @param {...*} children
 * @returns {HTMLElement}
 */
export function Section(props, ...children) {
  const [{ title, description, actions, id }, content] = splitArgs(props, children);
  return h(
    'section.section',
    { id },
    title || actions
      ? h(
          'header.section__header',
          h('div.section__heading', title ? h('h2.section__title.t-label', title) : null, description ? h('p.section__description', description) : null),
          actions ? h('div.section__actions', actions) : null,
        )
      : null,
    h('div.section__body', content),
  );
}

/**
 * Grille à 12 colonnes (gouttière 16 px). Y placer des `Col`.
 * @param {{gap?: number, align?: 'start'|'center'|'end'|'stretch', class?: string}} [props]
 * @param {...*} children
 * @returns {HTMLElement}
 */
export function Grid(props, ...children) {
  const [{ gap, align, class: className }, content] = splitArgs(props, children);
  return h(
    'div.grid',
    { class: className, style: { '--grid-gap': gap !== undefined ? `${gap}px` : null, alignItems: align ?? null } },
    content,
  );
}

/**
 * Colonne d'une `Grid`.
 * @param {{span?: number, md?: number, sm?: number, class?: string}} [props]
 *   `span` : largeur sur 12 (12 par défaut) ; `md` : largeur quand la zone de contenu fait moins de
 *   1100 px ; `sm` : quand elle fait moins de 840 px.
 * @param {...*} children
 * @returns {HTMLElement}
 */
export function Col(props, ...children) {
  const [{ span = 12, md, sm, class: className }, content] = splitArgs(props, children);
  return h('div.col', { class: cx(`col-${span}`, md ? `col-md-${md}` : null, sm ? `col-sm-${sm}` : null, className) }, content);
}

/**
 * Pile flexible. Verticale par défaut ; `direction: 'row'` pour une rangée.
 * @param {Object} [props]
 * @param {'column'|'row'} [props.direction='column']
 * @param {number} [props.gap] Écart en pixels (12 par défaut en colonne, 8 en rangée).
 * @param {'start'|'center'|'end'|'stretch'|'baseline'} [props.align]
 * @param {'start'|'center'|'end'|'between'} [props.justify]
 * @param {boolean} [props.wrap]
 * @param {string} [props.class]
 * @param {...*} children
 * @returns {HTMLElement}
 */
export function Stack(props, ...children) {
  const [{ direction = 'column', gap, align, justify, wrap = false, class: className }, content] = splitArgs(props, children);
  const flex = { start: 'flex-start', end: 'flex-end', between: 'space-between' };
  return h(
    'div.stack',
    {
      class: [`stack--${direction}`, { 'stack--wrap': wrap }, className],
      style: {
        '--gap': gap !== undefined ? `${gap}px` : null,
        alignItems: align ? (flex[align] ?? align) : null,
        justifyContent: justify ? (flex[justify] ?? justify) : null,
      },
    },
    content,
  );
}

/**
 * Filet de séparation, horizontal ou vertical, avec libellé facultatif.
 * @param {{vertical?: boolean, label?: string}} [props]
 * @returns {HTMLElement}
 */
export function Divider({ vertical = false, label = '' } = {}) {
  return h(
    'div.divider',
    { class: { 'divider--vertical': vertical, 'divider--labelled': Boolean(label) }, role: 'separator', 'aria-orientation': vertical ? 'vertical' : 'horizontal' },
    label ? h('span.divider__label.t-label', label) : null,
  );
}
