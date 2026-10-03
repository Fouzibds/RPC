/**
 * Chaos réseau (#/chaos) — le laboratoire de pannes : un appel distant ressemble à un appel de
 * fonction, mais vit sous les lois du réseau. La page laisse dérégler ce réseau (conditions,
 * pannes ponctuelles), choisir la stratégie du client, puis observer chaque tentative en direct ;
 * cinq scénarios guidés mesurent les pièges classiques (`#/chaos?scenario=<id>` en ouvre un).
 */

import { h } from '../core/dom.js';
import { refreshStatus } from '../core/api.js';
import { applyPreset, describeNetwork } from '../core/network.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../core/protocols.js';
import { Button, Callout, PageHeader } from '../components/ui.js';
import { ConditionsPanel } from './chaos/conditions.js';
import { ClientPanel } from './chaos/client.js';
import { Chronology } from './chaos/chronology.js';
import { Gauges, LocalVsRemote } from './chaos/gauges.js';
import { Feed } from './chaos/feed.js';
import { Scenarios } from './chaos/scenarios.js';
import { conditionsSummary, isDegraded } from './chaos/model.js';

/** Trajet d'un appel de la page : client → proxy de chaos → serveur, avec les ports réels. */
function routeMeta(store) {
  const el = h('div.chaos-route');
  function render(servers) {
    const known = REMOTE_PROTOCOL_IDS.map((id) => (servers ?? []).find((server) => server.id === id)).filter((server) => server?.proxy_port);
    el.hidden = !known.length;
    el.replaceChildren(
      h('span.chaos-route__lead', 'Trajet de chaque appel'),
      ...known.map((server) =>
        h(
          'span.chaos-route__hop',
          { style: { '--c': protocol(server.id).color }, title: `${protocol(server.id).label} : le client parle au proxy, qui relaie (ou non) vers le serveur` },
          h('span.chaos-route__dot', { 'aria-hidden': 'true' }),
          h('span', protocol(server.id).short),
          h('span.chaos-route__ports.num', `proxy :${server.proxy_port} → serveur :${server.port}`),
        ),
      ),
    );
  }
  const off = store.subscribe((state) => state.status?.servers ?? null, render, { immediate: true });
  return { el, off };
}

/** Section des scénarios de la page affichée, pour lui transmettre un changement de `?scenario=`. */
let openScenarios = null;

export default {
  id: 'chaos',
  title: 'Chaos réseau',
  subtitle: 'Latence, coupures et pannes : le réseau n’est pas fiable',
  icon: 'zap',
  /**
   * @param {HTMLElement} container Conteneur vide fourni par la coquille.
   * @param {{store: Object, api: Object, ws: Object, navigate: Function, toast: Object, query: Record<string, string>}} ctx
   * @returns {() => void} Nettoyage appelé quand on quitte la page.
   */
  mount(container, ctx) {
    const { store, toast } = ctx;
    let ownJob = null;
    let lastOwnJobId = null;
    let externalJob = null;
    let traffic = false;

    const route = routeMeta(store);
    const header = PageHeader({
      eyebrow: 'Laboratoire',
      title: 'Chaos réseau',
      icon: 'zap',
      description: 'Le piège de la transparence : un appel distant s’écrit comme un appel de fonction, mais il traverse un réseau qui ralentit, coupe et perd des réponses. Déréglez-le, puis regardez ce que chaque stratégie de client en fait.',
      meta: route.el,
    });

    const offline = h('div.chaos-offline', { hidden: true });
    offline.append(
      Callout({
        tone: 'danger',
        title: 'Laboratoire injoignable',
        text: 'Les serveurs du laboratoire ne répondent pas : réglages, appels et scénarios sont en attente. La reconnexion est automatique.',
        actions: Button({ label: 'Réessayer maintenant', icon: 'refresh-cw', size: 'sm', onClick: () => refreshStatus() }),
      }),
    );

    const conditions = ConditionsPanel(ctx);
    const gauges = Gauges(ctx);
    const feed = Feed(ctx);
    let chronology = null;
    let compare = null;
    const syncActivity = () => chronology?.setActivity(traffic || Boolean(ownJob ?? externalJob));
    const client = ClientPanel(ctx, {
      onSelection: (selection) => {
        chronology.setProtocol(selection.protocol);
        gauges.setProtocol(selection.protocol);
        compare.setSelection(selection);
      },
      onActivity: (inFlight, looping) => {
        traffic = inFlight > 0 || looping;
        syncActivity();
      },
      onResult: (response, sentAt) => {
        gauges.record(response);
        compare.record(response);
        chronology.ingestResponse(response, sentAt);
      },
    });
    chronology = Chronology(ctx, {
      footer: client.remote,
      onClear: () => {
        chronology.clear();
        gauges.clear();
      },
    });
    compare = LocalVsRemote(ctx, { onMeasureRequested: () => client.callOnce() });
    client.start();

    function applyBusy() {
      const job = ownJob ?? externalJob;
      conditions.setBusy(job);
      client.setBusy(job);
      compare.setBusy(Boolean(job));
      scenarios.setLocked(ownJob ? null : externalJob);
      syncActivity();
    }

    const scenarios = Scenarios(ctx, {
      onJob: (job) => {
        if (job) lastOwnJobId = job.id ?? job.job_id ?? lastOwnJobId;
        ownJob = job;
        applyBusy();
      },
    });
    openScenarios = scenarios;

    container.append(
      header,
      offline,
      h('div.chaos-layout', h('div.chaos-side', conditions.el, client.el), h('div.chaos-main', chronology.el, h('div.chaos-main__row', gauges.el, compare.el), feed.el)),
      scenarios.el,
    );

    const offs = [
      route.off,
      store.subscribe('api', (state) => (offline.hidden = state !== 'offline'), { immediate: true }),
      store.subscribe(
        (state) => state.stats?.job ?? null,
        (job) => {
          externalJob = job && job.id !== lastOwnJobId ? job : null;
          applyBusy();
        },
        { immediate: true },
      ),
    ];

    return () => {
      if (openScenarios === scenarios) openScenarios = null;
      for (const off of offs) off();
      for (const part of [client, conditions, chronology, gauges, compare, feed, scenarios]) part.destroy();
      const network = store.get().network;
      if (!isDegraded(network)) return;
      toast.warn('Le réseau simulé reste dégradé', {
        description: `${describeNetwork(network).label} · ${conditionsSummary(network)}. Les autres pages en subiront les effets.`,
        duration: 12000,
        action: {
          label: 'Réinitialiser',
          onClick: () =>
            applyPreset('ideal').then(
              () => toast.success('Réseau idéal rétabli'),
              (error) => toast.error('Réinitialisation impossible', { description: error?.message }),
            ),
        },
      });
    };
  },
  /**
   * La page reste montée quand seule la requête de l'URL change : `?scenario=<id>` présente ce scénario.
   * @param {Record<string, string>} query
   * @returns {void}
   */
  onQuery(query) {
    if (query.scenario) openScenarios?.open(query.scenario);
  },
};
