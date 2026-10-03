/**
 * Console RPC — résultat d'un appel synchrone : bandeau d'état, quatre mesures, puis les onglets
 * « Résultat », « Sur le fil », « Étapes » et, avec une politique de résilience, « Tentatives ».
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtMs, fmtNumber, fmtPercent } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';
import { JsonTree } from '../../components/jsontree.js';
import { Button, Callout, EmptyState, Skeleton, Stat, Tabs } from '../../components/ui.js';
import { AttemptList, Banner, CallId, Insight, errorBadges, strong } from './parts.js';
import { StagesPanel, WirePanel } from './wire.js';

const OUTCOME_TITLES = {
  gave_up: 'Abandon : toutes les tentatives ont échoué',
  circuit_open: 'Refusé par le disjoncteur, sans toucher au réseau',
};

function banner(response) {
  const info = protocol(response.protocol);
  const aside = CallId(response.call_id);
  if (response.ok) {
    const retried = response.attempts.length > 1;
    return Banner({
      tone: retried ? 'warning' : 'success',
      title: retried ? `Succès à la ${response.attempts.length}ᵉ tentative` : 'Succès',
      text: [h('span.mono', response.method), ` a répondu en ${fmtMs(response.duration_ms)} par ${info.label}${response.via_proxy ? ', à travers le proxy de chaos' : ''}.`],
      aside,
    });
  }
  const error = response.error ?? {};
  return Banner({ tone: 'danger', title: OUTCOME_TITLES[response.outcome] ?? 'Échec de l’appel', text: error.message, badges: errorBadges(error), aside });
}

function stats(response, policyEnabled, maxAttempts) {
  const local = response.protocol === 'local';
  const server = response.server_ms;
  const share = Number.isFinite(server) && response.duration_ms > 0 ? server / response.duration_ms : null;
  const noBytes = local ? 'aucun octet : en mémoire' : 'rien n’a été échangé';
  const attempts = response.attempts.length;
  return h(
    'div.console-stats',
    Stat({
      label: 'Durée',
      icon: 'timer',
      value: response.duration_ms,
      format: fmtMs,
      hint: share !== null ? `dont ${fmtMs(server)} dans la procédure (${fmtPercent(share, { decimals: share < 0.1 ? 1 : 0 })})` : policyEnabled ? 'tentatives et attentes comprises' : 'mesurée par le client',
    }),
    Stat({ label: 'Requête', icon: 'arrow-up-right', value: response.request_bytes ?? '—', format: (v) => fmtBytes(v, { exact: true }), hint: response.request_bytes == null ? noBytes : 'écrits sur le fil' }),
    Stat({ label: 'Réponse', icon: 'arrow-down-left', value: response.response_bytes ?? '—', format: (v) => fmtBytes(v, { exact: true }), hint: response.response_bytes == null ? noBytes : 'lus sur le fil' }),
    Stat({ label: 'Tentatives', icon: 'repeat', value: attempts, format: (v) => fmtNumber(v, { decimals: 0 }), hint: policyEnabled ? `sur ${maxAttempts} permises` : 'sans politique de résilience' }),
  );
}

/**
 * Vue du résultat d'un appel synchrone.
 * @param {Object} props
 * @param {Object} props.response Réponse `sync` de `POST /api/call`.
 * @param {Object} props.request Corps envoyé (`policy`, `params`).
 * @param {Object} props.catalog
 * @param {{get: (path: string) => Promise<*>}} props.api
 * @param {(node: T) => T} props.track Enregistre un composant à détruire.
 * @returns {HTMLElement}
 * @template T
 */
export function SyncResult({ response, request, catalog, api, track }) {
  const policy = request.policy;
  const attempts = response.attempts ?? [];
  let tracePromise = null;

  function loadTrace() {
    if (!tracePromise) tracePromise = api.get(`/api/traces/${encodeURIComponent(response.call_id)}`, { timeoutMs: 8000 });
    return tracePromise;
  }

  /** Panneau qui dépend de la trace : squelette, puis contenu, ou erreur avec « Réessayer ». */
  function tracePanel(render) {
    const slot = h('div.console-panel', { 'aria-live': 'polite' });
    async function load() {
      if (!response.call_id) {
        clear(slot, EmptyState({ icon: 'eye-off', size: 'sm', title: 'Appel non tracé', text: 'Aucune trace n’a été enregistrée pour cet appel (un banc d’essai en cours coupe le bus de traces).' }));
        return;
      }
      clear(slot, h('div.console-skeleton', Skeleton({ variant: 'block', height: 28 }), Skeleton({ variant: 'block', height: 150 }), Skeleton({ lines: 2 })));
      try {
        clear(slot, render(await loadTrace()));
      } catch (error) {
        tracePromise = null;
        clear(
          slot,
          Callout({
            tone: error.offline ? 'warning' : 'danger',
            title: error.offline ? 'Laboratoire injoignable' : 'Trace indisponible',
            text: error.message,
            actions: Button({ label: 'Réessayer', size: 'sm', icon: 'refresh-cw', onClick: load }),
          }),
        );
      }
    }
    load();
    return slot;
  }

  const tabs = [
    {
      id: 'result',
      label: response.ok ? 'Résultat' : 'Erreur',
      icon: 'braces',
      content: () =>
        h(
          'div.console-panel',
          track(JsonTree(response.ok ? response.result : response.error, { rootLabel: response.ok ? 'result' : 'error', collapsedDepth: 3, maxHeight: 420 })),
          response.breaker
            ? Insight(['Disjoncteur ', strong(response.breaker.state === 'closed' ? 'fermé' : response.breaker.state === 'open' ? 'ouvert' : 'semi-ouvert'), ` : ${response.breaker.failures} échec(s) consécutif(s) sur ${response.breaker.failure_threshold} avant ouverture.`], 'shield')
            : null,
        ),
    },
    { id: 'wire', label: 'Sur le fil', icon: 'binary', content: () => tracePanel((trace) => WirePanel({ trace, protocolId: response.protocol, track })) },
    { id: 'stages', label: 'Étapes', icon: 'timer', content: () => tracePanel((trace) => StagesPanel({ trace, catalog, protocolId: response.protocol, track })) },
  ];
  if (policy || attempts.length > 1) {
    tabs.push({ id: 'attempts', label: 'Tentatives', icon: 'repeat', badge: attempts.length, content: () => h('div.console-panel', AttemptList(attempts)) });
  }

  return h(
    'div.console-result',
    banner(response),
    stats(response, Boolean(policy), policy?.max_attempts ?? 1),
    Tabs({ tabs }),
  );
}
