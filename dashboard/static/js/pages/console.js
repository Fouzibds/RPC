/**
 * Console RPC — le terrain de jeu : lancer n'importe quelle procédure par n'importe quel
 * middleware, en synchrone, en lot asynchrone ou en flux, et lire l'issue comme un ingénieur.
 *
 * Volet gauche : la requête (formulaire généré depuis le catalogue) et le code Python équivalent.
 * Volet droit : le résultat, puis l'historique de la session.
 */

import { h, clear, on } from '../core/dom.js';
import { refreshCatalog, refreshStatus } from '../core/api.js';
import { hasBlockingLayer } from '../core/layers.js';
import { CodeBlock } from '../components/codeblock.js';
import { Badge, Button, Callout, Card, EmptyState, PageHeader, Skeleton } from '../components/ui.js';
import { BatchResult } from './console/batch.js';
import { ConfigForm, endpointOf } from './console/form.js';
import { HISTORY_LIMIT, HistoryPanel, loadHistory, saveHistory } from './console/history.js';
import { applyQuery, buildRequest, initialConfig, methodSpec, normalize, snapshot } from './console/model.js';
import { buildSnippet } from './console/snippet.js';
import { SNIPPET_NOTES, describeRequest, formSkeleton, requestBudgetMs, resultSkeleton } from './console/states.js';
import { StreamResult } from './console/stream.js';
import { SyncResult } from './console/sync.js';

const MUTATING = new Set(['update_stock', 'bulk_update_stock']);

/** Page active, pour appliquer un changement de chaîne de requête sans la remonter. */
let active = null;

function mountConsole(container, ctx) {
  const { store, api, ws, toast } = ctx;
  const cleanups = [];
  let disposables = [];
  let catalog = null;
  let config = null;
  let form = null;
  let snippet = null;
  let running = false;
  let stream = null; // { id: string|null, view: StreamResult|null, buffer: Object[] }
  let history = loadHistory();
  let activeEntry = null;

  /* --- Squelette de la page ------------------------------------------------------------ */
  const offlineSlot = h('div.console-offline', { hidden: true });
  const formSlot = h('div.console-slot');
  const codeNote = h('p.console-code__note');
  const codeSlot = h('div.console-slot');
  const codeCard = Card({ title: 'Code équivalent', subtitle: 'Le Python qui produit exactement cet appel, mis à jour à chaque réglage', icon: 'code-xml', class: 'console-code' }, codeSlot, codeNote);
  const resultBody = h('div.console-result-body', { 'aria-live': 'polite' });
  const resultActions = h('div.console-output__actions');
  const resultCard = Card({ title: 'Résultat', subtitle: 'Rien n’a encore été exécuté', icon: 'terminal', actions: resultActions, class: 'console-output' }, resultBody);
  const historyPanel = HistoryPanel({
    getCatalog: () => catalog,
    onLoad: (entry) => loadEntry(entry),
    onRerun: (entry) => {
      loadEntry(entry);
      run();
    },
    onClear: () => {
      history = [];
      activeEntry = null;
      saveHistory(history);
      historyPanel.set(history);
    },
  });

  codeCard.hidden = true;
  const meta = h('div.console-meta');

  container.append(
    PageHeader({
      eyebrow: 'Explorer',
      title: 'Console RPC',
      icon: 'square-terminal',
      description: 'Lancez n’importe quelle procédure par n’importe quel middleware — un appel, N appels de front ou un flux — puis lisez l’issue : résultat, durée, octets sur le fil, étapes.',
      meta,
    }),
    offlineSlot,
    h('div.console', h('div.console__config', formSlot, codeCard), h('div.console__main', resultCard, historyPanel)),
  );

  /* --- Résultat --------------------------------------------------------------------------- */
  const track = (node) => {
    disposables.push(node);
    return node;
  };

  /** Remplace le contenu du volet de résultat ; `build` n'est appelé qu'une fois l'ancien contenu détruit. */
  function setResult(subtitle, build) {
    for (const node of disposables) node.destroy?.();
    disposables = [];
    resultCard.setSubtitle(subtitle);
    setTraceLink('');
    clear(resultBody, typeof build === 'function' ? build() : build);
  }

  /** Lien « Ouvrir dans Sous le capot » de l'appel affiché (retiré si l'appel n'a pas de trace). */
  function setTraceLink(callId) {
    clear(resultActions, callId ? Button({ label: 'Ouvrir dans Sous le capot', size: 'sm', variant: 'ghost', iconRight: 'arrow-right', href: `#/xray?call=${encodeURIComponent(callId)}` }) : null);
  }

  function showIdle() {
    setResult(
      'Rien n’a encore été exécuté',
      EmptyState({
        icon: 'play',
        tone: 'accent',
        title: 'Prêt à appeler',
        text: 'Réglez la requête à gauche puis exécutez-la : le résultat, sa durée, les octets échangés et les étapes de l’appel s’affichent ici.',
        action: Button({ label: 'Exécuter l’appel', variant: 'primary', icon: 'play', kbd: 'mod+enter', onClick: () => run() }),
      }),
    );
  }

  function showFailure(request, error) {
    setResult(
      describeRequest(request),
      Callout({
        tone: error.offline ? 'warning' : 'danger',
        icon: error.offline ? 'cloud-off' : undefined,
        title: error.offline ? 'Laboratoire injoignable' : 'La requête a été refusée par le laboratoire',
        text: error.offline ? 'L’appel n’a pas pu être transmis : le serveur du dashboard ne répond pas. Relancez-le avec « python main.py --dashboard », puis réessayez.' : error.message,
        actions: Button({ label: 'Réessayer', size: 'sm', icon: 'refresh-cw', onClick: () => run() }),
      }),
    );
  }

  /* --- Historique -------------------------------------------------------------------------- */
  function remember(snap, outcome) {
    const entry = { id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`, at: Date.now(), config: snap, ...outcome };
    history = [entry, ...history].slice(0, HISTORY_LIMIT);
    activeEntry = entry.id;
    saveHistory(history);
    historyPanel.set(history, activeEntry);
  }

  function loadEntry(entry) {
    if (!form) return;
    Object.assign(config, structuredClone(entry.config), { values: { ...config.values, ...structuredClone(entry.config.values) } });
    activeEntry = entry.id;
    form.sync();
    updateSnippet();
    historyPanel.set(history, activeEntry);
  }

  /* --- Code équivalent ------------------------------------------------------------------------ */
  function updateSnippet() {
    if (!form || !form.isValid()) return;
    const spec = methodSpec(catalog, config.method);
    const { status } = store.get();
    const { code, title } = buildSnippet({ config, spec, endpoint: endpointOf(status, config.protocol, config.viaProxy), workers: catalog.limits?.async_workers });
    if (snippet) snippet.update({ code, title });
    else {
      snippet = CodeBlock({ code, title, language: 'python', maxHeight: 360, wrap: true });
      clear(codeSlot, snippet);
    }
    clear(codeNote, config.policy.enabled ? 'Avec une politique, le client du laboratoire est enveloppé : le code appelant ne change pas, les pannes du réseau y sont enfin traitées.' : SNIPPET_NOTES[config.protocol]);
  }

  /* --- Exécution -------------------------------------------------------------------------------- */
  function finish() {
    running = false;
    form?.setBusy(null);
  }

  async function run() {
    if (!form || running || !form.isValid()) return;
    const request = buildRequest(config, catalog);
    const snap = snapshot(config);
    const spec = methodSpec(catalog, request.method);

    if (request.mode === 'stream' && ws.state !== 'live') {
      setResult(
        describeRequest(request),
        Callout({
          tone: 'warning',
          icon: 'wifi-off',
          title: 'Flux temps réel déconnecté',
          text: 'Les éléments d’un flux sont relayés par le WebSocket du dashboard, qui n’est pas connecté pour l’instant. Il se reconnecte seul dès que le laboratoire répond.',
          actions: Button({ label: 'Réessayer', size: 'sm', icon: 'refresh-cw', onClick: () => refreshStatus().then(() => run()) }),
        }),
      );
      return;
    }

    running = true;
    form.setBusy(request.mode === 'stream' ? 'Flux en cours…' : request.mode === 'async' ? `${request.count} appels en cours…` : 'Appel en cours…');
    setResult(describeRequest(request), resultSkeleton());
    if (request.mode === 'stream') stream = { id: null, view: null, buffer: [] };

    let response;
    try {
      response = await api.post('/api/call', request, { timeoutMs: requestBudgetMs(request) });
    } catch (error) {
      stream = null;
      finish();
      showFailure(request, error);
      return;
    }

    if (request.mode === 'stream') {
      const view = StreamResult({
        request,
        spec,
        track,
        onDone: (summary) => {
          stream = null;
          finish();
          setTraceLink(summary.callId);
          remember(snap, { ok: summary.ok, code: summary.code, durationMs: summary.durationMs, items: summary.count });
          if (summary.ok && MUTATING.has(request.method)) refreshCatalog();
        },
      });
      const buffered = stream.buffer;
      stream = { id: response.stream_id, view, buffer: [] };
      setResult(describeRequest(request), () => track(view));
      for (const message of buffered) if (message.stream_id === response.stream_id) view.handle(message);
      return;
    }

    finish();
    if (request.mode === 'async') {
      setResult(describeRequest(request), () => BatchResult({ response }));
      const firstError = response.calls?.find((call) => !call.ok)?.error;
      remember(snap, { ok: response.ok, code: response.ok ? 'OK' : (firstError?.code ?? 'ERREUR'), durationMs: response.wall_ms, errors: response.errors });
    } else {
      setResult(describeRequest(request), () => SyncResult({ response, request, catalog, api, track }));
      setTraceLink(response.call_id);
      remember(snap, { ok: response.ok, code: response.ok ? 'OK' : (response.error?.code ?? 'ERREUR'), durationMs: response.duration_ms });
    }
    if (MUTATING.has(request.method)) refreshCatalog();
  }

  /* --- Catalogue : chargement, erreur, hors ligne ------------------------------------------------ */
  function showFormState(node) {
    clear(formSlot, Card({ title: 'Requête', subtitle: 'Quoi appeler, par quel middleware, et comment', icon: 'sliders-horizontal', class: 'console-form' }, node));
  }

  /** Puces de l'en-tête : ce que le catalogue met à disposition de la console. */
  function summarize() {
    const streams = catalog.methods.filter((spec) => spec.kind !== 'unary').length;
    clear(
      meta,
      Badge({ label: `${catalog.methods.length} procédures au catalogue`, icon: 'list' }),
      streams ? Badge({ label: `dont ${streams} en flux`, icon: 'activity' }) : null,
      Badge({ label: `${(catalog.protocols ?? []).length} protocoles`, icon: 'network' }),
    );
  }

  function buildForm(nextCatalog) {
    catalog = structuredClone(nextCatalog);
    summarize();
    config = normalize(initialConfig(catalog), catalog);
    applyQuery(config, ctx.query, catalog);
    form = ConfigForm({
      config,
      catalog,
      getLab: () => store.get(),
      onChange: () => {
        activeEntry = null;
        updateSnippet();
      },
      onRun: () => run(),
    });
    clear(formSlot, form);
    codeCard.hidden = false;
    updateSnippet();
    historyPanel.set(history, activeEntry);
    showIdle();
  }

  let catalogFailed = false;
  async function retryCatalog() {
    catalogFailed = false;
    renderCatalogState();
    const online = await refreshStatus();
    catalogFailed = online && !(await refreshCatalog());
    renderCatalogState();
  }

  function renderCatalogState() {
    const { catalog: loaded, api: apiState } = store.get();
    offlineSlot.hidden = !(form && apiState === 'offline');
    if (form) {
      if (loaded?.products) form.setProducts(loaded.products);
      return;
    }
    if (loaded?.methods?.length) {
      buildForm(loaded);
      return;
    }
    const retry = Button({ label: 'Réessayer', size: 'sm', icon: 'refresh-cw', onClick: retryCatalog });
    const waiting = apiState !== 'offline' && !catalogFailed;
    clear(meta, waiting ? [Skeleton({ width: 176, height: 22, variant: 'block' }), Skeleton({ width: 104, height: 22, variant: 'block' })] : null);
    if (apiState === 'offline') {
      showFormState(Callout({ tone: 'warning', icon: 'cloud-off', title: 'Laboratoire injoignable', text: 'Le catalogue des procédures n’a pas pu être chargé. Lancez « python main.py --dashboard » : la console se reconnecte seule.', actions: retry }));
      setResult('Laboratoire injoignable', EmptyState({ icon: 'cloud-off', tone: 'warning', title: 'Hors ligne', text: 'Aucun appel ne peut partir tant que le laboratoire ne répond pas.', action: Button({ label: 'Réessayer', icon: 'refresh-cw', onClick: retryCatalog }) }));
    } else if (catalogFailed) {
      showFormState(Callout({ tone: 'danger', title: 'Catalogue indisponible', text: 'Le laboratoire répond, mais GET /api/catalog a échoué : sans lui, le formulaire ne peut pas être généré.', actions: retry }));
      setResult('Catalogue indisponible', EmptyState({ icon: 'circle-alert', tone: 'danger', title: 'Formulaire indisponible', text: 'La console se construit à partir du catalogue des procédures.' }));
    } else {
      showFormState(formSkeleton());
      setResult('Chargement du catalogue…', resultSkeleton());
    }
  }

  clear(offlineSlot, Callout({ tone: 'warning', icon: 'cloud-off', title: 'Laboratoire injoignable', text: 'Le dashboard ne répond plus : les appels échoueront jusqu’à son retour. La reconnexion est automatique.' }));
  historyPanel.set(history);

  cleanups.push(
    store.subscribe('catalog', renderCatalogState),
    store.subscribe('api', () => {
      renderCatalogState();
      if (!form && store.get().api === 'online' && !store.get().catalog) retryCatalog();
    }),
    store.subscribe('status', () => {
      form?.refreshLab();
      updateSnippet();
    }),
    store.subscribe('network', () => form?.refreshLab()),
    ws.on('stream', routeStream),
    ws.on('stream_end', routeStream),
    ws.on('close', () => stream?.view?.interrupt('La connexion temps réel a été perdue pendant le flux : la suite des éléments ne peut plus être affichée.')),
    on(document, 'keydown', (event) => {
      if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey) || event.altKey || hasBlockingLayer()) return;
      event.preventDefault();
      if (running) toast.info('Un appel est déjà en cours', { description: 'Attendez sa fin, ou cessez de suivre le flux.' });
      else run();
    }),
  );

  function routeStream(message) {
    if (!stream) return;
    if (stream.id === null) stream.buffer.push(message);
    else if (message.stream_id === stream.id) stream.view.handle(message);
  }

  renderCatalogState();
  if (!store.get().catalog && store.get().api === 'online') retryCatalog();

  return {
    applyQuery(query) {
      if (!form || !applyQuery(config, query, catalog)) return;
      form.sync();
      updateSnippet();
    },
    destroy() {
      cleanups.forEach((off) => off());
      stream?.view?.destroy();
      stream = null;
      for (const node of disposables) node.destroy?.();
      disposables = [];
      snippet?.destroy();
      form?.destroy();
    },
  };
}

export default {
  id: 'console',
  title: 'Console RPC',
  subtitle: 'Appeler une procédure distante, protocole par protocole',
  icon: 'square-terminal',
  /**
   * @param {HTMLElement} container Conteneur vide fourni par la coquille.
   * @param {{store: Object, api: Object, ws: Object, navigate: Function, toast: Object, query: Record<string, string>, onCleanup: Function}} ctx
   * @returns {() => void} Nettoyage appelé quand on quitte la page.
   */
  mount(container, ctx) {
    const page = mountConsole(container, ctx);
    active = page;
    return () => {
      page.destroy();
      if (active === page) active = null;
    };
  },
  /**
   * Chaîne de requête modifiée sans changement de page (`#/console?protocol=grpc&method=update_stock`).
   * @param {Record<string, string>} query
   * @returns {void}
   */
  onQuery(query) {
    active?.applyQuery(query);
  },
};
