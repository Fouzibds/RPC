/**
 * Chaos réseau — pannes ponctuelles : « couper la prochaine requête » et « perdre la prochaine
 * réponse », armées protocole par protocole (`POST /api/network/arm`). Une puce armée se
 * désarme d'un second clic ; le proxy la consomme à la prochaine requête qui passe.
 */

import { h } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../../core/protocols.js';

const FAULTS = [
  { kind: 'reset', label: 'Couper la prochaine requête', icon: 'unplug', text: 'Coupée avant le serveur : rien n’est exécuté.' },
  { kind: 'lost_reply', label: 'Perdre la prochaine réponse', icon: 'cloud-off', text: 'Exécutée, mais réponse perdue : issue inconnue.' },
];

/**
 * Bloc « Pannes ponctuelles ».
 * @param {{onArm: (kind: 'reset'|'lost_reply', protocolId: string, count: number) => void}} hooks
 *   `onArm` reçoit le nombre de pannes voulu : 1 pour armer, 0 pour désarmer.
 * @returns {{el: HTMLElement, set: (armed: Record<string, Record<string, number>>) => void, setDisabled: (on: boolean) => void, armedPairs: () => Array<[string, string]>}}
 *   `armedPairs` liste les couples `[kind, protocole]` encore armés.
 */
export function FaultArming(hooks) {
  const chips = new Map();
  let armed = {};
  const countOf = (kind, id) => Number(armed?.[id]?.[kind] ?? 0);

  const el = h(
    'div.chaos-faults',
    FAULTS.map((fault) =>
      h(
        'div.chaos-fault',
        h('div.chaos-fault__head', h('span.chaos-fault__icon', { 'aria-hidden': 'true' }, icon(fault.icon, { size: 14 })), h('span.chaos-fault__label', fault.label)),
        h('p.chaos-fault__hint', fault.text),
        h(
          'div.chaos-fault__targets',
          { role: 'group', 'aria-label': fault.label },
          REMOTE_PROTOCOL_IDS.map((id) => {
            const chip = h(
              'button.chaos-arm',
              {
                type: 'button',
                'aria-pressed': 'false',
                style: { '--c': protocol(id).color },
                dataset: { armed: 'false' },
                title: `${fault.label} — ${protocol(id).label}`,
                onClick: () => hooks.onArm(fault.kind, id, countOf(fault.kind, id) > 0 ? 0 : 1),
              },
              h('span.chaos-arm__dot', { 'aria-hidden': 'true' }),
              h('span.chaos-arm__label', protocol(id).short),
              h('span.chaos-arm__state'),
            );
            chips.set(`${fault.kind}:${id}`, { chip, kind: fault.kind, id });
            return chip;
          }),
        ),
      ),
    ),
  );

  return {
    el,
    set(next) {
      armed = next ?? {};
      for (const { chip, kind, id } of chips.values()) {
        const count = countOf(kind, id);
        chip.dataset.armed = String(count > 0);
        chip.setAttribute('aria-pressed', String(count > 0));
        chip.lastElementChild.textContent = count > 1 ? `armée ×${count}` : count === 1 ? 'armée' : '';
      }
    },
    setDisabled(on) {
      for (const { chip } of chips.values()) chip.disabled = on;
    },
    armedPairs() {
      return Array.from(chips.values())
        .filter(({ kind, id }) => countOf(kind, id) > 0)
        .map(({ kind, id }) => [kind, id]);
    },
  };
}
