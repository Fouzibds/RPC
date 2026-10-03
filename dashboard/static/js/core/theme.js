/**
 * Thème sombre / clair : attribut `data-theme` sur `<html>`, choix persisté dans
 * `localStorage` sous la clé `rpcx.theme`. À la première visite, suit `prefers-color-scheme`.
 */

const STORAGE_KEY = 'rpcx.theme';
const THEMES = ['dark', 'light'];
const listeners = new Set();

function stored() {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return THEMES.includes(value) ? value : null;
  } catch {
    return null;
  }
}

function preferred() {
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function apply(theme) {
  document.documentElement.dataset.theme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'light' ? '#FAF9F6' : '#0C0F15');
}

/**
 * Thème actif.
 * @returns {'dark'|'light'}
 */
export function getTheme() {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

/**
 * Applique un thème, le mémorise et prévient les abonnés.
 * @param {'dark'|'light'} theme
 * @param {{persist?: boolean}} [options] `persist: false` n'écrit pas dans `localStorage`.
 * @returns {void}
 */
export function setTheme(theme, { persist = true } = {}) {
  const next = THEMES.includes(theme) ? theme : 'dark';
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* stockage indisponible : le thème reste valable pour la session */
    }
  }
  if (next === document.documentElement.dataset.theme) return;
  const root = document.documentElement;
  root.dataset.themeSwitching = '';
  apply(next);
  window.setTimeout(() => delete root.dataset.themeSwitching, 260);
  for (const listener of Array.from(listeners)) listener(next);
}

/**
 * Bascule entre sombre et clair.
 * @returns {'dark'|'light'} Le nouveau thème.
 */
export function toggleTheme() {
  const next = getTheme() === 'dark' ? 'light' : 'dark';
  setTheme(next);
  return next;
}

/**
 * Initialise le thème au démarrage (choix mémorisé, sinon préférence système) et suit
 * la préférence système tant que l'utilisateur n'a rien choisi.
 * @returns {'dark'|'light'}
 */
export function initTheme() {
  const initial = stored() ?? preferred();
  apply(initial);
  window.matchMedia?.('(prefers-color-scheme: light)').addEventListener?.('change', () => {
    if (!stored()) setTheme(preferred(), { persist: false });
  });
  return initial;
}

/**
 * S'abonne aux changements de thème (utile pour redessiner un graphique).
 * @param {(theme: 'dark'|'light') => void} callback
 * @returns {() => void} Désabonnement.
 */
export function onThemeChange(callback) {
  listeners.add(callback);
  return () => listeners.delete(callback);
}

/**
 * Valeur calculée d'une variable CSS (ex. `cssVar('--proto-grpc')` → `#2FD9C4`).
 * @param {string} name Nom de la variable, tirets compris.
 * @param {Element} [element=document.documentElement]
 * @returns {string}
 */
export function cssVar(name, element = document.documentElement) {
  return getComputedStyle(element).getPropertyValue(name).trim();
}
