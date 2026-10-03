/**
 * Amorçage : thème, coquille (barre latérale + barre haute + zone de contenu), routeur,
 * liaison avec le laboratoire, commandes et raccourcis.
 *
 * Contrat d'une page (`js/pages/<id>.js`) :
 *
 *     export default {
 *       id, title, subtitle, icon,
 *       mount(container, ctx) { …; return () => { nettoyage }; },   // peut être `async`
 *       onQuery(query) { … }   // facultatif : la page reste montée quand seuls les paramètres d'URL changent
 *     };
 *
 * `ctx = { store, api, ws, navigate, toast, query, onCleanup }` — `query` : paramètres de l'URL
 * (`#/xray?call=…`), `onCleanup(fn)` : enregistre un nettoyage supplémentaire. Le conteneur porte
 * `data-page="<id>"` ; une erreur au chargement ou au montage affiche un état d'erreur, sans
 * casser le reste de l'application.
 */

import { ApiError, api, startRuntime, ws } from './core/api.js';
import { h, clear, qsa } from './core/dom.js';
import { navigate, startRouter } from './core/router.js';
import { DEFAULT_ROUTE, NAV_GROUPS, ROUTES, routeInfo } from './core/routes.js';
import { appStore } from './core/store.js';
import { initTheme } from './core/theme.js';
import { openShortcutsHelp, registerDefaults } from './commands.js';
import { toast } from './components/toast.js';
import { Button, EmptyState } from './components/ui.js';
import { Sidebar } from './shell/sidebar.js';
import { Topbar } from './shell/topbar.js';

const SIDEBAR_KEY = 'rpcx.sidebar';
const APP_NAME = 'RPC Explorer';
const MAX_STAGGERED_CARDS = 14;

function readSidebarState() {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === 'collapsed' ? 'collapsed' : 'expanded';
  } catch {
    return 'expanded';
  }
}

function buildShell() {
  const content = h('main.content', { id: 'content', tabIndex: -1 });
  const sidebar = Sidebar({ onShortcuts: openShortcutsHelp });
  const topbar = Topbar({ onToggleSidebar: toggleSidebar });
  const skip = h('button.skip-link', { type: 'button', onClick: () => content.focus() }, 'Aller au contenu');
  const shell = h('div.shell', { dataset: { sidebar: readSidebarState() } }, skip, sidebar, h('div.main', topbar, h('div.main__body', content)));

  function toggleSidebar() {
    const next = shell.dataset.sidebar === 'collapsed' ? 'expanded' : 'collapsed';
    shell.dataset.sidebar = next;
    try {
      localStorage.setItem(SIDEBAR_KEY, next);
    } catch {
      /* stockage indisponible : l'état vaut pour la session */
    }
  }

  appStore.subscribe('wiretap', (open) => {
    shell.dataset.wiretap = open ? 'open' : 'closed';
  }, { immediate: true });

  return { shell, content, sidebar, topbar, toggleSidebar };
}

/** Monte les pages dans la zone de contenu et gère leur cycle de vie. */
function createPageHost({ content, sidebar, topbar }) {
  const groupLabel = Object.fromEntries(NAV_GROUPS.map((group) => [group.id, group.label]));
  let mounted = null;
  let generation = 0;

  function unmount() {
    if (!mounted) return;
    const { cleanups } = mounted;
    mounted = null;
    for (const cleanup of cleanups.reverse()) {
      try {
        cleanup();
      } catch {
        /* le nettoyage d'une page ne doit jamais bloquer la navigation */
      }
    }
  }

  function present(info, page) {
    topbar.setPage({ title: page?.title ?? info.label, subtitle: page?.subtitle ?? info.subtitle, group: groupLabel[info.group] });
    document.title = `${page?.title ?? info.label} · ${APP_NAME}`;
  }

  function failure(info, error) {
    return EmptyState({
      size: 'lg',
      tone: 'danger',
      icon: 'bug',
      title: `La page « ${info.label} » n’a pas pu s’afficher`,
      text: error?.message ?? String(error),
      action: Button({ label: 'Recharger', icon: 'refresh-cw', onClick: () => window.location.reload() }),
    });
  }

  async function show(route) {
    const info = routeInfo(route.id);
    if (mounted?.id === route.id && typeof mounted.page.onQuery === 'function') {
      mounted.page.onQuery(route.query);
      return;
    }

    generation += 1;
    const ticket = generation;
    unmount();
    sidebar.setActive(route.id);
    present(info, null);

    let page = null;
    let error = null;
    try {
      page = (await info.load()).default;
    } catch (cause) {
      console.error(`[page ${route.id}] chargement impossible`, cause);
      error = cause;
    }
    if (ticket !== generation) return;
    if (!page && !error) error = new Error('Le module de page n’a pas d’export par défaut.');

    const container = h('div.page', { dataset: { page: route.id, entering: '' } });
    const cleanups = [];
    clear(content, container);
    window.scrollTo(0, 0);

    const fail = (cause) => {
      console.error(`[page ${route.id}] erreur au montage`, cause);
      clear(container, failure(info, cause));
    };

    if (page) {
      present(info, page);
      const ctx = { store: appStore, api, ws, navigate, toast, query: route.query, onCleanup: (fn) => cleanups.push(fn) };
      try {
        // `mount` peut être synchrone ou asynchrone ; dans les deux cas il peut rendre une fonction de nettoyage.
        Promise.resolve(page.mount(container, ctx)).then((cleanup) => {
          if (typeof cleanup !== 'function') return;
          if (ticket === generation) cleanups.push(cleanup);
          else cleanup();
        }, fail);
      } catch (cause) {
        fail(cause);
      }
    } else {
      clear(container, failure(info, error));
    }

    qsa('.card', container)
      .slice(0, MAX_STAGGERED_CARDS)
      .forEach((card, index) => card.style.setProperty('--i', String(index)));
    window.setTimeout(() => delete container.dataset.entering, 900);
    mounted = { id: route.id, page: page ?? {}, cleanups };
  }

  return { show };
}

/**
 * Filet de sécurité : une `ApiError` qu'une page aurait oublié d'attraper ne finit jamais en
 * erreur de console — silencieuse si le laboratoire est hors ligne (l'état est déjà affiché),
 * signalée par une notification sinon.
 */
function catchStrayApiErrors() {
  window.addEventListener('unhandledrejection', (event) => {
    if (!(event.reason instanceof ApiError)) return;
    event.preventDefault();
    if (!event.reason.offline) toast.error('Requête refusée par le laboratoire', { description: event.reason.message });
  });
}

function boot() {
  initTheme();
  catchStrayApiErrors();
  const parts = buildShell();
  clear(document.getElementById('app'), parts.shell);
  registerDefaults({ toggleSidebar: parts.toggleSidebar });

  const host = createPageHost(parts);
  startRouter({ ids: ROUTES.map((route) => route.id), fallback: DEFAULT_ROUTE, onChange: (route) => host.show(route) });
  startRuntime();
}

boot();
