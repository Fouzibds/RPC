/**
 * Vue d'ensemble — « Activité récente » : les 12 derniers appels, chargés depuis
 * `GET /api/traces` puis alimentés par les messages WebSocket `call`. Une ligne ouvre la
 * trace complète de l'appel dans « Sous le capot ».
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtTime, fmtUs } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';
import { href } from '../../core/router.js';
import { Badge, Button, Callout, Card, EmptyState, ProtocolChip, StatusDot, Table } from '../../components/ui.js';
import { tracesOf } from './model.js';

const LIMIT = 12;
const ANNOUNCE_EVERY_MS = 4000;

const bytes = (value) => (value === null || value === undefined ? null : fmtBytes(value));

function statusBadge(call) {
  if (call.status === 'ok') return Badge({ label: 'OK', tone: 'success', size: 'sm', mono: true });
  return Badge({ label: call.error?.code ?? 'ERREUR', tone: 'danger', size: 'sm', mono: true, title: call.error?.message || null });
}

const COLUMNS = [
  { key: 'started_at', label: 'Heure', mono: true, format: (value) => fmtTime(value, { ms: true }) },
  { key: 'protocol', label: 'Protocole', format: (value) => ProtocolChip(value, { short: true, size: 'sm' }) },
  { key: 'method', label: 'Procédure', mono: true },
  { key: 'duration_us', label: 'Durée', align: 'right', mono: true, format: (value) => fmtUs(value) },
  { key: 'request_bytes', label: 'Envoyés', align: 'right', mono: true, title: 'Octets de la requête, tramage et en-têtes compris', format: bytes },
  { key: 'response_bytes', label: 'Reçus', align: 'right', mono: true, title: 'Octets de la réponse, tramage et en-têtes compris', format: bytes },
  { key: 'status', label: 'Statut', format: (value, row) => statusBadge(row) },
];

/** Du plus récent au plus ancien, d'après l'heure de départ de l'appel. */
const newestFirst = (a, b) => (Number(b.started_at) || 0) - (Number(a.started_at) || 0);

/**
 * Carte « Activité récente ».
 * @param {Object} props
 * @param {{get: Function}} props.api Client HTTP du laboratoire.
 * @param {(id: string, query?: Object) => void} props.navigate Navigation de la coquille.
 * @param {() => void} props.onTraffic Lance la rafale d'appels (action de l'état vide).
 * @returns {HTMLElement & {load: () => Promise<void>, push: (call: Object) => void, setOffline: (offline: boolean) => void, destroy: () => void}}
 *   `load` (re)charge l'historique ; `push` ajoute un appel terminé (message `call`).
 */
export function createActivity({ api, navigate, onTraffic }) {
  let rows = [];
  let phase = 'loading';
  let offline = false;
  let frame = 0;
  let fresh = new Set();
  let lastAnnounce = 0;
  let controller = null;

  const live = StatusDot({ tone: 'muted', label: 'En attente', size: 'sm' });
  const announcer = h('p.sr-only', { 'aria-live': 'polite' });
  const table = Table({
    columns: COLUMNS,
    rowKey: (row) => row.call_id,
    rowTone: (row) => (row.status === 'error' ? 'danger' : null),
    onRowClick: (row) => navigate('xray', { call: row.call_id }),
    caption: 'Derniers appels du laboratoire, du plus récent au plus ancien',
  });
  const body = h('div.ov-activity');
  const el = Card(
    {
      title: 'Activité récente',
      subtitle: `Les ${LIMIT} derniers appels, tous protocoles confondus`,
      icon: 'history',
      padding: 'none',
      actions: live,
      class: 'ov-activity-card',
      footer: [
        h('span', 'Une ligne ouvre la trace complète de l’appel : étapes, durées, octets.'),
        Button({ label: 'Sous le capot', variant: 'ghost', size: 'sm', iconRight: 'arrow-right', href: href('xray') }),
      ],
    },
    body,
    announcer,
  );

  function announce() {
    const now = performance.now();
    const last = rows[0];
    if (!last || now - lastAnnounce < ANNOUNCE_EVERY_MS) return;
    lastAnnounce = now;
    announcer.textContent = `Dernier appel : ${protocol(last.protocol).label}, ${last.method}, ${fmtUs(last.duration_us)}, ${last.status === 'ok' ? 'réussi' : `erreur ${last.error?.code ?? ''}`}.`;
  }

  function render() {
    live.set(offline ? { tone: 'muted', pulse: false, label: 'Hors ligne' } : { tone: 'success', pulse: true, label: 'En direct' });
    if (phase === 'loading') {
      table.setLoading(true, LIMIT);
      clear(body, table);
      return;
    }
    if (phase === 'error' && !offline) {
      clear(
        body,
        h(
          'div.ov-activity__state',
          Callout({
            tone: 'danger',
            title: 'Historique indisponible',
            text: 'Le laboratoire n’a pas renvoyé la liste des derniers appels. Les nouveaux appels s’afficheront tout de même ici.',
            actions: Button({ label: 'Réessayer', icon: 'refresh-cw', size: 'sm', onClick: () => el.load() }),
          }),
        ),
      );
      return;
    }
    if (!rows.length) {
      clear(
        body,
        offline
          ? EmptyState({ icon: 'unplug', tone: 'danger', title: 'Laboratoire hors ligne', text: 'L’historique des appels reviendra dès que le serveur du laboratoire répondra.' })
          : EmptyState({
              icon: 'history',
              tone: 'accent',
              title: 'Aucun appel pour l’instant',
              text: 'Lancez une rafale d’appels réels sur les trois middlewares pour voir le laboratoire s’animer.',
              action: Button({ label: 'Générer du trafic', variant: 'primary', icon: 'zap', onClick: onTraffic }),
            }),
      );
      return;
    }
    table.setRows(rows);
    if (table.parentNode !== body) clear(body, table);
    for (const row of table.querySelectorAll('.table__row[data-key]')) {
      if (fresh.has(row.dataset.key)) row.classList.add('ov-row-new');
    }
    fresh = new Set();
    announce();
  }

  el.load = async () => {
    controller?.abort();
    controller = new AbortController();
    const { signal } = controller;
    phase = 'loading';
    render();
    try {
      const loaded = tracesOf(await api.get('/api/traces', { query: { limit: LIMIT }, signal, timeoutMs: 8000 }));
      if (signal.aborted) return;
      const known = new Set(loaded.map((row) => row.call_id));
      rows = [...rows.filter((row) => !known.has(row.call_id)), ...loaded].sort(newestFirst).slice(0, LIMIT);
      phase = 'ready';
    } catch (error) {
      if (signal.aborted) return;
      phase = error?.offline ? 'ready' : 'error';
    }
    render();
  };

  el.push = (call) => {
    if (typeof call?.call_id !== 'string' || call.status === 'pending') return;
    rows = [call, ...rows.filter((row) => row.call_id !== call.call_id)].sort(newestFirst).slice(0, LIMIT);
    fresh.add(call.call_id);
    if (phase === 'error') phase = 'ready';
    if (phase !== 'ready' || frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      render();
    });
  };

  el.setOffline = (next) => {
    if (offline === next) return;
    offline = next;
    render();
  };

  el.destroy = () => {
    controller?.abort();
    cancelAnimationFrame(frame);
  };

  render();
  return el;
}
