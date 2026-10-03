/**
 * Vue d'ensemble — en-tête : ce qu'est le laboratoire, son état en trois puces, ses trois actions.
 */

import { h, clear } from '../../core/dom.js';
import { fmtDuration } from '../../core/format.js';
import { describeNetwork } from '../../core/network.js';
import { REMOTE_PROTOCOL_IDS } from '../../core/protocols.js';
import { href } from '../../core/router.js';
import { Badge, Button, PageHeader, Skeleton } from '../../components/ui.js';
import { serversById } from './model.js';

function serversChip(status, mode) {
  if (mode === 'offline') return Badge({ label: 'Laboratoire hors ligne', tone: 'danger', icon: 'unplug' });
  const servers = serversById(status);
  const total = REMOTE_PROTOCOL_IDS.length;
  const up = REMOTE_PROTOCOL_IDS.filter((id) => servers[id]?.up).length;
  const tone = up === total ? 'success' : up === 0 ? 'danger' : 'warning';
  const down = REMOTE_PROTOCOL_IDS.filter((id) => !servers[id]?.up).map((id) => servers[id]?.label ?? id);
  return Badge({
    label: `${up}/${total} serveurs en service`,
    tone,
    dot: true,
    pulse: up === total,
    title: down.length ? `Arrêté : ${down.join(', ')}` : 'JSON-RPC maison, gRPC et REST répondent',
  });
}

function networkChip(network) {
  const info = describeNetwork(network);
  const label = info.detail ? `${info.label} · ${info.detail}` : info.label;
  return h(
    'a.ov-chip-link',
    { href: href('chaos'), title: 'Réseau simulé par les proxys de chaos — ouvrir Chaos réseau pour le régler' },
    Badge({ label, tone: info.tone, icon: info.icon }),
  );
}

/**
 * En-tête de la page.
 * @param {Object} props
 * @param {() => void} props.onTraffic Lance la rafale d'appels (« Générer du trafic »).
 * @returns {HTMLElement & {traffic: HTMLElement, update: (state: {status: Object|null, network: Object|null, mode: 'loading'|'ready'|'offline'}) => void}}
 *   `traffic` est le bouton principal (pour son état de chargement).
 */
export function createHeader({ onTraffic }) {
  const serversSlot = h('span.ov-chips__slot', { role: 'status' });
  const networkSlot = h('span.ov-chips__slot', { role: 'status' });
  const chips = h('div.ov-chips', serversSlot, networkSlot);
  const uptime = Badge({ label: '', icon: 'clock', title: 'Temps écoulé depuis le démarrage du laboratoire' });
  const traffic = Button({ label: 'Générer du trafic', variant: 'primary', icon: 'zap', kbd: 'mod+enter', id: 'ov-traffic', onClick: onTraffic });
  let signature = '';

  const el = PageHeader({
    eyebrow: 'Explorer',
    title: 'Vue d’ensemble',
    icon: 'layout-dashboard',
    description:
      'Trois middlewares — JSON-RPC maison, gRPC / Protobuf et REST / JSON — exposent le même service d’inventaire. Le laboratoire les fait tourner côte à côte et mesure ce que chacun coûte, en direct.',
    meta: chips,
    actions: [
      Button({ label: 'Lancer un appel', icon: 'square-terminal', href: href('console') }),
      Button({ label: 'Lancer le benchmark', icon: 'gauge', href: href('benchmark') }),
      traffic,
    ],
  });

  el.traffic = traffic;
  el.update = ({ status, network, mode }) => {
    if (mode === 'loading') {
      if (signature !== 'loading') {
        clear(serversSlot, Skeleton({ width: 168, height: 22, variant: 'block' }));
        clear(networkSlot, Skeleton({ width: 112, height: 22, variant: 'block' }));
        uptime.remove();
      }
      signature = 'loading';
      return;
    }
    const servers = serversById(status);
    const info = describeNetwork(mode === 'offline' ? null : network);
    const next = [mode, REMOTE_PROTOCOL_IDS.map((id) => (servers[id]?.up ? 1 : 0)).join(''), info.tone, info.label, info.detail].join('|');
    if (next !== signature) {
      signature = next;
      clear(serversSlot, serversChip(status, mode));
      clear(networkSlot, networkChip(mode === 'offline' ? null : network));
      if (mode === 'offline') uptime.remove();
      else chips.append(uptime);
    }
    const seconds = status?.app?.uptime_s;
    uptime.setLabel(Number.isFinite(seconds) ? `actif depuis ${fmtDuration(seconds)}` : 'démarrage…');
  };
  return el;
}
