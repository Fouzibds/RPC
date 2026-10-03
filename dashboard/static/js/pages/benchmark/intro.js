/**
 * Banc d'essai — état initial : aucun rapport, aucune mesure en cours. Explique ce que les
 * quatre suites vont mesurer et propose de lancer un benchmark rapide.
 */

import { h } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { Button, Card, EmptyState, Kbd } from '../../components/ui.js';
import { PRESETS, SUITES } from './model.js';

const OUTPUTS = {
  payload: ['Requête et réponse par procédure', 'Octets réellement relayés sur TCP', 'Montée en charge de 1 à 1 000 produits'],
  serialization: ['Encodage et décodage, en nanosecondes', 'Une fiche produit, puis une liste de 100', 'Codec seul, puis conversion comprise'],
  latency: ['Moyenne, médiane, p95, p99', 'Distribution et débit par protocole', 'Surcoût face à l’appel local'],
  network: ['Temps par appel selon la latence ajoutée', 'Les quatre protocoles sur le même axe', 'Projection sur une boucle d’appels'],
};

/**
 * Carte d'accueil du banc d'essai.
 * @param {{onQuick: () => void}} props `onQuick` lance le préréglage « Rapide ».
 * @returns {HTMLElement & {setDisabled: (on: boolean) => void}}
 */
export function Intro({ onQuick }) {
  const quick = PRESETS.find((preset) => preset.id === 'quick');
  const action = Button({ label: 'Lancer un benchmark rapide', icon: 'play', variant: 'primary', size: 'lg', id: 'bench-quick', onClick: onQuick });
  const el = Card(
    { padding: 'none', class: 'bench-intro' },
    EmptyState({
      icon: 'flask-conical',
      size: 'lg',
      tone: 'accent',
      title: 'Aucune mesure pour l’instant',
      text: 'Le banc d’essai exécute quatre suites sur le laboratoire en marche. Rien n’est supposé : chaque nombre affiché ensuite aura été mesuré sur cette machine, et aucun vainqueur n’est écrit d’avance.',
      action: [
        action,
        h('span.bench-intro__hint', `${quick.iterations} appels par protocole · quelques secondes · `, Kbd('mod+enter', { size: 'sm' }), ' lance la configuration ci-dessus'),
      ],
    }),
    h(
      'ol.bench-intro__suites',
      SUITES.map((suite, index) =>
        h(
          'li.bench-intro__suite',
          h('span.bench-intro__step.num', String(index + 1).padStart(2, '0')),
          h('span.bench-intro__icon', { 'aria-hidden': 'true' }, icon(suite.icon, { size: 16 })),
          h('h3.bench-intro__title', suite.question),
          h('p.bench-intro__text', suite.method),
          h(
            'ul.bench-intro__list',
            OUTPUTS[suite.id].map((line) => h('li', line)),
          ),
        ),
      ),
    ),
  );
  el.setDisabled = (on) => action.setDisabled(on);
  return el;
}
