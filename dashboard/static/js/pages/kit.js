/**
 * Kit d'interface (#/kit) — guide de style vivant : chaque composant de `components/ui.js`,
 * `charts.js`, `hexview.js`, `codeblock.js` et `jsontree.js` dans toutes ses variantes, avec
 * des données réalistes du laboratoire. C'est la référence
 * des auteurs de pages et le banc de vérification visuelle du système de design.
 */

import { h } from '../core/dom.js';
import { iconNames } from '../core/icons.js';
import { toggleTheme } from '../core/theme.js';
import { Badge, Button, PageHeader } from '../components/ui.js';
import { bytesAndCode } from './kit/bytes.js';
import { charts } from './kit/charts.js';
import { controls } from './kit/controls.js';
import { display } from './kit/display.js';
import { foundations } from './kit/foundations.js';
import { layers } from './kit/layers.js';
import { structure } from './kit/structure.js';

const SECTIONS = [
  ['kit-foundations', 'Fondations'],
  ['kit-structure', 'Structure'],
  ['kit-controls', 'Contrôles'],
  ['kit-display', 'Affichage'],
  ['kit-layers', 'Couches'],
  ['kit-charts', 'Graphiques'],
  ['kit-bytes', 'Code & octets'],
];

function chapter(id, title, sections) {
  return h('div.kit-chapter', { id }, h('h2.kit-chapter__title', title), sections);
}

export default {
  id: 'kit',
  title: 'Kit d’interface',
  subtitle: 'Composants, couleurs et typographie du laboratoire',
  icon: 'component',
  /**
   * @param {HTMLElement} container Conteneur vide fourni par la coquille.
   * @returns {() => void} Nettoyage appelé quand on quitte la page.
   */
  mount(container) {
    const cleanups = [];
    const toc = h(
      'nav.kit-toc',
      { 'aria-label': 'Sections du guide' },
      SECTIONS.map(([id, label]) =>
        h('button.kit-toc__link', { type: 'button', onClick: () => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }, label),
      ),
    );

    container.append(
      PageHeader({
        eyebrow: 'Guide de style',
        title: 'Kit d’interface',
        description:
          'Le système de design du laboratoire : jetons, composants, couches flottantes, graphiques, code et octets. Tout vient de components/ (ui.js, charts.js, hexview.js, codeblock.js, jsontree.js) ; chaque exemple ci-dessous est un composant réel, pas une capture.',
        icon: 'component',
        actions: Button({ label: 'Basculer le thème', icon: 'sun', kbd: 't', onClick: () => toggleTheme() }),
        meta: [Badge({ label: '53 composants', tone: 'accent' }), Badge({ label: `${iconNames().length} icônes` }), Badge({ label: 'Sombre + clair' }), toc],
      }),
      chapter('kit-foundations', 'Fondations', foundations(cleanups)),
      chapter('kit-structure', 'Structure', structure()),
      chapter('kit-controls', 'Contrôles', controls()),
      chapter('kit-display', 'Affichage', display()),
      chapter('kit-layers', 'Couches', layers()),
      chapter('kit-charts', 'Graphiques', charts(cleanups)),
      chapter('kit-bytes', 'Code & octets', bytesAndCode(cleanups)),
    );
    return () => cleanups.forEach((cleanup) => cleanup());
  },
};
