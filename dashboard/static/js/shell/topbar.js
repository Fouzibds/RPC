/**
 * Barre haute : titre de la page, déclencheur de la palette, conditions réseau,
 * bascule « Sous le capot », thème, témoin de connexion temps réel.
 */

import { h, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { describeNetwork } from '../core/network.js';
import { href } from '../core/router.js';
import { appStore } from '../core/store.js';
import { getTheme, onThemeChange, toggleTheme } from '../core/theme.js';
import { openPalette } from '../components/palette.js';
import { wiretap } from '../components/wiretap.js';
import { Kbd, StatusDot } from '../components/ui/badges.js';
import { IconButton } from '../components/ui/buttons.js';
import { Tooltip } from '../components/ui/tooltip.js';

function NetworkChip() {
  const glyph = h('span.net-chip__icon', { 'aria-hidden': 'true' });
  const label = h('span.net-chip__label');
  const detail = h('span.net-chip__detail');
  const el = h('a.net-chip', { href: href('chaos') }, glyph, label, detail);
  let summary = describeNetwork(null);

  const stop = appStore.subscribe(
    (state) => (state.api === 'online' ? state.network : null),
    (conditions) => {
      summary = describeNetwork(conditions);
      el.dataset.tone = summary.tone;
      el.dataset.known = String(summary.known);
      clear(glyph, icon(summary.icon, { size: 14 }));
      label.textContent = summary.label;
      detail.textContent = summary.detail;
      detail.hidden = !summary.detail;
      detail.classList.toggle('num', /\d/.test(summary.detail));
    },
    { immediate: true },
  );
  Tooltip(
    el,
    () =>
      summary.known
        ? `Conditions réseau simulées : ${summary.label.toLowerCase()}${summary.detail ? ` (${summary.detail})` : ''}. Ouvrir le laboratoire de chaos.`
        : 'Conditions réseau inconnues — laboratoire hors ligne.',
    { placement: 'bottom' },
  );
  return { el, stop };
}

function LiveIndicator() {
  const dot = StatusDot({ size: 'sm' });
  const el = h('div.live', { role: 'status' }, dot);
  const labels = {
    live: { tone: 'success', pulse: true, label: 'En direct', tip: 'Flux temps réel connecté (WebSocket).' },
    connecting: { tone: 'warning', pulse: true, label: 'Connexion…', tip: 'Connexion au flux temps réel en cours.' },
    offline: { tone: 'muted', pulse: false, label: 'Hors ligne', tip: 'Laboratoire injoignable — nouvelle tentative automatique.' },
  };
  let current = labels.offline;
  const stop = appStore.subscribe(
    'ws',
    (state) => {
      current = labels[state] ?? labels.offline;
      dot.set(current);
      el.dataset.state = state;
    },
    { immediate: true },
  );
  Tooltip(el, () => current.tip, { placement: 'bottom-end' });
  return { el, stop };
}

/**
 * Construit la barre haute.
 * @param {{onToggleSidebar: () => void}} options
 * @returns {HTMLElement & {setPage: (page: {title: string, subtitle?: string, group?: string}) => void, destroy: () => void}}
 */
export function Topbar({ onToggleSidebar }) {
  const group = h('span.topbar__group');
  const title = h('span.topbar__title', { 'aria-current': 'page' });
  const subtitle = h('span.topbar__subtitle.truncate');
  const separator = h('span.topbar__sep', { 'aria-hidden': 'true' }, icon('chevron-right', { size: 13 }));

  const paletteTrigger = h(
    'button.palette-trigger',
    { type: 'button', 'aria-label': 'Rechercher ou lancer une action', 'aria-keyshortcuts': 'Control+K', onClick: () => openPalette() },
    icon('search', { size: 14 }),
    h('span.palette-trigger__label.palette-trigger__label--full.truncate', 'Rechercher ou lancer une action'),
    h('span.palette-trigger__label.palette-trigger__label--short.truncate', 'Rechercher…'),
    Kbd('mod+k', { size: 'sm' }),
  );

  const tap = h(
    'button.btn.btn--ghost.btn--md.tap-toggle',
    { type: 'button', 'aria-pressed': 'false', onClick: () => wiretap.toggle() },
    h('span.btn__icon', icon('radio', { size: 15 })),
    h('span.btn__label', 'Sous le capot'),
  );
  Tooltip(tap, () => (wiretap.isOpen() ? 'Fermer l’écoute du fil' : 'Écouter les messages sur le fil, en direct'), { placement: 'bottom', kbd: 'u' });

  const themeButton = IconButton({
    icon: getTheme() === 'dark' ? 'sun' : 'moon',
    label: getTheme() === 'dark' ? 'Passer au thème clair' : 'Passer au thème sombre',
    kbd: 't',
    tooltip: 'bottom',
    onClick: () => toggleTheme(),
  });

  const network = NetworkChip();
  const live = LiveIndicator();

  const el = h(
    'header.topbar',
    h(
      'div.topbar__left',
      IconButton({ icon: 'panel-left', label: 'Réduire ou déployer la barre latérale', kbd: 'b', tooltip: 'bottom-start', onClick: onToggleSidebar }),
      h('nav.topbar__crumbs', { 'aria-label': 'Fil d’Ariane' }, group, separator, title, subtitle),
    ),
    h('div.topbar__right', paletteTrigger, network.el, h('span.topbar__divider', { 'aria-hidden': 'true' }), tap, themeButton, h('span.topbar__divider', { 'aria-hidden': 'true' }), live.el),
  );

  const stops = [
    network.stop,
    live.stop,
    appStore.subscribe('wiretap', (open) => tap.setAttribute('aria-pressed', String(open)), { immediate: true }),
    onThemeChange((theme) => {
      themeButton.setIcon(theme === 'dark' ? 'sun' : 'moon');
      themeButton.setLabel(theme === 'dark' ? 'Passer au thème clair' : 'Passer au thème sombre');
    }),
  ];

  el.setPage = (page) => {
    group.textContent = page.group ?? '';
    group.hidden = !page.group;
    separator.hidden = !page.group;
    title.textContent = page.title;
    subtitle.textContent = page.subtitle ?? '';
    subtitle.hidden = !page.subtitle;
  };
  el.destroy = () => stops.forEach((stop) => stop());
  return el;
}
