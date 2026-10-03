/**
 * Banc d'essai — états de la page hors résultats : chargement du dernier rapport, laboratoire
 * injoignable, erreur de chargement, et sommaire des sections.
 */

import { h } from '../../core/dom.js';
import { Button, Callout, Card, EmptyState, Skeleton } from '../../components/ui.js';

const TOC = [
  ['bench-highlights', 'Faits marquants'],
  ['bench-payload', 'Tailles'],
  ['bench-latency', 'Latence'],
  ['bench-serialization', 'Sérialisation'],
  ['bench-network', 'Local vs distant'],
  ['bench-report', 'Rapport'],
];

/**
 * Sommaire : un bouton par section de résultats, qui fait défiler la page jusqu'à elle.
 * @returns {HTMLElement}
 */
export function Toc() {
  return h(
    'nav.bench-toc',
    { 'aria-label': 'Sections du rapport' },
    TOC.map(([id, label]) => h('button.bench-toc__link', { type: 'button', onClick: () => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }, label)),
  );
}

/**
 * Squelettes affichés pendant le chargement du dernier rapport.
 * @returns {HTMLElement}
 */
export function LoadingState() {
  return h(
    'div.bench-loading',
    { 'aria-busy': 'true', 'aria-label': 'Chargement du dernier rapport' },
    h(
      'div.bench-facts',
      Array.from({ length: 5 }, () => h('div.bench-fact.bench-fact--loading', Skeleton({ width: '55%' }), Skeleton({ variant: 'block', width: '42%', height: 26 }), Skeleton({ lines: 2 }))),
    ),
    Card({ padding: 'md' }, Skeleton({ variant: 'block', height: 240 })),
  );
}

/**
 * Bandeau d'état de la page : laboratoire injoignable (avec ou sans rapport déjà affiché) ou
 * dernier rapport illisible. `null` quand tout va bien.
 * @param {Object} props
 * @param {boolean} props.offline Le laboratoire ne répond pas.
 * @param {boolean} props.failed Le dernier rapport n'a pas pu être chargé (hors panne du laboratoire).
 * @param {boolean} props.hasView Un rapport est déjà affiché.
 * @param {boolean} props.tracking Une mesure est suivie par la page.
 * @param {string} [props.message] Message de l'erreur de chargement.
 * @param {() => void} props.onRetry
 * @returns {HTMLElement|null}
 */
export function PageAlert({ offline, failed, hasView, tracking, message, onRetry }) {
  const retry = Button({ label: 'Réessayer', icon: 'refresh-cw', size: 'sm', onClick: onRetry });
  if (offline && !hasView) {
    return Card(
      { padding: 'lg' },
      EmptyState({
        icon: 'wifi-off',
        tone: 'warning',
        title: 'Laboratoire injoignable',
        text: 'Le dernier rapport n’a pas pu être lu et aucune mesure ne peut être lancée. Démarrez le laboratoire (python main.py --dashboard) : la page se reconnecte toute seule.',
        action: retry,
      }),
    );
  }
  if (offline) {
    return Callout({
      tone: 'warning',
      title: 'Laboratoire injoignable',
      text: tracking
        ? 'La mesure continue côté serveur ; son suivi reprendra dès que le laboratoire répondra.'
        : 'Les mesures affichées sont celles du dernier rapport chargé. Le lancement est suspendu jusqu’au retour du laboratoire.',
      actions: retry,
    });
  }
  if (failed) return Callout({ tone: 'danger', title: 'Le dernier rapport n’a pas pu être chargé', text: message, actions: retry });
  return null;
}
