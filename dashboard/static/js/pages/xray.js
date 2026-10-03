/**
 * Sous le capot — un appel de procédure, disséqué : stub, marshalling, tramage, transport,
 * squelette, dispatch, exécution et retour, avec les octets et les durées réellement mesurés.
 * La page lance une inspection au chargement (`POST /api/inspect`) ou relit une trace existante
 * (`#/xray?call=<id>` → `GET /api/traces/{id}`). `#/xray?protocol=<custom|grpc|rest>` ouvre
 * l'inspection sur ce protocole.
 */

import { h, clear, on } from '../core/dom.js';
import { refreshCatalog } from '../core/api.js';
import { hasBlockingLayer } from '../core/layers.js';
import { REMOTE_PROTOCOL_IDS } from '../core/protocols.js';
import { Badge, Button, Callout, Card, EmptyState, PageHeader, Section, Skeleton } from '../components/ui.js';
import { CommandBar } from './xray/command.js';
import { Compare } from './xray/compare.js';
import { Glossary } from './xray/glossary.js';
import { Hero } from './xray/hero.js';
import { buildModel } from './xray/model.js';
import { Timeline } from './xray/timeline.js';
import { Wire } from './xray/wire.js';

/** Contrôles qui gardent pour eux la barre d'espace et les flèches. */
const TYPING = 'input, textarea, select, [contenteditable], [role="radio"], [role="tab"], [role="slider"], [role="listbox"], [role="option"], [role="menu"], [role="tree"], [role="dialog"]';

let mounted = null;

function skeleton() {
  return h(
    'div.xr-loading',
    { 'aria-busy': 'true', 'aria-label': 'Inspection en cours' },
    Card({ padding: 'md' }, Skeleton({ variant: 'block', height: 34, width: '56%' }), Skeleton({ variant: 'block', height: 208 }), Skeleton({ variant: 'block', height: 30 }), Skeleton({ lines: 3 })),
    h('div.xr-loading__row', Card({ padding: 'md' }, Skeleton({ variant: 'block', height: 220 })), Card({ padding: 'md' }, Skeleton({ variant: 'block', height: 220 }))),
    Card({ padding: 'md' }, Skeleton({ variant: 'block', height: 180 })),
  );
}

function mount(container, ctx) {
  const { store, api, toast } = ctx;
  const abort = new AbortController();
  const cleanups = [];
  let disposed = false;
  let ticket = 0;
  let bar = null;
  let sections = null;
  let models = [];
  let active = REMOTE_PROTOCOL_IDS.includes(ctx.query.protocol) ? ctx.query.protocol : null;
  let offline = false;
  let retry = null;

  const barSlot = h('div.xr-bar-slot', Card({ padding: 'sm' }, Skeleton({ variant: 'block', height: 52 })));
  const notice = h('div.xr-notice', { 'aria-live': 'polite' });
  const results = h('div.xr-results', skeleton());
  const meta = h('div.xr-meta', Skeleton({ width: 168, height: 22, variant: 'block' }), Skeleton({ width: 184, height: 22, variant: 'block' }));
  container.append(
    PageHeader({
      eyebrow: 'Explorer',
      title: 'Sous le capot',
      icon: 'scan-search',
      description: 'Un appel de procédure, disséqué. Ce que le middleware fait à votre place entre l’appel d’une fonction et son résultat — avec les octets et les durées d’un appel qui vient d’avoir lieu.',
      meta,
    }),
    barSlot,
    notice,
    results,
  );

  /* --- Sections de résultat (créées au premier résultat) ------------------ */

  function buildSections() {
    const hero = Hero({
      onProtocol: (id) => setActive(id, { refocus: true }),
      onStep: (step, model, playing) => sections?.timeline.setActive(step.stage, playing),
    });
    const scrollToHero = () => hero.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    const timeline = Timeline({ onSelect: (stage) => hero.journey.goToStage(stage), onProtocol: (id) => setActive(id) });
    const wire = Wire({ onProtocol: (id) => setActive(id) });
    const compare = Compare({
      onPick: (id) => {
        setActive(id);
        scrollToHero();
      },
    });
    const glossary = Glossary({
      onStage: (stage) => {
        hero.journey.goToStage(stage);
        scrollToHero();
      },
    });
    clear(
      results,
      Section({ title: 'Le voyage d’un appel', description: 'Que se passe-t-il entre l’appel d’une fonction et son résultat ? Douze étapes, toujours les mêmes : seuls les octets changent d’un protocole à l’autre.' }, hero),
      Section({ title: 'Chronologie réelle', description: 'Où passe le temps ? Les durées de chaque étape, en microsecondes, telles que le stub et le squelette les ont chronométrées.' }, timeline),
      Section({ title: 'Les octets sur le fil', description: 'Qu’est-ce qui circule vraiment sur le réseau ? La requête et la réponse, octet par octet.' }, wire),
      Section({ title: 'Trois protocoles, un même appel', description: 'Que change le choix du protocole ? Le même appel, côte à côte.' }, compare),
      glossary,
    );
    cleanups.push(() => [hero, timeline, wire, compare].forEach((part) => part.destroy()));
    return { hero, timeline, wire, compare };
  }

  function renderActive(options = {}) {
    const model = models.find((item) => item.protocol === active) ?? models[0];
    const protocols = models.map((item) => item.protocol);
    sections.hero.update(models, model.protocol, options);
    sections.timeline.update(model, protocols);
    sections.wire.update(model, protocols);
    sections.compare.update(models, model.protocol);
  }

  function setActive(id, options = {}) {
    if (id === active || !models.some((model) => model.protocol === id)) return;
    active = id;
    renderActive({ keepStage: true, ...options });
  }

  /** `?protocol=<id>` : affiche ce protocole, quitte à relancer l'inspection s'il n'a pas été appelé. */
  function openProtocol(id) {
    if (!REMOTE_PROTOCOL_IDS.includes(id)) return;
    if (models.some((model) => model.protocol === id)) {
      setActive(id, { refocus: true });
      return;
    }
    active = id;
    if (bar?.includeProtocol(id) && bar.canRun()) inspect();
  }

  /* --- États ---------------------------------------------------------------- */

  function setNotice(node) {
    clear(notice, node);
  }

  function showOffline(again) {
    offline = true;
    retry = again;
    sections = null;
    models = [];
    setNotice(null);
    clear(
      results,
      Card(
        EmptyState({
          icon: 'unplug',
          tone: 'warning',
          size: 'lg',
          title: 'Laboratoire injoignable',
          text: 'Sans les serveurs RPC, aucun appel à disséquer. Relancez « python main.py --dashboard » : la page reprendra d’elle-même dès que le laboratoire répondra.',
          action: Button({ label: 'Réessayer', icon: 'refresh-cw', onClick: () => again() }),
        }),
      ),
    );
  }

  function showError(error, again) {
    if (error.offline) {
      showOffline(again);
      return;
    }
    const callout = Callout({
      tone: 'danger',
      title: 'L’inspection n’a pas abouti',
      text: error.message,
      actions: Button({ label: 'Réessayer', size: 'sm', icon: 'refresh-cw', onClick: () => again() }),
    });
    setNotice(callout);
    if (!sections) {
      clear(
        results,
        Card(EmptyState({ icon: 'scan-search', title: 'Aucun appel à disséquer', text: 'Corrigez la demande ci-dessus, puis relancez l’inspection.', action: Button({ label: 'Inspecter', variant: 'primary', icon: 'scan-search', onClick: () => bar?.submit() }) })),
      );
    }
  }

  function showEmpty(text) {
    sections = null;
    models = [];
    clear(
      results,
      Card(EmptyState({ icon: 'scan-search', size: 'lg', title: 'Aucune trace à afficher', text, action: Button({ label: 'Inspecter', variant: 'primary', icon: 'scan-search', kbd: 'mod+enter', onClick: () => bar?.submit() }) })),
    );
  }

  function show(traces, { viaProxy = false, autoplay = true, info = null } = {}) {
    const catalog = store.get().catalog;
    const built = traces.map((trace) => ({ ...buildModel(trace, catalog), viaProxy }));
    const usable = built.filter((model) => model.events.length);
    const silent = built.filter((model) => !model.events.length).map((model) => model.protocol);
    offline = false;
    if (!usable.length) {
      setNotice(null);
      showEmpty('L’appel n’a publié aucun évènement : le bus de traces était coupé (un banc d’essai est peut-être en cours). Relancez l’inspection dans un instant.');
      return;
    }
    models = usable;
    if (!models.some((model) => model.protocol === active)) active = models[0].protocol;
    if (!sections) sections = buildSections();
    setNotice(
      info ??
        (silent.length
          ? Callout({ tone: 'warning', text: `Aucune trace pour ${silent.join(', ')} : le bus de traces était coupé pendant l’appel.` })
          : null),
    );
    renderActive({ autoplay });
  }

  /* --- Chargements ---------------------------------------------------------- */

  async function guarded(work, again) {
    const mine = (ticket += 1);
    bar?.setBusy(true);
    results.setAttribute('aria-busy', 'true');
    try {
      const outcome = await work();
      if (disposed || mine !== ticket) return;
      outcome();
    } catch (error) {
      if (disposed || mine !== ticket) return;
      showError(error, again);
    } finally {
      if (!disposed && mine === ticket) {
        bar?.setBusy(false);
        results.removeAttribute('aria-busy');
      }
    }
  }

  function inspect(request = bar?.value()) {
    if (!request?.method) return;
    return guarded(async () => {
      const data = await api.post('/api/inspect', request, { signal: abort.signal });
      // Une écriture change les stocks : le catalogue du magasin est rechargé pour les prochains appels.
      if (store.get().catalog?.methods.find((method) => method.name === request.method)?.idempotent === false) refreshCatalog();
      return () => show(data.traces ?? [], { viaProxy: Boolean(data.via_proxy) });
    }, () => inspect(request));
  }

  function loadTrace(callId) {
    return guarded(async () => {
      const data = await api.get(`/api/traces/${encodeURIComponent(callId)}`, { signal: abort.signal });
      return () => {
        const kwargs = data.events?.find((event) => event.stage === 'client.call')?.detail?.kwargs ?? {};
        const known = bar?.setRequest(data.summary?.method, kwargs);
        show([data], {
          info: Callout({
            tone: 'info',
            title: `Trace ${callId} relue depuis le collecteur`,
            text: 'Un seul protocole est affiché : celui de l’appel d’origine.',
            actions: known ? Button({ label: 'Rejouer sur les trois protocoles', size: 'sm', icon: 'scan-search', onClick: () => inspect() }) : null,
          }),
        });
      };
    }, () => loadTrace(callId));
  }

  function start() {
    const catalog = store.get().catalog;
    if (!catalog || bar) return;
    bar = CommandBar({ catalog, store, onRun: (request) => inspect(request), onHint: (message) => toast.info(message) });
    cleanups.push(() => bar.destroy());
    clear(barSlot, bar);
    clear(
      meta,
      Badge({ label: `${(catalog.pipeline ?? []).length} étapes tracées par appel`, icon: 'route' }),
      Badge({ label: `${REMOTE_PROTOCOL_IDS.length} protocoles côte à côte`, icon: 'columns-2' }),
    );
    if (ctx.query.call) loadTrace(ctx.query.call);
    else inspect();
  }

  /** Le catalogue est rechargé à l'ouverture : la barre de commande propose un produit encore en stock. */
  async function boot() {
    const loaded = await refreshCatalog();
    if (disposed) return;
    if (loaded || store.get().catalog) start();
    else {
      clear(meta);
      showOffline(boot);
    }
  }

  /* --- Clavier et reprise ----------------------------------------------------- */

  cleanups.push(
    on(document, 'keydown', (event) => {
      if (event.defaultPrevented || hasBlockingLayer()) return;
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        bar?.submit();
        return;
      }
      if (!sections || event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest(TYPING)) return;
      const journey = sections.hero.journey;
      if (event.key === ' ' && !target?.closest('button, a, summary')) {
        event.preventDefault();
        journey.toggle();
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        journey.next();
      } else if (event.key === 'ArrowLeft') {
        event.preventDefault();
        journey.previous();
      }
    }),
    store.subscribe('api', (state) => {
      if (state === 'online' && offline && retry) retry();
    }),
    store.subscribe('catalog', () => start()),
  );

  mounted = { loadTrace, openProtocol };
  boot();

  return () => {
    disposed = true;
    mounted = null;
    abort.abort();
    cleanups.forEach((cleanup) => cleanup());
  };
}

export default {
  id: 'xray',
  title: 'Sous le capot',
  subtitle: 'Du stub au squelette : chaque étape, chaque octet',
  icon: 'scan-search',
  /**
   * @param {HTMLElement} container Conteneur vide fourni par la coquille.
   * @param {{store: Object, api: Object, ws: Object, navigate: Function, toast: Object, query: Record<string, string>}} ctx
   * @returns {() => void} Nettoyage appelé quand on quitte la page.
   */
  mount,
  /**
   * La page reste montée quand seule la requête de l'URL change : `?call=<id>` relit une trace,
   * `?protocol=<id>` affiche ce protocole.
   * @param {Record<string, string>} query
   */
  onQuery(query) {
    if (query.call) mounted?.loadTrace(query.call);
    else if (query.protocol) mounted?.openProtocol(query.protocol);
  },
};
