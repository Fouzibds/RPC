/**
 * Raccourcis clavier globaux : touches simples (`t`, `?`), combinaisons (`mod+k`) et
 * séquences à la Vim (`g o`). Jamais déclenchés pendant la saisie dans un champ, ni
 * (hors combinaisons `mod+`) quand une fenêtre modale est ouverte.
 */

import { hasBlockingLayer } from './layers.js';

/**
 * @typedef {Object} Shortcut
 * @property {string} keys Séquence séparée par des espaces, combinaisons avec `+` : `'g o'`, `'mod+k'`, `'?'`.
 * @property {string} description Ce que fait le raccourci (affiché dans l'aide).
 * @property {string} [group] Rubrique de l'aide (« Navigation », « Affichage »…).
 * @property {(event: KeyboardEvent) => void} run
 */

const SEQUENCE_TIMEOUT_MS = 1200;
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform);

const shortcuts = [];
let pending = '';
let pendingTimer = 0;
let installed = false;

function isTyping(target) {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag !== 'INPUT') return false;
  return !['checkbox', 'radio', 'range', 'button', 'submit'].includes(target.type);
}

function keyOf(event) {
  const mod = event.ctrlKey || event.metaKey;
  return `${mod ? 'mod+' : ''}${event.altKey ? 'alt+' : ''}${event.key.toLowerCase()}`;
}

function onKeydown(event) {
  if (event.defaultPrevented || event.isComposing) return;
  if (['Shift', 'Control', 'Alt', 'Meta'].includes(event.key)) return;
  const key = keyOf(event);
  const isCombo = key.startsWith('mod+');
  if (!isCombo && (isTyping(event.target) || hasBlockingLayer())) return;

  const candidate = pending ? `${pending} ${key}` : key;
  window.clearTimeout(pendingTimer);
  pending = '';

  const exact = shortcuts.find((shortcut) => shortcut.keys === candidate);
  if (exact) {
    event.preventDefault();
    exact.run(event);
    return;
  }
  if (shortcuts.some((shortcut) => shortcut.keys.startsWith(`${candidate} `))) {
    event.preventDefault();
    pending = candidate;
    pendingTimer = window.setTimeout(() => {
      pending = '';
    }, SEQUENCE_TIMEOUT_MS);
  }
}

/**
 * Enregistre un raccourci global.
 * @param {Shortcut} shortcut
 * @returns {() => void} Retire le raccourci.
 */
export function registerShortcut(shortcut) {
  if (!installed) {
    installed = true;
    document.addEventListener('keydown', onKeydown);
  }
  const entry = { group: 'Général', ...shortcut, keys: shortcut.keys.toLowerCase() };
  shortcuts.push(entry);
  return () => {
    const index = shortcuts.indexOf(entry);
    if (index !== -1) shortcuts.splice(index, 1);
  };
}

/**
 * Raccourcis enregistrés, dans l'ordre d'enregistrement (pour la fenêtre d'aide).
 * @returns {Array<{keys: string, description: string, group: string}>}
 */
export function listShortcuts() {
  return shortcuts.map(({ keys, description, group }) => ({ keys, description, group }));
}

const KEY_LABELS = {
  mod: IS_MAC ? '⌘' : 'Ctrl',
  shift: 'Maj',
  alt: IS_MAC ? '⌥' : 'Alt',
  escape: 'Échap',
  esc: 'Échap',
  enter: 'Entrée',
  tab: 'Tab',
  space: 'Espace',
  arrowup: '↑',
  arrowdown: '↓',
  up: '↑',
  down: '↓',
};

/**
 * Découpe une description de raccourci en touches affichables.
 * `'mod+k'` → `[['Ctrl', 'K']]` ; `'g o'` → `[['G'], ['O']]` (une sous-liste par étape de séquence).
 * @param {string} keys
 * @returns {string[][]}
 */
export function formatKeys(keys) {
  return keys
    .trim()
    .split(/\s+/)
    .map((step) =>
      step
        .split('+')
        .filter(Boolean)
        .map((key) => KEY_LABELS[key.toLowerCase()] ?? (key.length === 1 ? key.toUpperCase() : key)),
    );
}
