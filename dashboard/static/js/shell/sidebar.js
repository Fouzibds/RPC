/**
 * Barre latérale : marque, navigation groupée, état en direct des trois serveurs, version.
 */

import { h, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../core/protocols.js';
import { href } from '../core/router.js';
import { NAV_GROUPS, routesOf } from '../core/routes.js';
import { appStore } from '../core/store.js';
import { Kbd } from '../components/ui/badges.js';
import { IconButton } from '../components/ui/buttons.js';
import { Tooltip } from '../components/ui/tooltip.js';
import { LogoMark } from './logo.js';

/** Version affichée tant que le laboratoire n'a pas répondu. */
const FALLBACK_VERSION = '1.0.0';

function navItem(route) {
  const item = h(
    'a.nav-item',
    { href: href(route.id), dataset: { route: route.id } },
    icon(route.icon, { size: 16 }),
    h('span.nav-item__label', route.label),
    route.key ? h('span.nav-item__kbd', Kbd(`g ${route.key}`, { size: 'sm' })) : null,
  );
  Tooltip(item, () => (item.closest('[data-sidebar="collapsed"]') ? route.label : null), { placement: 'right', kbd: route.key ? `g ${route.key}` : '', delay: 120 });
  return item;
}

function serverRows(list) {
  const known = new Map(list.map((server) => [server.id, server]));
  return REMOTE_PROTOCOL_IDS.map((id) => {
    const info = protocol(id);
    const server = known.get(id);
    const state = !server ? 'unknown' : server.up ? 'up' : 'down';
    const port = server?.port ?? info.port;
    const stateLabel = { up: 'en service', down: 'arrêté', unknown: 'état inconnu' }[state];
    const row = h(
      'li.server',
      { dataset: { state }, style: { '--proto': info.color } },
      h('span.server__swatch', { 'aria-hidden': 'true' }),
      h('span.server__name', server?.label ?? info.label),
      h('span.server__port.num', `:${port}`),
      h('span.server__state', { role: 'img', 'aria-label': stateLabel }),
    );
    Tooltip(row, `${server?.label ?? info.label} · port ${port} · ${stateLabel}`, { placement: 'right', delay: 200 });
    return row;
  });
}

/**
 * Construit la barre latérale.
 * @param {{onShortcuts: () => void}} options `onShortcuts` ouvre l'aide des raccourcis.
 * @returns {HTMLElement & {setActive: (routeId: string) => void, destroy: () => void}}
 */
export function Sidebar({ onShortcuts }) {
  const items = new Map();
  const nav = h(
    'nav.sidebar__nav.scroll-y',
    { 'aria-label': 'Navigation principale' },
    NAV_GROUPS.map((group) =>
      h(
        'div.nav-group',
        h('div.nav-group__label.t-label', group.label),
        h(
          'ul.nav-group__list',
          routesOf(group.id).map((route) => {
            const item = navItem(route);
            items.set(route.id, item);
            return h('li', item);
          }),
        ),
      ),
    ),
  );

  const servers = h('ul.server-list');
  const upCount = h('span.sidebar__count.num');
  const version = h('span.sidebar__version.num');

  const el = h(
    'aside.sidebar',
    h(
      'div.sidebar__inner',
      h(
        'a.brand',
        { href: href('overview'), 'aria-label': 'RPC Explorer — Vue d’ensemble' },
        h('span.brand__mark', LogoMark()),
        h('span.brand__text', h('span.brand__name', 'RPC Explorer'), h('span.brand__tagline', 'Benchmark Lab')),
      ),
      nav,
      h(
        'div.sidebar__footer',
        h('div.sidebar__footer-head', h('span.t-label', 'Serveurs'), upCount),
        servers,
        h(
          'div.sidebar__meta',
          version,
          IconButton({ icon: 'keyboard', label: 'Raccourcis clavier', size: 'sm', kbd: '?', tooltip: 'top', onClick: onShortcuts }),
        ),
      ),
    ),
  );

  function render() {
    const { status, api } = appStore.get();
    const list = api === 'online' ? (status?.servers ?? []) : [];
    clear(servers, serverRows(list));
    // Le compteur porte sur les trois serveurs listés (custom, grpc, rest), pas sur les serveurs « contrat v2 ».
    const total = REMOTE_PROTOCOL_IDS.length;
    const up = list.filter((server) => server.up && REMOTE_PROTOCOL_IDS.includes(server.id)).length;
    upCount.textContent = list.length ? `${up}/${total}` : '';
    upCount.dataset.tone = list.length && up === total ? 'success' : list.length ? 'danger' : 'muted';
    version.textContent = `v${status?.app?.version ?? FALLBACK_VERSION}`;
  }

  // Ne redessine que si l'état affiché change réellement (le magasin est rafraîchi chaque seconde).
  const signature = (state) =>
    JSON.stringify([state.api, state.status?.app?.version, (state.status?.servers ?? []).map((server) => [server.id, server.label, server.port, server.up])]);
  const unsubscribe = appStore.subscribe(signature, render, { immediate: true });

  el.setActive = (routeId) => {
    items.forEach((item, id) => {
      if (id === routeId) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    });
  };
  el.destroy = unsubscribe;
  return el;
}
