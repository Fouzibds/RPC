/**
 * Transparence — « L'illusion a un prix » : la même ligne de code exécutée en local puis à
 * travers le proxy de chaos, avec un réseau dégradé et un délai d'attente au choix. Microsecondes
 * contre centaines de millisecondes, et une erreur qu'un appel local ne lève jamais.
 */

import { h, clear, on } from '../../core/dom.js';
import { fmtMs, fmtNumber } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { describeNetwork } from '../../core/network.js';
import { Badge, Button, Callout, Card, EmptyState, ProtocolChip, Section, Skeleton, Toggle } from '../../components/ui.js';
import { fmtCall, fmtFold, intentText, splitDifference } from './model.js';
import { readProduct, restoreStock, sleep, updateStock } from './ops.js';

const DEGRADED_MS = 200;
const TIMEOUT_MS = 100;
const REMOTE = 'custom';
const CONDITION_FIELDS = ['latency_ms', 'jitter_ms', 'spike_probability', 'spike_ms', 'reset_probability', 'blackhole', 'down', 'bandwidth_kbps'];

/** Corps de `PUT /api/network` qui rétablit des conditions relevées plus tôt. */
function restorePayload(previous) {
  if (!previous) return { preset: 'ideal' };
  if (previous.preset && previous.preset !== 'custom') return { preset: previous.preset };
  return Object.fromEntries(CONDITION_FIELDS.filter((field) => previous[field] !== undefined).map((field) => [field, previous[field]]));
}

function codeLine(id, text, differing, caption) {
  const index = differing ? text.indexOf(differing) : -1;
  const parts = index < 0 ? [text] : [text.slice(0, index), h('mark.cmp-same__diff', differing), text.slice(index + differing.length)];
  return h('div.cmp-same__row', ProtocolChip(id, { short: true, size: 'sm' }), h('code.cmp-same__code', parts), h('span.cmp-same__caption', caption));
}

function outcomeTile({ id, caption, reply, share, extra }) {
  const failed = !reply.ok;
  return h(
    'div.cmp-outcome',
    { dataset: { state: failed ? 'error' : 'ok' }, style: { '--proto': protocol(id).color } },
    h('div.cmp-outcome__head', ProtocolChip(id, { size: 'sm' }), h('span.cmp-outcome__caption', caption)),
    h('div.cmp-outcome__metric', h('span.cmp-outcome__value.num', fmtCall(reply.duration_ms)), reply.cold ? Badge({ label: 'connexion ouverte pendant l’appel', size: 'sm', variant: 'outline' }) : null),
    h('div.cmp-outcome__bar', { 'aria-hidden': 'true' }, h('span.cmp-outcome__fill', { style: { '--share': String(share) } })),
    failed
      ? h(
          'div.cmp-outcome__error',
          h('div.cmp-outcome__error-head', icon('circle-alert', { size: 14 }), h('code', reply.error?.type ?? 'Erreur'), Badge({ label: reply.error?.code ?? 'ERREUR', tone: 'danger', size: 'sm', mono: true })),
          h('p', reply.error?.message ?? ''),
        )
      : h('div.cmp-outcome__result', icon('circle-check', { size: 14 }), h('code', `new_stock = ${fmtNumber(reply.result?.new_stock)}`)),
    extra ?? null,
  );
}

function failureList(local, remote) {
  if (!remote.failures?.length) return null;
  const shared = new Set(local.failures ?? []);
  return h(
    'div.cmp-fail',
    h('span.t-label', 'Ce qui peut faire échouer l’appel'),
    h(
      'ul.cmp-fail__list',
      remote.failures.map((failure) =>
        h(
          'li',
          { dataset: { remote: String(!shared.has(failure)) } },
          h('span.cmp-fail__icon', { 'aria-hidden': 'true' }, icon(shared.has(failure) ? 'circle' : 'triangle-alert', { size: 13 })),
          h('span.cmp-fail__text', failure),
          Badge({ label: shared.has(failure) ? 'local et distant' : 'distant seulement', tone: shared.has(failure) ? 'neutral' : 'danger', size: 'sm' }),
        ),
      ),
    ),
  );
}

/**
 * Section « L'illusion a un prix ». Le nœud renvoyé porte `run()` et `card` (pour le raccourci clavier).
 * Renvoie `null` si le laboratoire ne fournit pas les écritures locale et « RPC maison ».
 * @param {Object} data Réponse de `GET /api/code-compare`.
 * @param {Object} env Environnement de la page (`api`, `store`, `toast`, `track`, `alive`, `refreshProduct`).
 * @returns {(HTMLElement & {run: () => Promise<void>, card: HTMLElement})|null}
 */
export function illusionSection(data, env) {
  const { api, store, toast } = env;
  const local = data.snippets.find((snippet) => snippet.id === 'local');
  const remote = data.snippets.find((snippet) => snippet.id === REMOTE);
  if (!local || !remote) return null;

  const localLine = intentText(local.code, data.method);
  const remoteLine = intentText(remote.code, data.method);
  const difference = splitDifference(localLine, remoteLine);
  let override = null;
  let graceUntil = 0;
  let running = false;

  /* --- Réseau ---------------------------------------------------------------------------- */

  const networkLine = h('p.cmp-net', { role: 'status' });
  function paintNetwork(conditions) {
    const info = describeNetwork(conditions);
    clear(networkLine, icon(info.icon, { size: 14 }), h('span', 'Réseau simulé :'), h('strong', info.known ? `${info.label}${info.detail ? ` (${info.detail})` : ''}` : 'inconnu'));
    networkLine.dataset.tone = info.tone;
  }

  async function putNetwork(body) {
    graceUntil = performance.now() + 1500;
    const reply = await api.put('/api/network', body);
    const conditions = reply?.conditions ?? reply;
    store.set({ network: conditions });
    return conditions;
  }

  async function setDegraded(enabled) {
    degraded.setDisabled(true);
    try {
      if (enabled) {
        const previous = store.get().network ?? (await api.get('/api/network')).conditions;
        override = { previous };
        await putNetwork({ preset: 'ideal', latency_ms: DEGRADED_MS });
      } else if (override) {
        const { previous } = override;
        override = null;
        await putNetwork(restorePayload(previous));
      }
    } catch (error) {
      if (enabled) override = null;
      degraded.setChecked(!enabled);
      if (env.alive()) toast.error('Réseau simulé inchangé', { description: error.message });
    } finally {
      degraded.setDisabled(store.get().api === 'offline');
    }
  }

  const degraded = Toggle({
    label: `Réseau dégradé (${fmtMs(DEGRADED_MS, { decimals: 0 })})`,
    description: 'Ajoute cette latence aller-retour sur les proxys de chaos. Rétabli en quittant la page.',
    onChange: (enabled) => setDegraded(enabled),
  });
  const deadline = Toggle({
    label: `Échéance de ${fmtMs(TIMEOUT_MS, { decimals: 0 })}`,
    description: 'Le client abandonne s’il n’a pas de réponse à temps.',
  });

  /* --- Appels ---------------------------------------------------------------------------- */

  const results = h('div.cmp-ill__results', { role: 'status', 'aria-live': 'polite' });
  const callButton = Button({ label: 'Appeler des deux côtés', variant: 'primary', icon: 'play', id: 'compare-illusion-run', onClick: () => run() });

  function showEmpty() {
    clear(
      results,
      EmptyState({
        icon: 'timer',
        title: 'Aucun appel pour l’instant',
        text: 'Appelez les deux chemins, puis dégradez le réseau et recommencez : seule la seconde ligne change de comportement.',
        size: 'sm',
      }),
    );
  }

  function aftermath(reply, restored, before) {
    if (reply.ok || restored.found === null) return null;
    const applied = restored.found !== before;
    return h(
      'p.cmp-outcome__aftermath',
      { dataset: { applied: String(applied) } },
      applied
        ? `Et pourtant le stock est passé de ${fmtNumber(before)} à ${fmtNumber(restored.found)} : le serveur a exécuté la procédure, le client n’en a rien su.`
        : `Le stock est resté à ${fmtNumber(before)} : la requête n’a pas été exécutée.`,
      restored.stock === before && applied ? ' Stock rétabli.' : '',
    );
  }

  function statement(localReply, remoteReply) {
    if (!localReply.ok) return Callout({ tone: 'warning', title: 'L’appel local a échoué', text: localReply.error?.message ?? '' });
    if (remoteReply.ok) {
      const ratio = localReply.duration_ms > 0 ? remoteReply.duration_ms / localReply.duration_ms : NaN;
      return Callout({
        tone: 'info',
        title: Number.isFinite(ratio) ? `Même ligne, même résultat — ${fmtFold(ratio)} plus lent` : 'Même ligne, même résultat',
        text: `L’appel local rend ${fmtNumber(localReply.result?.new_stock)} en ${fmtCall(localReply.duration_ms)} ; le stub rend ${fmtNumber(remoteReply.result?.new_stock)} en ${fmtCall(remoteReply.duration_ms)}. Rien dans le code ne signale cet écart.`,
      });
    }
    return Callout({
      tone: 'danger',
      title: `La même ligne vient de lever ${remoteReply.error?.type ?? 'une erreur'}`,
      text: `Après ${fmtCall(remoteReply.duration_ms)}, le stub abandonne (${remoteReply.error?.code ?? 'erreur'}). L’appel local, lui, a rendu ${fmtNumber(localReply.result?.new_stock)} en ${fmtCall(localReply.duration_ms)} : une fonction locale ne peut pas échouer ainsi.`,
    });
  }

  async function run() {
    if (running || store.get().api === 'offline') return;
    running = true;
    callButton.setLoading(true);
    clear(results, h('div.cmp-outcomes', Skeleton({ variant: 'block', height: 132 }), Skeleton({ variant: 'block', height: 132 })));
    const timeoutMs = deadline.checked ? TIMEOUT_MS : undefined;
    const call = { method: data.method, productId: data.product_id, delta: -data.quantity, timeoutMs };
    try {
      const before = (await readProduct(api, data.product_id)).stock;
      const localReply = await updateStock(api, { ...call, protocol: 'local' });
      if (before !== null) await restoreStock(api, data.method, data.product_id, before);
      let remoteReply = await updateStock(api, { ...call, protocol: REMOTE, viaProxy: true });
      if (remoteReply.ok && remoteReply.cold && before !== null) {
        /* Premier appel de la voie : sa durée comprend l'ouverture de la connexion. On mesure le suivant. */
        await restoreStock(api, data.method, data.product_id, before);
        remoteReply = await updateStock(api, { ...call, protocol: REMOTE, viaProxy: true });
      }
      if (!remoteReply.ok && remoteReply.error?.retryable) {
        /* Issue inconnue : la requête est peut-être encore en route. On lui laisse le temps d'arriver avant de constater. */
        await sleep(Math.min(2500, Number(store.get().network?.latency_ms ?? 0) + 350));
      }
      const restored = before === null ? { found: null, stock: null } : await restoreStock(api, data.method, data.product_id, before);
      if (!env.alive()) return;
      const longest = Math.max(localReply.duration_ms, remoteReply.duration_ms) || 1;
      clear(
        results,
        h(
          'div.cmp-outcomes',
          outcomeTile({ id: 'local', caption: 'dans le processus', reply: localReply, share: localReply.duration_ms / longest }),
          outcomeTile({
            id: REMOTE,
            caption: 'via le proxy de chaos',
            reply: remoteReply,
            share: remoteReply.duration_ms / longest,
            extra: aftermath(remoteReply, restored, before),
          }),
        ),
        h('p.cmp-ill__scale', `Barres à la même échelle linéaire${localReply.duration_ms / longest < 0.01 ? ' : à cette échelle, l’appel local ne se voit plus' : ''}.`),
        statement(localReply, remoteReply),
      );
    } catch (error) {
      if (!env.alive()) return;
      const offline = Boolean(error?.offline);
      clear(
        results,
        Callout({
          tone: offline ? 'warning' : 'danger',
          icon: offline ? 'wifi-off' : undefined,
          title: offline ? 'Laboratoire injoignable' : 'L’appel n’a pas pu être lancé',
          text: offline ? 'Le serveur du tableau de bord ne répond pas : réessayez dès qu’il est de retour.' : (error?.message ?? 'Erreur inattendue.'),
          actions: Button({ label: 'Réessayer', icon: 'refresh-cw', size: 'sm', onClick: () => run() }),
        }),
      );
    } finally {
      running = false;
      callButton.setLoading(false);
      env.refreshProduct();
    }
  }

  /* --- Abonnements et nettoyage ---------------------------------------------------------- */

  env.track(
    store.subscribe(
      'network',
      (conditions) => {
        paintNetwork(conditions);
        const ours = conditions && conditions.preset === 'custom' && Number(conditions.latency_ms) === DEGRADED_MS;
        if (override && !ours && performance.now() > graceUntil) {
          override = null; // le réseau a été réglé ailleurs : ce n'est plus à cette page de le rétablir
          degraded.setChecked(false);
        }
      },
      { immediate: true },
    ),
  );
  env.track(
    store.subscribe(
      'api',
      (value) => {
        const offline = value === 'offline';
        callButton.setDisabled(offline);
        degraded.setDisabled(offline);
      },
      { immediate: true },
    ),
  );
  env.track(
    on(window, 'pagehide', () => {
      if (!override) return;
      fetch('/api/network', { method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(restorePayload(override.previous)) }).catch(() => {});
    }),
  );
  env.track(() => {
    if (!override) return;
    const { previous } = override;
    override = null;
    putNetwork(restorePayload(previous)).catch(() => {});
  });

  showEmpty();
  const card = Card(
    {
      title: 'La même ligne, deux mondes',
      subtitle: 'Un appel en mémoire et un appel à travers le réseau simulé',
      icon: 'hourglass',
      class: 'cmp-ill',
      footer: [
        h('span', 'Échéances, nouvelles tentatives, idempotence, disjoncteur : ce que l’appelant doit prévoir quand le réseau s’en mêle.'),
        Button({ label: 'Explorer les pannes dans Chaos réseau', href: '#/chaos', iconRight: 'arrow-right', size: 'sm' }),
      ],
    },
    h(
      'div.cmp-ill__grid',
      h('div.cmp-ill__controls', h('span.t-label', 'Conditions'), h('div.cmp-ill__toggles', degraded, deadline), networkLine),
      h(
        'div.cmp-ill__stage',
        h(
          'div.cmp-same',
          h('span.t-label', 'Le code appelant'),
          codeLine('local', localLine, difference.left, 'objet en mémoire'),
          codeLine(REMOTE, remoteLine, difference.right, 'mandataire réseau'),
        ),
        h('div.cmp-ill__action', callButton, h('span.cmp-ill__hint', 'Les deux appels partent du même stock, rétabli ensuite.')),
        results,
      ),
      failureList(local, remote),
    ),
  );

  const section = Section(
    {
      title: 'L’illusion a un prix',
      description: 'Le code a la forme d’un appel local ; l’appel, lui, traverse un réseau. Il coûte des ordres de grandeur de plus et peut échouer sans dire si le serveur a agi.',
      id: 'compare-illusion',
    },
    card,
  );
  section.run = run;
  section.card = card;
  return section;
}
