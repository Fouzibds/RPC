/**
 * Console RPC — états d'attente et petites fonctions de présentation partagées par la page :
 * squelettes de chargement, résumé d'une requête, budget de temps accordé à `POST /api/call`.
 */

import { h } from '../../core/dom.js';
import { protocol } from '../../core/protocols.js';
import { Skeleton } from '../../components/ui.js';

/** Ce que le code équivalent enseigne, protocole par protocole. */
export const SNIPPET_NOTES = Object.freeze({
  local: 'Une ligne : un appel de méthode sur un objet du même processus.',
  custom: 'La même forme que l’appel local : seul le stub sait qu’un réseau se trouve derrière.',
  grpc: 'Stub généré depuis le contrat .proto : la requête et la réponse sont des messages typés.',
  rest: 'Sans stub : URL, verbe, en-têtes, JSON et statut reviennent à l’appelant.',
});

/**
 * Délai accordé à `POST /api/call` : il doit couvrir les tentatives, leurs attentes et, pour un
 * lot, les vagues successives d'appels simultanés.
 * @param {Object} request Corps de la requête.
 * @returns {number} Millisecondes (10 minutes au plus).
 */
export function requestBudgetMs(request) {
  const attempts = request.policy?.max_attempts ?? 1;
  const waves = Math.ceil((request.count ?? 1) / 16);
  return Math.min(600000, 20000 + request.timeout_ms * attempts * waves + (request.policy?.base_delay_ms ?? 0) * 2 ** attempts);
}

/**
 * Résumé d'une requête pour le sous-titre du volet de résultat.
 * @param {Object} request Corps de la requête.
 * @returns {string}
 */
export function describeRequest(request) {
  const mode = request.mode === 'async' ? `${request.count} appels de front` : request.mode === 'stream' ? 'flux' : 'appel synchrone';
  return `${request.method} · ${protocol(request.protocol).label} · ${mode}${request.via_proxy ? ' · via le proxy de chaos' : ''}`;
}

/**
 * Squelette du volet de résultat pendant un appel.
 * @returns {HTMLElement}
 */
export function resultSkeleton() {
  return h(
    'div.console-skeleton',
    { 'aria-busy': 'true', 'aria-label': 'Appel en cours' },
    Skeleton({ variant: 'block', height: 60 }),
    h('div.console-stats', ...Array.from({ length: 4 }, () => Skeleton({ variant: 'block', height: 72 }))),
    Skeleton({ variant: 'block', height: 36 }),
    Skeleton({ variant: 'block', height: 180 }),
  );
}

/**
 * Squelette du formulaire pendant le chargement du catalogue.
 * @returns {HTMLElement}
 */
export function formSkeleton() {
  return h(
    'div.console-skeleton',
    { 'aria-busy': 'true', 'aria-label': 'Chargement du catalogue' },
    Skeleton({ variant: 'block', height: 32 }),
    Skeleton({ lines: 2 }),
    Skeleton({ variant: 'block', height: 32 }),
    Skeleton({ lines: 3 }),
    Skeleton({ variant: 'block', height: 96 }),
    Skeleton({ variant: 'block', height: 32 }),
    Skeleton({ variant: 'block', height: 40 }),
  );
}
