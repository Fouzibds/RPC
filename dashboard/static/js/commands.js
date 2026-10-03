/**
 * Commandes et raccourcis par défaut de l'application : navigation, affichage,
 * remise à zéro du laboratoire, préréglages réseau, aide des raccourcis.
 */

import { api, refreshCatalog, refreshStatus } from './core/api.js';
import { h } from './core/dom.js';
import { NETWORK_PRESETS, applyPreset } from './core/network.js';
import { navigate } from './core/router.js';
import { NAV_GROUPS, ROUTES } from './core/routes.js';
import { listShortcuts, registerShortcut } from './core/shortcuts.js';
import { toggleTheme } from './core/theme.js';
import { confirm, openModal } from './components/modal.js';
import { registerCommand, togglePalette } from './components/palette.js';
import { toast } from './components/toast.js';
import { wiretap } from './components/wiretap.js';
import { Kbd } from './components/ui/badges.js';

let shortcutsModal = null;

/**
 * Ouvre la fenêtre d'aide listant tous les raccourcis clavier enregistrés.
 * @returns {void}
 */
export function openShortcutsHelp() {
  if (shortcutsModal) return;
  const groups = new Map();
  for (const shortcut of listShortcuts()) {
    if (!groups.has(shortcut.group)) groups.set(shortcut.group, []);
    groups.get(shortcut.group).push(shortcut);
  }
  shortcutsModal = openModal({
    title: 'Raccourcis clavier',
    description: 'Inactifs pendant la saisie dans un champ.',
    icon: 'keyboard',
    size: 'lg',
    body: h(
      'div.shortcuts',
      Array.from(groups, ([group, items]) =>
        h(
          'section.shortcuts__group',
          h('h3.t-label', group),
          h(
            'ul.shortcuts__list',
            items.map((item) => h('li.shortcuts__item', h('span', item.description), Kbd(item.keys))),
          ),
        ),
      ),
    ),
    onClose: () => {
      shortcutsModal = null;
    },
  });
}

async function resetLab() {
  const ok = await confirm({
    title: 'Réinitialiser le laboratoire ?',
    text: 'Les stocks, les conditions réseau, les statistiques et les traces reviennent à leur état initial. Les rapports enregistrés sont conservés.',
    confirmLabel: 'Réinitialiser',
    tone: 'danger',
    icon: 'rotate-ccw',
  });
  if (!ok) return;
  try {
    await api.post('/api/reset');
    wiretap.clear();
    await Promise.all([refreshStatus(), refreshCatalog()]);
    toast.success('Laboratoire réinitialisé', { description: 'Stocks, réseau, statistiques et traces remis à zéro.' });
  } catch (error) {
    toast.error('Réinitialisation impossible', { description: error.message });
  }
}

async function usePreset(preset) {
  try {
    await applyPreset(preset.id);
    const notify = preset.tone === 'danger' ? toast.warn : toast.success;
    notify(`Réseau : ${preset.label}`, { description: preset.description });
  } catch (error) {
    toast.error('Préréglage réseau non appliqué', { description: error.message });
  }
}

/**
 * Enregistre les commandes de la palette et les raccourcis globaux. À appeler une fois au démarrage.
 * @param {{toggleSidebar: () => void}} actions Actions fournies par la coquille.
 * @returns {void}
 */
export function registerDefaults({ toggleSidebar }) {
  const groupLabel = Object.fromEntries(NAV_GROUPS.map((group) => [group.id, group.label]));

  for (const route of ROUTES) {
    registerCommand({
      id: `go.${route.id}`,
      title: route.label,
      subtitle: route.group ? groupLabel[route.group] : 'Guide de style',
      group: 'Aller à',
      icon: route.icon,
      keywords: [route.id, route.subtitle, 'page', 'ouvrir', 'aller'],
      shortcut: route.key ? `g ${route.key}` : undefined,
      run: () => navigate(route.id),
    });
    if (route.key) {
      registerShortcut({ keys: `g ${route.key}`, description: route.label, group: 'Navigation', run: () => navigate(route.id) });
    }
  }

  const view = [
    { id: 'view.palette', title: 'Palette de commandes', icon: 'command', keys: 'mod+k', run: togglePalette, inPalette: false },
    { id: 'view.theme', title: 'Basculer le thème clair / sombre', icon: 'sun', keys: 't', keywords: ['dark', 'light', 'nuit', 'jour', 'apparence'], run: toggleTheme },
    { id: 'view.wiretap', title: 'Sous le capot : écouter le fil', icon: 'radio', keys: 'u', keywords: ['trace', 'wire', 'octets', 'messages', 'tiroir'], run: wiretap.toggle },
    { id: 'view.sidebar', title: 'Réduire ou déployer la barre latérale', icon: 'panel-left', keys: 'b', keywords: ['menu', 'navigation'], run: toggleSidebar },
    { id: 'view.shortcuts', title: 'Afficher les raccourcis clavier', icon: 'keyboard', keys: '?', keywords: ['aide', 'help', 'touches'], run: openShortcutsHelp },
  ];
  for (const item of view) {
    registerShortcut({ keys: item.keys, description: item.title, group: 'Affichage', run: () => item.run() });
    if (item.inPalette === false) continue;
    registerCommand({ id: item.id, title: item.title, group: 'Affichage', icon: item.icon, keywords: item.keywords, shortcut: item.keys, run: () => item.run() });
  }

  registerCommand({
    id: 'lab.reset',
    title: 'Réinitialiser le laboratoire',
    subtitle: 'POST /api/reset',
    group: 'Laboratoire',
    icon: 'rotate-ccw',
    keywords: ['reset', 'zéro', 'remise', 'stocks', 'effacer'],
    run: resetLab,
  });
  registerCommand({
    id: 'lab.clear-wire',
    title: 'Effacer les messages « Sous le capot »',
    group: 'Laboratoire',
    icon: 'trash-2',
    keywords: ['trace', 'vider', 'clear'],
    run: () => wiretap.clear(),
  });

  for (const preset of NETWORK_PRESETS) {
    registerCommand({
      id: `network.${preset.id}`,
      title: `Réseau : ${preset.label}`,
      subtitle: preset.description,
      group: 'Conditions réseau',
      icon: preset.icon,
      keywords: ['network', 'chaos', 'latence', 'préréglage', 'preset', preset.id],
      run: () => usePreset(preset),
    });
  }
}
