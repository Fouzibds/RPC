/**
 * Transparence — « La preuve par l'exécution » : les écritures affichées sont exécutées par le
 * laboratoire (`POST /api/code-compare/run`), puis un appel tracé de la même procédure sur chaque
 * protocole (`POST /api/call`) donne les octets échangés. Le stock est rétabli après chaque appel.
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtNumber } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { Badge, Button, Callout, Card, EmptyState, ProtocolChip, Section, Skeleton } from '../../components/ui.js';
import { fmtCall, fmtTimes, plural, shortTitle } from './model.js';
import { restoreStock, updateStock } from './ops.js';

const COUNT_WORDS = { 2: 'deux', 3: 'trois', 4: 'quatre', 5: 'cinq' };

function pending(width) {
  return Skeleton({ variant: 'text', width });
}

function stateMark(state) {
  if (state === 'running') return h('span.cmp-proof__state', { dataset: { state } }, icon('loader-circle', { size: 15, class: 'spin', label: 'En cours' }));
  if (state === 'ok') return h('span.cmp-proof__state', { dataset: { state } }, icon('circle-check', { size: 15, label: 'Réussi' }));
  return h('span.cmp-proof__state', { dataset: { state } }, icon('circle-x', { size: 15, label: 'Échec' }));
}

/** Appel affiché en chasse fixe ; il ne se replie qu'après la parenthèse ouvrante ou entre deux arguments. */
function callExpression(call) {
  const open = String(call).indexOf('(') + 1;
  return h('code.cmp-proof__call', open ? [call.slice(0, open), h('wbr'), call.slice(open)] : call);
}

function row(snippet) {
  const info = protocol(snippet.id);
  const cells = { stock: h('td.cmp-proof__stock'), duration: h('td.cmp-proof__duration'), ratio: h('td.cmp-proof__ratio.num'), bytes: h('td.cmp-proof__bytes'), state: h('td.cmp-proof__mark') };
  const el = h(
    'tr',
    { style: { '--proto': info.color } },
    h('th.cmp-proof__name', { scope: 'row' }, h('div.cmp-proof__who', ProtocolChip(snippet.id, { short: true, size: 'sm' }), callExpression(snippet.call))),
    cells.stock,
    cells.duration,
    cells.ratio,
    cells.bytes,
    cells.state,
  );

  return {
    el,
    setPending() {
      clear(cells.stock, pending(36));
      clear(cells.duration, pending(72));
      clear(cells.ratio, pending(40));
      clear(cells.bytes, pending(96));
      clear(cells.state, stateMark('running'));
    },
    /** @param {Object} run Entrée de `runs[]`. @param {number} share Part de la barre (0..1, échelle logarithmique). */
    setRun(run, share) {
      clear(
        cells.stock,
        run.ok ? h('span.cmp-proof__value.num', fmtNumber(run.result)) : Badge({ label: run.error?.code ?? 'ERREUR', tone: 'danger', size: 'sm', mono: true, title: run.error?.message }),
      );
      clear(
        cells.duration,
        h('span.num', fmtCall(run.duration_ms)),
        h('span.cmp-proof__bar', { 'aria-hidden': 'true' }, h('span.cmp-proof__fill', { style: { '--share': String(share) } })),
      );
      clear(cells.ratio, run.ratio_to_local === null || run.ratio_to_local === undefined ? '—' : fmtTimes(run.ratio_to_local));
      clear(cells.state, stateMark(run.ok ? 'ok' : 'error'));
    },
    /** @param {Object|null} reply Réponse de `POST /api/call`, ou `null` si la mesure n'a pas eu lieu. */
    setBytes(reply) {
      if (!reply) return void clear(cells.bytes, h('span.cmp-proof__none', '—'));
      const sent = reply.request_bytes;
      const received = reply.response_bytes;
      if (sent === null && received === null) {
        return void clear(cells.bytes, h('span.cmp-proof__none', { title: 'Rien n’est sérialisé : l’appel ne quitte pas le processus' }, 'aucun octet'));
      }
      clear(cells.bytes, h('span.num', fmtBytes(sent)), h('span.cmp-proof__to', { 'aria-hidden': 'true' }, '→'), h('span.num', fmtBytes(received)));
      cells.bytes.title = `Requête ${fmtBytes(sent)}, réponse ${fmtBytes(received)}`;
    },
    setBytesPending() {
      clear(cells.bytes, pending(96));
    },
  };
}

/** Part de chaque durée sur une échelle logarithmique : la plus courte garde une barre visible. */
function logShares(runs) {
  const values = runs.map((run) => Number(run.duration_ms)).filter((value) => value > 0);
  const min = Math.min(...values);
  const max = Math.max(...values);
  return runs.map((run) => {
    const value = Number(run.duration_ms);
    if (!(value > 0) || max === min) return 1;
    return 0.06 + 0.94 * ((Math.log(value) - Math.log(min)) / (Math.log(max) - Math.log(min)));
  });
}

function verdict(result, snippets, restored) {
  const runs = result.runs;
  const allOk = runs.every((run) => run.ok);
  const tone = !result.equivalent ? 'danger' : allOk ? 'success' : 'warning';
  const title = !result.equivalent
    ? 'Les écritures ne donnent pas le même résultat'
    : allOk
      ? `${plural(runs.length, 'écriture')}, un seul résultat : ${fmtNumber(runs[0].result)}`
      : `Les ${runs.length} écritures échouent de la même façon : ${runs[0].error?.code ?? 'erreur'}`;

  const local = runs.find((run) => run.id === 'local' && run.ok);
  const remote = runs.filter((run) => run.id !== 'local' && run.ok).sort((a, b) => a.duration_ms - b.duration_ms)[0];
  const remoteTitle = remote ? shortTitle(snippets.find((snippet) => snippet.id === remote.id) ?? remote) : '';
  const cost =
    local && remote && local.duration_ms > 0
      ? `Le plus rapide des appels distants (« ${remoteTitle} », ${fmtCall(remote.duration_ms)}) coûte ${fmtTimes(remote.duration_ms / local.duration_ms)} l’appel local (${fmtCall(local.duration_ms)}).`
      : '';
  const stockAfter = restored ?? result.stock_after;
  const stock = `Stock de ${result.product_id} : ${fmtNumber(result.stock_before)} avant, ${fmtNumber(stockAfter)} après${stockAfter === result.stock_before ? ' — rétabli' : ''}.`;
  return Callout({ tone, title }, h('p', result.summary), cost ? h('p', cost) : null, h('p.cmp-proof__stockline', stock));
}

/**
 * Section « La preuve par l'exécution ». Le nœud renvoyé porte `run()` (raccourci Ctrl/⌘ + Entrée)
 * et `runLabel`, le libellé de cette action (« Exécuter les quatre »).
 * @param {Object} data Réponse de `GET /api/code-compare`.
 * @param {Object} env Environnement de la page (`api`, `store`, `toast`, `track`, `alive`, `refreshProduct`).
 * @returns {HTMLElement & {run: () => Promise<void>, runLabel: string}}
 */
export function proofSection(data, env) {
  const { api, store } = env;
  const snippets = data.snippets;
  const count = COUNT_WORDS[snippets.length] ?? String(snippets.length);
  const body = h('div.cmp-proof__body');
  const live = h('div.cmp-proof__live', { role: 'status', 'aria-live': 'polite' });
  let running = false;

  const again = Button({ label: 'Relancer', icon: 'rotate-ccw', kbd: 'mod+enter', onClick: () => run() });
  again.hidden = true;
  const runLabel = `Exécuter les ${count}`;
  const primary = Button({ label: runLabel, variant: 'primary', icon: 'play', kbd: 'mod+enter', id: 'compare-run', disabled: store.get().api === 'offline', onClick: () => run() });

  function showEmpty() {
    clear(
      body,
      EmptyState({
        icon: 'flask-conical',
        title: 'Aucune exécution pour l’instant',
        text: `Lancez les ${count} écritures sur le laboratoire : parties du même stock, elles doivent renvoyer le même nouveau stock.`,
        action: primary,
        size: 'sm',
      }),
    );
  }

  function failure(error) {
    const offline = Boolean(error?.offline);
    return Callout({
      tone: offline ? 'warning' : 'danger',
      icon: offline ? 'wifi-off' : undefined,
      title: offline ? 'Laboratoire injoignable' : 'L’exécution a échoué',
      text: offline ? 'Le serveur du tableau de bord ne répond pas. Les écritures restent lisibles ; relancez dès qu’il est de retour.' : (error?.message ?? 'Erreur inattendue.'),
      actions: Button({ label: 'Réessayer', icon: 'refresh-cw', size: 'sm', onClick: () => run() }),
    });
  }

  async function measureBytes(rows, expected) {
    let restored = null;
    for (const snippet of snippets) {
      const view = rows.get(snippet.id);
      const reply = await updateStock(api, { protocol: snippet.id, method: data.method, productId: data.product_id, delta: -data.quantity });
      view.setBytes(reply);
      restored = (await restoreStock(api, data.method, data.product_id, expected)).stock;
    }
    return restored;
  }

  async function run() {
    if (running || store.get().api === 'offline') return;
    running = true;
    again.hidden = false;
    again.setLoading(true);
    const rows = new Map(snippets.map((snippet) => [snippet.id, row(snippet)]));
    rows.forEach((view) => view.setPending());
    const note = h('p.cmp-proof__note');
    const table = h(
      'div.cmp-proof__scroll.scroll-x',
      h(
        'table.cmp-proof__table',
        h('caption.sr-only', 'Résultat, durée et octets de chaque écriture'),
        h(
          'thead',
          h(
            'tr',
            h('th', { scope: 'col' }, 'Écriture'),
            h('th', { scope: 'col' }, 'Nouveau stock'),
            h('th', { scope: 'col' }, 'Durée médiane'),
            h('th', { scope: 'col' }, '× local'),
            h('th', { scope: 'col' }, 'Octets sur le fil'),
            h('th', { scope: 'col' }, h('span.sr-only', 'État')),
          ),
        ),
        h('tbody', [...rows.values()].map((view) => view.el)),
      ),
    );
    clear(live);
    clear(body, table, note, live);

    try {
      const result = await api.post('/api/code-compare/run', { product_id: data.product_id, quantity: data.quantity });
      const shares = logShares(result.runs);
      result.runs.forEach((entry, index) => rows.get(entry.id)?.setRun(entry, shares[index]));
      clear(
        note,
        `Durées : médiane de ${plural(result.repeats, 'appel')} des fonctions affichées plus haut, traces coupées (barres en échelle logarithmique). `,
        'Octets : requête → réponse d’un appel tracé de la même procédure sur chaque protocole.',
      );

      let restored = null;
      let bytesError = null;
      rows.forEach((view) => view.setBytesPending());
      try {
        restored = await measureBytes(rows, result.stock_before);
      } catch (error) {
        bytesError = error;
        rows.forEach((view) => view.setBytes(null));
      }
      if (!env.alive()) return;
      clear(
        live,
        verdict(result, snippets, restored),
        bytesError ? Callout({ tone: 'warning', title: 'Octets non mesurés', text: bytesError.message ?? 'Les appels tracés ont échoué.' }) : null,
      );
    } catch (error) {
      if (!env.alive()) return;
      clear(body, failure(error));
    } finally {
      running = false;
      again.setLoading(false);
      env.refreshProduct();
    }
  }

  env.track(
    store.subscribe('api', (value) => {
      primary.setDisabled(value === 'offline');
      again.setDisabled(value === 'offline');
    }),
  );
  showEmpty();

  const section = Section(
    {
      title: 'La preuve par l’exécution',
      description: `${count[0].toUpperCase()}${count.slice(1)} textes différents, une seule procédure côté serveur. Si la transparence tient, les ${count} chemins rendent exactement le même stock.`,
      id: 'compare-proof',
    },
    Card({ title: `Les mêmes arguments, par ${count} chemins`, subtitle: `${data.operation}, puis remise du stock à sa valeur de départ`, icon: 'play', actions: again, class: 'cmp-proof' }, body),
  );
  section.run = run;
  section.runLabel = runLabel;
  return section;
}
