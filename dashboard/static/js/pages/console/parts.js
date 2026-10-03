/**
 * Console RPC — petites pièces partagées par les vues de résultat : bandeau d'état, description
 * d'une erreur RPC, liste des tentatives, phrase explicative.
 */

import { h } from '../../core/dom.js';
import { fmtMs } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { Badge, CopyButton } from '../../components/ui.js';

const BANNER_ICONS = { success: 'circle-check', danger: 'circle-x', warning: 'triangle-alert', info: 'loader-circle', neutral: 'info' };

/**
 * Bandeau d'état d'un résultat.
 * @param {Object} props
 * @param {'success'|'danger'|'warning'|'info'|'neutral'} props.tone
 * @param {string|Node} props.title
 * @param {string|Node} [props.text]
 * @param {Array<Node|null>} [props.badges] Puces sous le texte.
 * @param {Node|Node[]} [props.aside] Contenu aligné à droite (identifiant d'appel, bouton).
 * @param {boolean} [props.busy] Icône animée (appel ou flux en cours).
 * @returns {HTMLElement}
 */
export function Banner({ tone, title, text, badges, aside, busy = false }) {
  const shown = (badges ?? []).filter(Boolean);
  return h(
    'div.console-banner',
    { dataset: { tone }, role: tone === 'danger' ? 'alert' : 'status' },
    h('span.console-banner__icon', { 'aria-hidden': 'true' }, icon(BANNER_ICONS[tone] ?? 'info', { size: 18, class: busy ? 'spin' : null })),
    h(
      'div.console-banner__body',
      h('p.console-banner__title', title),
      text ? h('p.console-banner__text', text) : null,
      shown.length ? h('div.console-banner__badges', shown) : null,
    ),
    aside ? h('div.console-banner__aside', aside) : null,
  );
}

/**
 * Identifiant d'appel copiable.
 * @param {string} callId
 * @returns {HTMLElement|null}
 */
export function CallId(callId) {
  if (!callId) return null;
  return h('span.console-callid', h('span.console-callid__text.mono', callId), CopyButton({ text: callId, label: 'Copier l’identifiant de l’appel' }));
}

/** Code propre au middleware porté par `error.detail` : statut gRPC, code JSON-RPC, statut HTTP. */
function nativeCode(error) {
  const detail = error?.detail ?? {};
  if (detail.grpc_status) return `statut gRPC ${detail.grpc_status}`;
  if (detail.jsonrpc_code !== undefined) return `code JSON-RPC ${detail.jsonrpc_code}`;
  if (detail.http_status !== undefined) return `HTTP ${detail.http_status}`;
  if (detail.rejected_by === 'client') return 'refusé côté client, avant tout envoi';
  return '';
}

/**
 * Puces décrivant une erreur RPC (`RpcError.to_dict()`) : code canonique, rejouable ou non,
 * classe d'exception et code propre au protocole.
 * @param {Object} error
 * @returns {Array<Node|null>}
 */
export function errorBadges(error) {
  const native = nativeCode(error);
  return [
    Badge({ label: error.code ?? 'ERREUR', tone: 'danger', mono: true, title: 'Code canonique, identique quel que soit le protocole' }),
    error.retryable
      ? Badge({ label: 'rejouable', tone: 'info', icon: 'repeat', title: 'Erreur passagère : une nouvelle tentative peut réussir.' })
      : Badge({ label: 'non rejouable', tone: 'neutral', icon: 'ban', title: 'Rejouer l’appel donnerait la même erreur.' }),
    error.type ? Badge({ label: error.type, tone: 'neutral', variant: 'outline', mono: true, title: 'Exception levée par le client' }) : null,
    native ? Badge({ label: native, tone: 'neutral', variant: 'outline', title: 'Traduction de l’erreur dans le vocabulaire du middleware' }) : null,
  ];
}

const ATTEMPT_OUTCOMES = {
  ok: { label: 'succès', tone: 'success' },
  error: { label: 'erreur', tone: 'danger' },
  retry: { label: 'échec, rejoué', tone: 'warning' },
  gave_up: { label: 'abandon', tone: 'danger' },
  circuit_open: { label: 'disjoncteur ouvert', tone: 'danger' },
  rejected: { label: 'refusé', tone: 'danger' },
};

/**
 * Liste des tentatives d'un appel soumis à une politique de résilience.
 * @param {Array<{n: number, call_id?: string, outcome: string, code?: string, duration_ms?: number, backoff_ms?: number}>} attempts
 * @returns {HTMLElement}
 */
export function AttemptList(attempts) {
  const longest = Math.max(1, ...attempts.map((attempt) => (attempt.duration_ms ?? 0) + (attempt.backoff_ms ?? 0)));
  return h(
    'ol.console-attempts',
    attempts.map((attempt) => {
      const outcome = ATTEMPT_OUTCOMES[attempt.outcome] ?? { label: attempt.outcome, tone: 'neutral' };
      const duration = attempt.duration_ms ?? 0;
      const backoff = attempt.backoff_ms ?? 0;
      return h(
        'li.console-attempt',
        { dataset: { tone: outcome.tone } },
        h('span.console-attempt__n.num', String(attempt.n)),
        h(
          'div.console-attempt__main',
          h('div.console-attempt__line', Badge({ label: outcome.label, tone: outcome.tone, size: 'sm', dot: true }), attempt.code && attempt.code !== 'OK' ? h('span.mono.console-attempt__code', attempt.code) : null, attempt.call_id ? h('span.mono.console-attempt__id', attempt.call_id) : null),
          h(
            'div.console-attempt__track',
            { 'aria-hidden': 'true' },
            h('span.console-attempt__bar', { style: { width: `${Math.max(1.5, (duration / longest) * 100)}%` } }),
            backoff > 0 ? h('span.console-attempt__wait', { style: { width: `${(backoff / longest) * 100}%` } }) : null,
          ),
        ),
        h('div.console-attempt__times', h('span.num', fmtMs(duration)), backoff > 0 ? h('span.console-attempt__backoff.num', `puis ${fmtMs(backoff)} d’attente`) : null),
      );
    }),
  );
}

/**
 * Phrase explicative calculée à partir des mesures affichées.
 * @param {string|Node|Array<string|Node>} content
 * @param {string} [iconName='lightbulb']
 * @returns {HTMLElement}
 */
export function Insight(content, iconName = 'lightbulb') {
  return h('p.console-insight', { 'aria-live': 'polite' }, h('span.console-insight__icon', { 'aria-hidden': 'true' }, icon(iconName, { size: 15 })), h('span', content));
}

/**
 * Nombre mis en avant dans une phrase (chasse fixe, sans retouche typographique).
 * @param {string} text
 * @returns {HTMLElement}
 */
export function strong(text) {
  return h('strong.num.console-strong', text);
}
