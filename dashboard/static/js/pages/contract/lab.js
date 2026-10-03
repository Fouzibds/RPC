/**
 * Contrat & IDL — « Expérimenter la rupture » : les scénarios regroupés par middleware, chacun
 * rejouable sur le serveur « contrat v2 » du laboratoire, et le bilan de l'ensemble.
 */

import { h, clear, uid } from '../../core/dom.js';
import { protocol } from '../../core/protocols.js';
import { Badge, Button, Callout, IconButton, ProtocolChip, Section, Skeleton, Toggle } from '../../components/ui.js';
import { kindInfo, plural, scenarioGroups } from './model.js';
import { resultPanel } from './result.js';
import { summaryCard } from './summary.js';

const RUN_URL = '/api/contract/run';

const GROUP_NOTES = {
  grpc: 'Contrat écrit dans service.proto : sur le fil, seuls les numéros et les types de fil voyagent.',
  custom: 'Aucun IDL : le contrat tient aux noms des procédures, des paramètres et des clés JSON.',
};

/**
 * Carte d'un scénario (ou de deux scénarios jumeaux séparés par la validation stricte du serveur).
 */
function scenarioCard({ card, index, outcomes, changes, results, onRun, onChange }) {
  const paired = card.variants.length > 1;
  let active = card.variants.find((variant) => results.has(variant.id)) ?? card.variants[0];
  let open = false;
  let panel = null;
  let failure = null;
  const running = new Set();
  const panelId = uid('scenario');

  const title = h('h4.contract-scn__title');
  const summary = h('p.contract-scn__summary');
  const meta = h('div.contract-scn__meta');
  const verdict = h('div.contract-scn__verdict', { 'aria-live': 'polite' });
  const slot = h('div.contract-scn__panel', { id: panelId, hidden: true, 'aria-live': 'polite' });
  const runButton = Button({ label: 'Exécuter', icon: 'play', onClick: () => onRun(active.id) });
  const expander = IconButton({ icon: 'chevron-down', label: 'Afficher le résultat', tooltip: 'left', class: 'contract-scn__expander', onClick: () => setOpen(!open) });
  expander.setAttribute('aria-controls', panelId);

  const strict = paired
    ? Toggle({
        checked: Boolean(active.strict),
        size: 'sm',
        label: 'Validation stricte côté serveur',
        description: 'Le serveur v2 vérifie ses entrées avant d’exécuter : mêmes octets, autre issue.',
        onChange: (checked) => {
          active = card.variants.find((variant) => Boolean(variant.strict) === checked) ?? active;
          failure = null;
          renderHead();
          if (open) showPanel();
          onRun(active.id);
        },
      })
    : null;

  const el = h(
    'article.contract-scn',
    { style: { '--i': index }, dataset: { protocol: active.protocol } },
    h(
      'div.contract-scn__head',
      h('div.contract-scn__text', title, summary),
      h('div.contract-scn__actions', verdict, runButton, expander),
      meta,
    ),
    strict ? h('div.contract-scn__strict', strict) : null,
    slot,
  );

  function setOpen(next) {
    open = Boolean(next) && (results.has(active.id) || running.has(active.id) || failure !== null);
    if (open && !slot.childElementCount) showPanel();
    slot.hidden = !open;
    el.classList.toggle('is-open', open);
    expander.setAttribute('aria-expanded', String(open));
    expander.setLabel(open ? 'Masquer le résultat' : 'Afficher le résultat');
  }

  function renderHead() {
    const change = changes.get(active.change_id);
    const expected = outcomes.get(active.expected_outcome);
    const result = results.get(active.id);
    const outcome = result ? outcomes.get(result.outcome) : null;
    const kind = change ? kindInfo(change.kind) : null;
    clear(title, active.title);
    clear(summary, active.summary);
    clear(
      meta,
      change
        ? h(
            'button.contract-scn__change',
            { type: 'button', title: 'Retrouver ce changement dans le diff', onClick: () => onChange(change.id) },
            Badge({ label: kind.label, tone: kind.tone, size: 'sm' }),
            h('span.truncate', change.title),
          )
        : null,
      h('span.contract-scn__expected', 'Issue attendue', Badge({ label: expected.short, tone: expected.tone, size: 'sm', variant: 'outline', icon: expected.icon, title: expected.text })),
    );
    clear(verdict, outcome ? Badge({ label: outcome.short.toUpperCase(), tone: outcome.tone, variant: outcome.danger >= 3 ? 'solid' : 'soft', icon: outcome.icon, title: outcome.text }) : null);
    el.dataset.tone = outcome ? outcome.tone : '';
    el.dataset.outcome = outcome ? outcome.id : '';
    runButton.setLabel(result ? 'Rejouer' : 'Exécuter');
    runButton.setIcon(result ? 'rotate-ccw' : 'play');
    runButton.setLoading(running.has(active.id));
    expander.disabled = !result && !running.has(active.id) && failure === null;
  }

  /** Remplit le panneau selon l'état de la variante active : erreur, résultat, attente, rien. */
  function showPanel() {
    panel?.destroy();
    panel = null;
    const result = results.get(active.id);
    slot.dataset.busy = String(running.has(active.id));
    if (failure) {
      clear(
        slot,
        Callout({
          tone: 'danger',
          title: failure.offline ? 'Laboratoire injoignable' : 'Le scénario n’a pas pu être joué',
          text: failure.message,
          actions: Button({ label: 'Réessayer', size: 'sm', icon: 'refresh-cw', onClick: () => onRun(active.id) }),
        }),
      );
    } else if (result) {
      panel = resultPanel(result, outcomes);
      clear(slot, panel.el);
    } else if (running.has(active.id)) {
      clear(slot, h('div.contract-scn__loading', Skeleton({ variant: 'block', height: 84 }), h('div.contract-cols', Skeleton({ lines: 5 }), Skeleton({ lines: 5 }), Skeleton({ lines: 5 }))));
    } else {
      clear(slot);
    }
  }

  renderHead();

  return {
    el,
    ids: card.variants.map((variant) => variant.id),
    /** Le scénario `id` démarre ou s'arrête ; `reveal` ouvre le panneau pendant l'attente. */
    setRunning(id, on, { reveal = false } = {}) {
      if (on) running.add(id);
      else running.delete(id);
      if (id !== active.id) return;
      if (on) failure = null;
      renderHead();
      slot.dataset.busy = String(on);
      if (on && reveal) {
        if (!results.has(id)) showPanel();
        setOpen(true);
      }
    },
    /** Un résultat (déjà rangé dans `results`) vient d'arriver pour `id`. */
    refresh(id, { reveal = false } = {}) {
      if (id !== active.id) return;
      failure = null;
      renderHead();
      if (reveal || open) {
        showPanel();
        setOpen(true);
      } else {
        panel?.destroy();
        panel = null;
        clear(slot);
      }
    },
    /** L'exécution de `id` a échoué : le panneau montre l'erreur et propose de réessayer. */
    fail(id, error) {
      if (id !== active.id) return;
      failure = error;
      renderHead();
      showPanel();
      setOpen(true);
    },
    /** Amène la carte à l'écran, sur la variante `id`, résultat déplié s'il existe. */
    reveal(id) {
      const variant = card.variants.find((candidate) => candidate.id === id);
      if (variant && variant !== active) {
        active = variant;
        failure = null;
        strict?.setChecked(Boolean(active.strict));
        renderHead();
        showPanel();
      }
      setOpen(true);
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      el.classList.remove('is-flash');
      void el.offsetWidth;
      el.classList.add('is-flash');
    },
    destroy: () => panel?.destroy(),
  };
}

/**
 * Section des scénarios de rupture.
 * @param {Object} options
 * @param {Object} options.overview Réponse de `GET /api/contract` (scénarios, changements, derniers résultats).
 * @param {{list: Array<Object>, get: Function}} options.outcomes Catalogue des issues.
 * @param {{post: Function}} options.api Client HTTP du laboratoire.
 * @param {Object} options.toast Notifications.
 * @param {AbortSignal} options.signal Annulé quand on quitte la page.
 * @param {(changeId: string) => void} options.onChange Montre un changement dans le diff.
 * @param {(busy: boolean) => void} options.onBusy Prévenu quand « tout exécuter » démarre et s'arrête.
 * @returns {{el: HTMLElement, runAll: () => Promise<void>, focusScenario: (id: string) => void, destroy: () => void}}
 */
export function labSection({ overview, outcomes, api, toast, signal, onChange, onBusy }) {
  const scenarios = overview.scenarios ?? [];
  const changes = new Map((overview.changes ?? []).map((change) => [change.id, change]));
  const results = new Map(Object.entries(overview.results ?? {}));
  const groups = scenarioGroups(scenarios);
  const cardOf = new Map();
  const cards = [];
  let runningAll = false;

  const summary = summaryCard({ scenarios, outcomes, onRunAll: () => runAll(), onPick: (id) => focusScenario(id) });

  function store(list) {
    for (const result of list ?? []) if (result?.id) results.set(result.id, result);
    summary.update(results);
  }

  async function run(id) {
    const card = cardOf.get(id);
    if (!card || runningAll) return;
    const scenario = scenarios.find((candidate) => candidate.id === id);
    const twins = card.ids.length > 1;
    card.setRunning(id, true, { reveal: true });
    try {
      const reply = await api.post(RUN_URL, twins ? { scenario: id, strict: Boolean(scenario.strict) } : { scenario: id }, { signal });
      if (signal.aborted) return;
      if (!reply?.results?.some((result) => result.id === id)) throw new Error('Le laboratoire n’a renvoyé aucun résultat pour ce scénario.');
      store(reply.results);
      card.setRunning(id, false);
      card.refresh(id, { reveal: true });
    } catch (error) {
      if (signal.aborted) return;
      card.setRunning(id, false);
      card.fail(id, error);
      toast.error('Scénario non joué', { description: error.message });
    }
  }

  async function runAll() {
    if (runningAll) return;
    runningAll = true;
    onBusy(true);
    summary.setBusy(true);
    for (const scenario of scenarios) cardOf.get(scenario.id)?.setRunning(scenario.id, true);
    let failure = null;
    try {
      const reply = await api.post(RUN_URL, { scenario: 'all' }, { signal, timeoutMs: 60000 });
      if (signal.aborted) return;
      store(reply?.results);
    } catch (error) {
      failure = error;
    }
    if (signal.aborted) return;
    runningAll = false;
    onBusy(false);
    for (const scenario of scenarios) {
      const card = cardOf.get(scenario.id);
      card?.setRunning(scenario.id, false);
      if (!failure) card?.refresh(scenario.id);
    }
    summary.setBusy(false);
    if (failure) {
      summary.setError(failure);
      toast.error('Scénarios non joués', { description: failure.message });
      return;
    }
    const silent = [...results.values()].filter((result) => outcomes.get(result.outcome).danger >= 3).length;
    toast.success(`${plural(results.size, 'scénario joué', 'scénarios joués')}`, {
      description: silent ? `${plural(silent, 'corruption silencieuse', 'corruptions silencieuses')} : aucune erreur, des données fausses.` : 'Aucune corruption silencieuse.',
    });
    summary.el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function focusScenario(id) {
    cardOf.get(id)?.reveal(id);
  }

  let index = 0;
  const groupNodes = groups.map((group) => {
    const info = protocol(group.protocol);
    const count = group.cards.reduce((total, card) => total + card.variants.length, 0);
    const nodes = group.cards.map((card) => {
      const controller = scenarioCard({ card, index: index++, outcomes, changes, results, onRun: run, onChange });
      cards.push(controller);
      for (const id of controller.ids) cardOf.set(id, controller);
      return controller.el;
    });
    return h(
      'section.contract-group',
      { dataset: { protocol: group.protocol }, style: { '--c': info.color } },
      h(
        'header.contract-group__head',
        ProtocolChip(group.protocol, { icon: true }),
        h('p.contract-group__note', GROUP_NOTES[group.protocol] ?? info.transport ?? ''),
        h('span.contract-group__count.num', plural(count, 'scénario')),
      ),
      h('div.contract-group__list', nodes),
    );
  });

  summary.update(results);

  const el = Section(
    {
      id: 'contract-lab',
      title: 'Expérimenter la rupture',
      description: 'Un client resté au contrat v1 appelle le serveur « contrat v2 ». Chaque scénario confronte ce qu’il envoie, ce qu’il attend et ce qui se passe réellement.',
    },
    summary.el,
    groupNodes,
  );

  return {
    el,
    runAll,
    focusScenario,
    destroy: () => cards.forEach((card) => card.destroy()),
  };
}
