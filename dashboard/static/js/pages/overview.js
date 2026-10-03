/**
 * Vue d'ensemble — le poste de contrôle du laboratoire : ce qu'il est (trois middlewares, un
 * même service d'inventaire), son état, et tout ce qui y circule, en direct.
 *
 * Sources : `appStore.status` (`GET /api/status`, rafraîchi par le message `stats`),
 * messages WebSocket `stats` (débits par seconde) et `call` (chaque appel terminé),
 * `GET /api/traces` (historique). Rien n'est affiché qui n'ait été mesuré par le laboratoire.
 */

import { h, clear } from '../core/dom.js';
import { refreshStatus } from '../core/api.js';
import { fmtMs, fmtNumber } from '../core/format.js';
import { href } from '../core/router.js';
import { registerShortcut } from '../core/shortcuts.js';
import { Button, Callout, Card, Col, Grid } from '../components/ui.js';
import { createActivity } from './overview/activity.js';
import { createDiagram } from './overview/diagram.js';
import { createHeader } from './overview/header.js';
import { createJourney } from './overview/journey.js';
import { createKpis } from './overview/kpis.js';
import { createInventory, createThroughput } from './overview/side.js';
import { generateTraffic } from './overview/traffic.js';

const plural = (count, one, many) => `${fmtNumber(count)} ${count > 1 ? many : one}`;

function legend() {
  return h(
    'div.ov-legend',
    { 'aria-hidden': 'true' },
    h('span.ov-legend__key', h('span.ov-legend__dot'), 'un appel réel'),
    h('span.ov-legend__key', h('span.ov-legend__dot', { dataset: { tone: 'danger' } }), 'une erreur'),
  );
}

export default {
  id: 'overview',
  title: 'Vue d’ensemble',
  subtitle: 'État du laboratoire, compteurs et architecture en direct',
  icon: 'layout-dashboard',
  /**
   * @param {HTMLElement} container Conteneur vide fourni par la coquille.
   * @param {{store: Object, api: Object, ws: Object, navigate: Function, toast: Object}} ctx
   * @returns {() => void} Nettoyage appelé quand on quitte la page.
   */
  mount(container, ctx) {
    const { store, api, ws, navigate, toast } = ctx;
    let alive = true;
    let busy = false;
    let wasOffline = false;

    async function runTraffic() {
      if (busy) return;
      if (store.get().api === 'offline') {
        toast.error('Laboratoire injoignable', { description: 'Impossible de lancer des appels tant que le serveur du laboratoire ne répond pas.' });
        return;
      }
      busy = true;
      header.traffic.setLoading(true);
      try {
        const result = await generateTraffic(api, store);
        if (!alive) return;
        const summary = `${plural(result.calls, 'appel réel', 'appels réels')} en ${fmtMs(result.wallMs)}`;
        if (result.errors) {
          toast.warn('Trafic généré, avec des erreurs', { description: `${summary}, dont ${fmtNumber(result.errors)} en erreur : le réseau simulé est peut-être dégradé.` });
        } else {
          toast.success('Trafic généré', { description: `${summary}, répartis sur les trois middlewares et l’appel local.` });
        }
      } catch (error) {
        if (alive) toast.error('Trafic non généré', { description: error?.message ?? 'Le laboratoire n’a pas répondu.' });
      } finally {
        busy = false;
        header.traffic.setLoading(false);
      }
    }

    const header = createHeader({ onTraffic: runTraffic });
    const retry = Button({
      label: 'Réessayer',
      icon: 'refresh-cw',
      size: 'sm',
      onClick: async () => {
        retry.setLoading(true);
        await refreshStatus();
        retry.setLoading(false);
      },
    });
    const offlineText = h('span');
    const offline = h('div.ov-offline', { hidden: true }, Callout({ tone: 'danger', icon: 'unplug', title: 'Laboratoire injoignable', text: offlineText, actions: retry }));
    const kpis = createKpis();
    const diagram = createDiagram();
    const architecture = Card(
      {
        title: 'Architecture en direct',
        subtitle: 'Un même appel, quatre chemins possibles. Cliquez une voie pour l’ouvrir sous le capot.',
        icon: 'workflow',
        actions: legend(),
        class: 'ov-architecture',
        footer: [
          h('span', 'Le stub sérialise l’appel, le proxy de chaos dégrade le réseau à la demande, le squelette désérialise puis appelle la procédure.'),
          Button({ label: 'Régler le réseau', variant: 'ghost', size: 'sm', iconRight: 'arrow-right', href: href('chaos') }),
        ],
      },
      diagram,
    );
    architecture.style.setProperty('--i', '4');
    const activity = createActivity({ api, navigate, onTraffic: runTraffic });
    const throughput = createThroughput();
    const inventory = createInventory();
    [activity, throughput, inventory].forEach((card, index) => card.style.setProperty('--i', String(5 + index)));

    container.append(
      header,
      offline,
      kpis,
      architecture,
      Grid(Col({ span: 7, md: 12 }, activity), Col({ span: 5, md: 12 }, h('div.ov-side', throughput, inventory))),
      createJourney(),
    );

    function sync({ api: link, status, network }) {
      const mode = link === 'offline' ? 'offline' : status ? 'ready' : 'loading';
      const isOffline = mode === 'offline';
      offline.hidden = !isOffline;
      clear(
        offlineText,
        status
          ? 'Le serveur du laboratoire ne répond plus : les valeurs ci-dessous sont les dernières reçues. Une nouvelle tentative est faite automatiquement.'
          : 'Le serveur du laboratoire ne répond pas : démarrez-le avec « python main.py --dashboard ». Une nouvelle tentative est faite automatiquement.',
      );
      header.update({ status, network, mode });
      kpis.update({ status, mode });
      diagram.update({ status, network, offline: isOffline });
      inventory.update(isOffline ? null : (status?.inventory ?? null));
      throughput.setOffline(isOffline);
      activity.setOffline(isOffline);
      if (wasOffline && !isOffline) activity.load();
      wasOffline = isOffline;
    }

    const stop = [
      store.subscribe((state) => ({ api: state.api, status: state.status, network: state.network }), sync, { immediate: true }),
      ws.on('stats', (message) => {
        kpis.tick(message);
        throughput.tick(message);
      }),
      ws.on('call', (message) => {
        diagram.packet(message);
        activity.push(message);
      }),
      registerShortcut({ keys: 'mod+enter', description: 'Générer du trafic', group: 'Vue d’ensemble', run: runTraffic }),
    ];
    activity.load();

    return () => {
      alive = false;
      stop.forEach((off) => off());
      kpis.destroy();
      diagram.destroy();
      activity.destroy();
      throughput.destroy();
    };
  },
};
