/**
 * Console RPC — historique des 30 derniers appels de la session (conservé dans `sessionStorage`) :
 * un clic recharge la configuration, un bouton relance l'appel.
 */

import { h, clear } from '../../core/dom.js';
import { fmtMs, fmtNumber, fmtTime } from '../../core/format.js';
import { Badge, Button, Card, EmptyState, IconButton, ProtocolChip, StatusDot } from '../../components/ui.js';
import { MODES, sentParams } from './model.js';

const STORAGE_KEY = 'rpcx.console.history';
/** Nombre d'appels conservés. */
export const HISTORY_LIMIT = 30;

/**
 * @typedef {Object} HistoryEntry
 * @property {string} id
 * @property {number} at Horodatage (ms) de la fin de l'appel.
 * @property {import('./model.js').ConsoleConfig} config Configuration rejouable.
 * @property {boolean} ok
 * @property {string} code Code canonique (`OK`, `NOT_FOUND`, `TIMEOUT`…).
 * @property {number|null} durationMs Durée de l'appel, temps réel du lot, ou durée du flux.
 * @property {number} [items] Nombre d'éléments reçus (flux).
 * @property {number} [errors] Appels en échec (lot asynchrone).
 */

/**
 * Lit l'historique de la session.
 * @returns {HistoryEntry[]}
 */
export function loadHistory() {
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) ?? '[]');
    return Array.isArray(parsed) ? parsed.filter((entry) => entry?.config?.method).slice(0, HISTORY_LIMIT) : [];
  } catch {
    return [];
  }
}

/**
 * Enregistre l'historique (silencieux si le stockage est indisponible).
 * @param {HistoryEntry[]} entries
 * @returns {void}
 */
export function saveHistory(entries) {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(entries.slice(0, HISTORY_LIMIT)));
  } catch {
    /* stockage plein ou désactivé : l'historique reste en mémoire */
  }
}

function modeLabel(config) {
  if (config.mode === 'async') return `${MODES.async.short} × ${fmtNumber(config.count)}`;
  return MODES[config.mode]?.short ?? config.mode;
}

function argsPreview(entry, catalog) {
  const spec = catalog?.methods?.find((method) => method.name === entry.config.method);
  if (!spec) return '';
  return sentParams(spec, entry.config.values?.[spec.name])
    .map((param) => `${param.name}=${typeof param.value === 'object' ? `[${param.value?.length ?? 0}]` : JSON.stringify(param.value)}`)
    .join(', ');
}

function detail(entry) {
  if (entry.config.mode === 'stream' && Number.isFinite(entry.items)) return `${fmtNumber(entry.items)} ${entry.config.method === 'bulk_update_stock' ? 'envoi' : 'élément'}${entry.items > 1 ? 's' : ''}`;
  if (entry.config.mode === 'async' && entry.errors) return `${fmtNumber(entry.errors)} en échec`;
  return '';
}

/**
 * Carte « Historique ».
 * @param {Object} props
 * @param {() => Object|null} props.getCatalog
 * @param {(entry: HistoryEntry) => void} props.onLoad Recharge la configuration d'un appel.
 * @param {(entry: HistoryEntry) => void} props.onRerun Recharge puis relance.
 * @param {() => void} props.onClear
 * @returns {HTMLElement & {set: (entries: HistoryEntry[], activeId?: string|null) => void}}
 */
export function HistoryPanel({ getCatalog, onLoad, onRerun, onClear }) {
  const list = h('ol.console-history__list.scroll-y');
  const clearButton = Button({ label: 'Effacer', size: 'sm', variant: 'ghost', icon: 'trash-2', onClick: onClear });
  const counter = Badge({ label: '0', size: 'sm', mono: true });
  const el = Card({ title: h('span.console-history__title', 'Historique', counter), subtitle: `Les ${HISTORY_LIMIT} derniers appels de cette session — cliquez pour recharger la configuration`, icon: 'history', actions: clearButton, padding: 'none', class: 'console-history' }, list);

  el.set = (entries, activeId = null) => {
    counter.setLabel(String(entries.length));
    clearButton.setDisabled(entries.length === 0);
    if (!entries.length) {
      clear(list, h('li.console-history__empty', EmptyState({ icon: 'history', size: 'sm', title: 'Aucun appel pour l’instant', text: 'Chaque appel exécuté s’ajoute ici avec sa durée et son issue, pour être comparé ou relancé.' })));
      return;
    }
    const catalog = getCatalog();
    clear(
      list,
      entries.map((entry) =>
        h(
          'li.console-history__row',
          { dataset: { active: String(entry.id === activeId) } },
          h(
            'button.console-history__main',
            { type: 'button', title: 'Recharger cette configuration', onClick: () => onLoad(entry) },
            StatusDot({ tone: entry.ok ? 'success' : 'danger', size: 'sm' }),
            ProtocolChip(entry.config.protocol, { short: true, size: 'sm' }),
            h('span.console-history__call', h('span.mono.console-history__method', entry.config.method), h('span.mono.console-history__args.truncate', `(${argsPreview(entry, catalog)})`)),
            h('span.console-history__mode', modeLabel(entry.config), detail(entry) ? ` · ${detail(entry)}` : ''),
            h('span.console-history__code.mono', { dataset: { ok: String(entry.ok) } }, entry.code),
            h('span.console-history__duration.num', fmtMs(entry.durationMs)),
            h('span.console-history__time.num', fmtTime(entry.at)),
          ),
          IconButton({ icon: 'rotate-ccw', label: 'Relancer cet appel', size: 'sm', onClick: () => onRerun(entry) }),
        ),
      ),
    );
  };
  el.set([]);
  return el;
}
