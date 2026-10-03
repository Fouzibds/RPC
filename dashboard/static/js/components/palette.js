/**
 * Palette de commandes (Ctrl/⌘ K) : registre de commandes, recherche floue insensible aux
 * accents, navigation au clavier. Les commandes par défaut sont enregistrées par `commands.js`.
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { lockScroll, pushLayer, trapFocus } from '../core/layers.js';
import { Kbd } from './ui/badges.js';
import { toast } from './toast.js';

/**
 * @typedef {Object} Command
 * @property {string} id Identifiant unique.
 * @property {string} title Libellé affiché et recherché.
 * @property {string} [group='Actions'] Rubrique.
 * @property {string} [icon]
 * @property {string} [subtitle] Précision affichée à droite du titre.
 * @property {string[]} [keywords] Synonymes pris en compte par la recherche.
 * @property {string} [shortcut] Raccourci affiché (`'g o'`, `'mod+k'`) — purement indicatif.
 * @property {() => boolean} [when] La commande n'apparaît que si la fonction renvoie vrai.
 * @property {() => (void|Promise<void>)} run
 */

const LEAVE_MS = 140;
const commands = new Map();
let state = null;

function normalize(text) {
  return String(text)
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase();
}

/** Correspondance floue : sous-chaîne d'abord, sinon sous-séquence ; renvoie score + positions. */
function match(query, text) {
  const source = normalize(text);
  const index = source.indexOf(query);
  if (index !== -1) {
    const boundary = index === 0 || /[\s\-_/.:«(]/.test(source[index - 1]);
    return { score: 100 + (boundary ? 40 : 0) - index * 0.5 - source.length * 0.05, positions: Array.from(query, (_, i) => index + i) };
  }
  const positions = [];
  let score = 0;
  let cursor = 0;
  for (const char of query) {
    if (char === ' ') continue;
    const found = source.indexOf(char, cursor);
    if (found === -1) return null;
    const boundary = found === 0 || /[\s\-_/.:«(]/.test(source[found - 1]);
    score += boundary ? 12 : found === cursor ? 8 : 2;
    score -= (found - cursor) * 0.4;
    positions.push(found);
    cursor = found + 1;
  }
  return { score, positions };
}

function highlight(text, positions) {
  if (!positions?.length) return [text];
  const marked = new Set(positions);
  const parts = [];
  let buffer = '';
  let inMark = false;
  Array.from(text).forEach((char, index) => {
    const on = marked.has(index);
    if (on !== inMark && buffer) {
      parts.push(inMark ? h('mark.palette__mark', buffer) : buffer);
      buffer = '';
    }
    inMark = on;
    buffer += char;
  });
  if (buffer) parts.push(inMark ? h('mark.palette__mark', buffer) : buffer);
  return parts;
}

function search(query) {
  const available = Array.from(commands.values()).filter((command) => !command.when || command.when());
  const needle = normalize(query.trim());
  if (!needle) {
    const groups = [];
    for (const command of available) if (!groups.includes(command.group ?? 'Actions')) groups.push(command.group ?? 'Actions');
    return available
      .map((command, index) => ({ command, positions: null, order: groups.indexOf(command.group ?? 'Actions') * 1000 + index }))
      .sort((a, b) => a.order - b.order);
  }
  const results = [];
  for (const command of available) {
    const title = match(needle, command.title);
    const extra = [command.subtitle, command.group, ...(command.keywords ?? [])]
      .filter(Boolean)
      .map((text) => match(needle, text))
      .filter(Boolean)
      .reduce((best, current) => Math.max(best, current.score * 0.6), -Infinity);
    const score = Math.max(title?.score ?? -Infinity, extra);
    if (score === -Infinity) continue;
    results.push({ command, positions: title?.positions ?? null, score });
  }
  return results.sort((a, b) => b.score - a.score);
}

function render() {
  const { list, input, counter } = state;
  const query = input.value;
  const results = search(query);
  state.results = results;
  state.active = Math.min(state.active, Math.max(0, results.length - 1));
  list.replaceChildren();

  if (!results.length) {
    list.appendChild(
      h('div.palette__empty', icon('search', { size: 18 }), h('p', 'Aucune commande ne correspond à « ', h('strong', query.trim()), ' ».')),
    );
    counter.textContent = '';
    return;
  }

  let lastGroup = null;
  results.forEach(({ command, positions }, index) => {
    const group = command.group ?? 'Actions';
    if (!query.trim() && group !== lastGroup) {
      list.appendChild(h('div.palette__group.t-label', { role: 'presentation' }, group));
      lastGroup = group;
    }
    list.appendChild(
      h(
        'div.palette__item',
        {
          role: 'option',
          id: `palette-option-${index}`,
          'aria-selected': String(index === state.active),
          dataset: { index },
          onPointermove: () => {
            if (state.active !== index) setActive(index, false);
          },
          onClick: () => execute(index),
        },
        h('span.palette__icon', { 'aria-hidden': 'true' }, icon(command.icon ?? 'command', { size: 15 })),
        h('span.palette__title', highlight(command.title, positions)),
        command.subtitle ? h('span.palette__subtitle', command.subtitle) : null,
        query.trim() ? h('span.palette__badge', group) : null,
        command.shortcut ? Kbd(command.shortcut, { size: 'sm' }) : null,
      ),
    );
  });
  counter.textContent = `${results.length} commande${results.length > 1 ? 's' : ''}`;
  setActive(state.active, true);
}

function setActive(index, scroll) {
  if (!state || !state.results.length) return;
  const count = state.results.length;
  state.active = ((index % count) + count) % count;
  for (const item of state.list.querySelectorAll('.palette__item')) {
    const on = Number(item.dataset.index) === state.active;
    item.setAttribute('aria-selected', String(on));
    if (on) {
      state.input.setAttribute('aria-activedescendant', item.id);
      if (scroll) item.scrollIntoView({ block: 'nearest' });
    }
  }
}

async function execute(index) {
  const entry = state?.results[index];
  if (!entry) return;
  closePalette();
  try {
    await entry.command.run();
  } catch (error) {
    toast.error('La commande a échoué', { description: error?.message ?? String(error) });
  }
}

function onKeydown(event) {
  const { key } = event;
  if (key === 'ArrowDown' || key === 'ArrowUp') {
    event.preventDefault();
    setActive(state.active + (key === 'ArrowDown' ? 1 : -1), true);
  } else if (key === 'Home' || key === 'End') {
    if (state.input.value) return;
    event.preventDefault();
    setActive(key === 'Home' ? 0 : state.results.length - 1, true);
  } else if (key === 'PageDown' || key === 'PageUp') {
    event.preventDefault();
    const target = state.active + (key === 'PageDown' ? 6 : -6);
    setActive(Math.max(0, Math.min(state.results.length - 1, target)), true);
  } else if (key === 'Enter') {
    event.preventDefault();
    execute(state.active);
  }
}

/**
 * Enregistre une commande dans la palette.
 * @param {Command} command
 * @returns {() => void} Retire la commande.
 */
export function registerCommand(command) {
  commands.set(command.id, command);
  if (state) render();
  return () => {
    if (commands.get(command.id) === command) commands.delete(command.id);
    if (state) render();
  };
}

/**
 * Ouvre la palette de commandes.
 * @param {string} [query=''] Recherche préremplie.
 * @returns {void}
 */
export function openPalette(query = '') {
  if (state) {
    state.input.focus();
    return;
  }
  const input = h('input.palette__input', {
    type: 'text',
    value: query,
    placeholder: 'Rechercher une page, une action, un préréglage réseau…',
    spellcheck: false,
    autocomplete: 'off',
    role: 'combobox',
    'aria-expanded': 'true',
    'aria-controls': 'palette-list',
    'aria-label': 'Rechercher une commande',
    onInput: () => {
      state.active = 0;
      render();
    },
    onKeydown,
  });
  const list = h('div.palette__list.scroll-y', { role: 'listbox', id: 'palette-list' });
  const counter = h('span.palette__count.num');
  const panel = h(
    'div.palette',
    { role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Palette de commandes' },
    h('div.palette__search', icon('search', { size: 17, class: 'palette__search-icon' }), input, Kbd('esc', { size: 'sm' })),
    list,
    h(
      'footer.palette__footer',
      h('span.palette__hint', Kbd('up', { size: 'sm' }), Kbd('down', { size: 'sm' }), 'naviguer'),
      h('span.palette__hint', Kbd('enter', { size: 'sm' }), 'exécuter'),
      h('span.palette__hint', Kbd('?', { size: 'sm' }), 'raccourcis'),
      counter,
    ),
  );
  const backdrop = h(
    'div.overlay.overlay--palette',
    {
      onPointerdown: (event) => {
        if (event.target === backdrop) closePalette();
      },
    },
    panel,
  );
  document.body.appendChild(backdrop);

  state = { backdrop, input, list, counter, results: [], active: 0 };
  state.cleanups = [lockScroll(), pushLayer({ onEscape: closePalette }), trapFocus(panel, { initial: input })];
  render();
  input.select();
}

/**
 * Ferme la palette (sans effet si elle est fermée).
 * @returns {void}
 */
export function closePalette() {
  if (!state) return;
  const { backdrop, cleanups } = state;
  state = null;
  cleanups.forEach((cleanup) => cleanup());
  backdrop.dataset.leaving = 'true';
  window.setTimeout(() => backdrop.remove(), LEAVE_MS);
}

/**
 * Ouvre ou ferme la palette.
 * @returns {void}
 */
export function togglePalette() {
  if (state) closePalette();
  else openPalette();
}
