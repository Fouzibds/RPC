/**
 * Briques de mise en page du guide de style : carte de démonstration et spécimen étiqueté.
 */

import { h } from '../../core/dom.js';
import { Card, Col } from '../../components/ui.js';

/**
 * Carte de démonstration placée dans une colonne de grille.
 * @param {{title: string, subtitle?: string, span?: number, md?: number, sm?: number, padding?: string, actions?: Node|Node[]}} props
 * @param {...*} children
 * @returns {HTMLElement}
 */
export function DemoCard({ title, subtitle, span = 6, md = 12, sm = 12, padding = 'md', actions }, ...children) {
  return Col({ span, md, sm }, Card({ title, subtitle, padding, actions }, h('div.kit-demo', children)));
}

/**
 * Ligne de spécimen : petit libellé à gauche, variantes à droite.
 * @param {string} label
 * @param {...*} content
 * @returns {HTMLElement}
 */
export function Specimen(label, ...content) {
  return h('div.kit-specimen', h('div.kit-specimen__label.t-label', label), h('div.kit-specimen__content', content));
}

/**
 * Extrait de code d'usage affiché sous une démonstration.
 * @param {string} code
 * @returns {HTMLElement}
 */
export function Usage(code) {
  return h('pre.kit-usage.mono', code);
}
