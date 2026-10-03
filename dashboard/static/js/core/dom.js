/**
 * Outils DOM minimalistes : hyperscript `h()`, équivalent SVG, sélecteurs.
 * Tout le reste de l'interface est construit avec ces fonctions.
 */

import { typo } from './format.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Propriétés affectées directement à l'élément plutôt que posées en attribut. */
const DIRECT_PROPS = new Set([
  'value', 'checked', 'disabled', 'selected', 'hidden', 'indeterminate', 'readOnly', 'tabIndex',
  'textContent', 'innerHTML', 'htmlFor', 'title', 'id', 'type', 'name', 'placeholder', 'href',
  'min', 'max', 'step', 'rows', 'spellcheck', 'autocomplete',
]);

/** Conteneurs dont le texte est une donnée brute (code, octets, saisie) : aucune retouche typographique. */
const RAW_TEXT = 'pre, code, kbd, samp, textarea, script, style, .mono, .num, [data-raw]';

let uidCounter = 0;

/**
 * Assemble des noms de classes : chaînes, tableaux imbriqués ou objets `{nom: booléen}`.
 * @param {...(string|string[]|Record<string, unknown>|null|undefined|false)} parts
 * @returns {string}
 */
export function cx(...parts) {
  const out = [];
  for (const part of parts) {
    if (!part) continue;
    if (typeof part === 'string') out.push(part);
    else if (Array.isArray(part)) out.push(cx(...part));
    else if (typeof part === 'object') {
      for (const [name, on] of Object.entries(part)) if (on) out.push(name);
    }
  }
  return out.filter(Boolean).join(' ');
}

function isPlainProps(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) && !(value instanceof Node);
}

/**
 * Sépare les arguments d'un composant `(props?, ...children)` : si le premier argument n'est pas
 * un objet de propriétés, il est traité comme un enfant.
 * @param {*} props
 * @param {Array<*>} children
 * @returns {[Record<string, *>, Array<*>]} `[props, children]`
 */
export function splitArgs(props, children) {
  if (isPlainProps(props)) return [props, children];
  return [{}, props === undefined ? children : [props, ...children]];
}

function applyStyle(el, style) {
  if (typeof style === 'string') {
    el.setAttribute('style', style);
    return;
  }
  for (const [name, value] of Object.entries(style)) {
    if (value === null || value === undefined || value === false) continue;
    if (name.startsWith('--') || name.includes('-')) el.style.setProperty(name, String(value));
    else el.style[name] = value;
  }
}

function applyProps(el, props, isSvg) {
  for (const [name, value] of Object.entries(props)) {
    if (name === 'class' || name === 'className') {
      const classes = cx(value);
      if (classes) el.setAttribute('class', el.getAttribute('class') ? `${el.getAttribute('class')} ${classes}` : classes);
    } else if (name === 'style') {
      if (value) applyStyle(el, value);
    } else if (name === 'dataset') {
      for (const [key, data] of Object.entries(value || {})) {
        if (data !== null && data !== undefined && data !== false) el.dataset[key] = String(data);
      }
    } else if (name === 'ref') {
      if (typeof value === 'function') value(el);
    } else if (name.startsWith('on') && typeof value === 'function') {
      el.addEventListener(name.slice(2).toLowerCase(), value);
    } else if (value === null || value === undefined || value === false) {
      continue;
    } else if (!isSvg && DIRECT_PROPS.has(name)) {
      el[name] = value;
    } else {
      el.setAttribute(name, value === true ? '' : String(value));
    }
  }
}

/**
 * Ajoute des enfants à un élément. Accepte nœuds, chaînes, nombres, tableaux imbriqués ;
 * ignore `null`, `undefined` et `false`. Les chaînes reçoivent la typographie française
 * (`typo()` : espaces insécables avant « : ; ? ! % ») sauf dans un conteneur de texte brut
 * (`pre`, `code`, `kbd`, `textarea`, `.mono`, `.num`, `[data-raw]`).
 * @param {Element|DocumentFragment} parent
 * @param {...*} children
 * @returns {Element|DocumentFragment} `parent`
 */
export function append(parent, ...children) {
  const raw = parent instanceof Element && parent.matches(RAW_TEXT);
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) append(parent, ...child);
    else if (child instanceof Node) parent.appendChild(child);
    else parent.appendChild(document.createTextNode(raw ? String(child) : typo(String(child))));
  }
  return parent;
}

function create(ns, spec, props, children) {
  const [tag, ...classes] = spec.split('.');
  const el = ns ? document.createElementNS(ns, tag || 'g') : document.createElement(tag || 'div');
  if (classes.length) el.setAttribute('class', classes.join(' '));
  if (isPlainProps(props)) applyProps(el, props, Boolean(ns));
  else if (props !== null && props !== undefined) children.unshift(props);
  append(el, ...children);
  return el;
}

/**
 * Hyperscript : crée un élément HTML.
 *
 * `h('div.card.card--flat', { class: {'is-on': ok}, style: {'--i': 2}, dataset: {id}, onClick }, 'texte', enfant)`
 *
 * - `tag` accepte le raccourci `balise.classe1.classe2` (balise omise = `div`).
 * - `props` (facultatif) : `class` (chaîne / tableau / objet), `style` (chaîne ou objet, variables CSS
 *   comprises), `dataset`, `on<Évènement>`, `ref(el)`, propriétés DOM usuelles (`value`, `checked`,
 *   `disabled`…) ; tout le reste devient un attribut (`aria-*`, `role`…). `false`/`null` = ignoré.
 * - `children` : nœuds, chaînes, nombres, tableaux ; `null`/`false` ignorés. Le texte reçoit la
 *   typographie française, sauf dans un conteneur de texte brut (voir `append`).
 * @param {string} tag
 * @param {Record<string, *>|Node|string|Array|null} [props]
 * @param {...*} children
 * @returns {HTMLElement}
 */
export function h(tag, props, ...children) {
  return create(null, tag, props, children);
}

/**
 * Comme `h()`, mais dans l'espace de noms SVG (`svg('circle', {cx: 4, cy: 4, r: 2})`).
 * @param {string} tag
 * @param {Record<string, *>|Node|string|Array|null} [attrs]
 * @param {...*} children
 * @returns {SVGElement}
 */
export function svg(tag, attrs, ...children) {
  return create(SVG_NS, tag, attrs, children);
}

/**
 * Vide un élément ; s'il reçoit des enfants, les insère à la place.
 * @param {Element} el
 * @param {...*} children
 * @returns {Element} `el`
 */
export function clear(el, ...children) {
  el.replaceChildren();
  append(el, ...children);
  return el;
}

/**
 * `querySelector` raccourci.
 * @param {string} selector
 * @param {ParentNode} [root=document]
 * @returns {Element|null}
 */
export function qs(selector, root = document) {
  return root.querySelector(selector);
}

/**
 * `querySelectorAll` renvoyant un vrai tableau.
 * @param {string} selector
 * @param {ParentNode} [root=document]
 * @returns {Element[]}
 */
export function qsa(selector, root = document) {
  return Array.from(root.querySelectorAll(selector));
}

/**
 * Abonne un écouteur et renvoie la fonction qui le retire.
 * @param {EventTarget} target
 * @param {string} type
 * @param {EventListener} handler
 * @param {boolean|AddEventListenerOptions} [options]
 * @returns {() => void}
 */
export function on(target, type, handler, options) {
  target.addEventListener(type, handler, options);
  return () => target.removeEventListener(type, handler, options);
}

/**
 * Identifiant unique dans la page (liaison `label`/`aria-*`).
 * @param {string} [prefix='id']
 * @returns {string}
 */
export function uid(prefix = 'id') {
  uidCounter += 1;
  return `${prefix}-${uidCounter}`;
}
