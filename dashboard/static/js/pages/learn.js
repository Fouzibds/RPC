/**
 * Bilan (#/learn) — la synthèse demandée par le cahier des charges : avantages et inconvénients
 * du RPC, chiffrés par ce que le laboratoire a mesuré (`GET /api/summary`), puis le comparatif,
 * les conseils de choix, les règles à retenir, le glossaire et la correspondance avec le sujet.
 * La page peut lancer elle-même les expériences qui la chiffrent, et s'imprime proprement.
 */

import { h, clear } from '../core/dom.js';
import { registerShortcut } from '../core/shortcuts.js';
import { getTheme, setTheme } from '../core/theme.js';
import { Badge, Button, Callout, Card, Col, EmptyState, Grid, IconButton, PageHeader, Section } from '../components/ui.js';
import { ArgumentColumns, ArgumentsSkeleton } from './learn/arguments.js';
import { EXPERIMENTS, EXPERIMENT_NAMES, runExperiment } from './learn/campaign.js';
import { coverage, loadBilan } from './learn/data.js';
import { Decisions, Fallacies, Rules } from './learn/guidance.js';
import { Matrix, MatrixSkeleton } from './learn/matrix.js';
import { StatusPanel } from './learn/provenance.js';
import { Glossary, Phases } from './learn/reference.js';

const CONTENTS = [
  ['learn-arguments', 'Avantages et inconvénients'],
  ['learn-matrix', 'Comparatif'],
  ['learn-choose', 'Quand choisir quoi'],
  ['learn-rules', 'Règles'],
  ['learn-glossary', 'Glossaire'],
  ['learn-brief', 'Cahier des charges'],
];
const RELATIVE_TIME_REFRESH_MS = 30000;
const PRINT_DATE = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'long', timeStyle: 'short' });

function tableOfContents() {
  return h(
    'nav.learn-toc.learn-noprint',
    { 'aria-label': 'Sections du bilan' },
    CONTENTS.map(([id, label]) => h('button.learn-toc__link', { type: 'button', dataset: { target: id }, onClick: () => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }, label)),
  );
}

export default {
  id: 'learn',
  title: 'Bilan',
  subtitle: 'Avantages et inconvénients, chiffrés par vos mesures',
  icon: 'scale',
  /**
   * @param {HTMLElement} container Conteneur vide fourni par la coquille.
   * @param {{store: Object, api: Object, ws: Object, navigate: Function, toast: Object}} ctx
   * @returns {() => void} Nettoyage appelé quand on quitte la page.
   */
  mount(container, ctx) {
    const { api, ws, store, toast } = ctx;
    const abort = new AbortController();
    const cleanups = [() => abort.abort()];
    const state = { phase: 'loading', summary: null, previous: null, report: null, failures: null, contract: null, error: null, running: false, busy: false, token: 0 };

    const measuredBadge = Badge({ label: 'Chargement…', dot: true });
    const coverageBadge = Badge({ label: '' });
    const announcer = h('p.sr-only', { 'aria-live': 'polite' });
    const printLine = h('p.learn-printline');
    const refresh = IconButton({ icon: 'refresh-cw', label: 'Actualiser le bilan', variant: 'secondary', onClick: () => load() });
    const status = StatusPanel({ onRun: (experiment) => runCampaign(experiment ? [experiment] : null) });
    const notice = h('div.learn-notice');
    const argumentsZone = h('div.learn-zone');
    const matrixZone = h('div.learn-zone');
    const decisionsZone = h('div.learn-zone');
    const rulesZone = h('div.learn-zone');

    const sections = {
      arguments: Section(
        { id: 'learn-arguments', title: 'Avantages et inconvénients', description: 'Chaque carte : l’argument, son chiffre mesuré dans ce laboratoire, la preuve, et la page où le reproduire.' },
        argumentsZone,
      ),
      matrix: Section({ id: 'learn-matrix', title: 'Comparatif', description: 'La même procédure appelée de quatre façons, critère par critère.' }, matrixZone),
    };

    container.append(
      printLine,
      PageHeader({
        eyebrow: 'Synthèse',
        title: 'Bilan',
        description: 'Ce que le RPC apporte, ce qu’il coûte, et quand lui préférer autre chose — chaque argument appuyé sur ce que ce laboratoire a mesuré.',
        icon: 'scale',
        actions: h('div.learn-actions.learn-noprint', refresh, Button({ label: 'Imprimer / exporter en PDF', icon: 'file-text', onClick: () => window.print() })),
        meta: [measuredBadge, coverageBadge, tableOfContents()],
      }),
      announcer,
      status,
      notice,
      sections.arguments,
      sections.matrix,
      Section({ id: 'learn-choose', title: 'Quand choisir quoi', description: 'Quatre situations, un choix pour chacune, et la donnée du bilan qui l’appuie.' }, decisionsZone),
      Section(
        { id: 'learn-rules', title: 'Ne jamais traiter un appel distant comme un appel local', description: 'Ce que le laboratoire de pannes enseigne, en six règles — et les huit illusions qu’elles corrigent.' },
        Grid(Col({ span: 7, md: 12 }, rulesZone), Col({ span: 5, md: 12 }, Fallacies())),
      ),
      Section({ id: 'learn-glossary', title: 'Glossaire', description: 'Dix mots pour suivre un appel de bout en bout, puis le rendre robuste.' }, Glossary()),
      Section(
        { id: 'learn-brief', title: 'Correspondance avec le cahier des charges', description: 'Les quatre phases du sujet : où elles vivent dans le code, dans cette application et en ligne de commande.' },
        Phases(),
      ),
    );

    function totals() {
      return coverage([...state.summary.advantages, ...state.summary.drawbacks]);
    }

    function paintHeader() {
      const ready = state.phase === 'ready';
      const labels = { loading: ['Chargement…', 'neutral'], offline: ['Laboratoire injoignable', 'danger'], error: ['Bilan indisponible', 'danger'] };
      const [label, tone] = ready ? (state.summary.measured ? ['Chiffré par vos mesures', 'success'] : ['Pas encore chiffré', 'warning']) : labels[state.phase];
      measuredBadge.setLabel(label);
      measuredBadge.setTone(tone);
      const { measured, total } = ready ? totals() : { measured: 0, total: 0 };
      coverageBadge.hidden = !total;
      if (total) {
        coverageBadge.setLabel(`${measured} ${measured > 1 ? 'arguments chiffrés' : 'argument chiffré'} sur ${total}`);
      }
    }

    function unavailable() {
      const retry = Button({ label: 'Réessayer', icon: 'refresh-cw', onClick: () => load() });
      if (state.phase === 'offline') {
        return Card(
          { padding: 'none' },
          EmptyState({
            icon: 'unplug',
            tone: 'warning',
            title: 'Laboratoire injoignable',
            text: 'Les chiffres du bilan viennent du laboratoire, qui ne répond pas. Relancez-le avec « python main.py --dashboard » : la page se rechargera dès qu’il sera revenu. Les sections de référence ci-dessous restent consultables.',
            action: retry,
          }),
        );
      }
      return Callout({ tone: 'danger', title: 'Le bilan n’a pas pu être chargé', text: state.error?.message ?? 'Erreur inconnue.', actions: retry });
    }

    function render() {
      const { phase, summary } = state;
      const ready = phase === 'ready';
      const down = phase === 'offline' || phase === 'error';
      status.hidden = down;
      sections.arguments.hidden = down;
      sections.matrix.hidden = down;
      notice.hidden = !down;
      clear(notice, down ? unavailable() : null);
      refresh.disabled = phase === 'loading';
      for (const link of container.querySelectorAll('.learn-toc__link')) link.disabled = Boolean(document.getElementById(link.dataset.target)?.hidden);
      container.setAttribute('aria-busy', String(phase === 'loading'));

      if (phase === 'loading') {
        status.setLoading();
        clear(argumentsZone, ArgumentsSkeleton());
        clear(matrixZone, MatrixSkeleton());
      } else if (ready) {
        status.update(state);
        const empty = !summary.advantages.length && !summary.drawbacks.length;
        clear(
          argumentsZone,
          empty
            ? Card(
                { padding: 'none' },
                EmptyState({
                  icon: 'scale',
                  title: 'Aucun argument dans le bilan',
                  text: 'Le laboratoire n’a renvoyé ni avantage ni inconvénient. Lancez une campagne de mesure pour le remplir.',
                  action: Button({ label: 'Mesurer maintenant', variant: 'primary', icon: 'play', onClick: () => runCampaign(null) }),
                }),
              )
            : ArgumentColumns({ summary, previous: state.previous }),
        );
        clear(matrixZone, Matrix({ summary, report: state.report }));
      }
      clear(decisionsZone, Decisions({ summary: ready ? summary : null }));
      clear(rulesZone, Rules({ summary: ready ? summary : null }));
      paintHeader();
    }

    /** Charge le bilan. `quiet` : rafraîchissement en place, sans repasser par les squelettes. */
    async function load({ quiet = false } = {}) {
      state.token += 1;
      const { token } = state;
      if (!quiet || !state.summary) {
        Object.assign(state, { phase: 'loading', summary: null, previous: null });
        render();
      }
      try {
        const result = await loadBilan(api, { signal: abort.signal, known: { failures: state.failures, contract: state.contract } });
        if (token !== state.token || abort.signal.aborted) return;
        Object.assign(state, result, { phase: 'ready', error: null, previous: state.summary });
        render();
        const { measured, total } = totals();
        clear(announcer, `Bilan à jour : ${measured} arguments chiffrés sur ${total}.`);
      } catch (error) {
        if (token !== state.token || abort.signal.aborted) return;
        if (state.summary) {
          toast.error('Bilan non actualisé', { description: error.message });
          return;
        }
        Object.assign(state, { phase: error.offline ? 'offline' : 'error', error });
        render();
      }
    }

    /** Lance les expériences demandées (ou, à défaut, celles qui manquent ; toutes si rien ne manque). */
    async function runCampaign(only) {
      if (state.running || state.busy || state.phase !== 'ready') return;
      const missing = status.missing();
      const queue = only ?? (missing.length ? missing : [...EXPERIMENTS]);
      const failed = [];
      state.running = true;
      try {
        for (const [index, id] of queue.entries()) {
          const report = (progress, message) => status.setRun({ current: id, queue: queue.slice(index + 1), step: index + 1, steps: queue.length, progress, message });
          report(0, '');
          clear(announcer, `Étape ${index + 1} sur ${queue.length} : ${EXPERIMENT_NAMES[id]}.`);
          try {
            await runExperiment(id, { api, ws, signal: abort.signal, failures: state.failures, report: state.report, catalog: store.get().catalog }, report);
            status.setError(id, null);
          } catch (error) {
            if (abort.signal.aborted) return;
            failed.push(id);
            status.setError(id, error.message);
            if (error.offline) break;
          }
          await load({ quiet: true });
          if (abort.signal.aborted) return;
        }
      } finally {
        state.running = false;
        if (!abort.signal.aborted) status.setRun(null);
      }
      if (failed.length) {
        toast.error(failed.length === queue.length ? 'Aucune mesure n’a abouti' : 'Campagne incomplète', { description: 'Le détail figure sous chaque expérience en échec.' });
      } else if (state.phase === 'ready') {
        const { measured, total } = totals();
        toast.success('Bilan mis à jour', { description: `${measured} arguments chiffrés sur ${total}.` });
      }
    }

    /** Une expérience lancée depuis une autre page occupe le laboratoire : on attend sa fin, puis on recharge. */
    function syncBusy(job) {
      if (state.running) return;
      const wasBusy = state.busy;
      if (!job && !wasBusy) return;
      state.busy = Boolean(job);
      status.setBusy(job ? { title: job.title ?? 'Expérience', message: job.message ?? '', progress: Number(job.progress) || 0 } : null);
      if (wasBusy && !job) load({ quiet: true });
    }

    cleanups.push(
      ws.on('job', (message) => syncBusy(message.state === 'running' ? message : null)),
      ws.on('close', () => syncBusy(null)),
      store.subscribe('stats', (stats) => {
        if (stats && 'job' in stats) syncBusy(stats.job);
      }),
      store.subscribe('api', (value) => {
        if (value === 'online' && (state.phase === 'offline' || state.phase === 'error')) load();
      }),
      registerShortcut({ keys: 'mod+enter', description: 'Mesurer le bilan', group: 'Bilan', run: () => runCampaign(null) }),
    );

    const ticker = window.setInterval(() => {
      if (state.phase === 'ready' && !state.running) status.update(state);
    }, RELATIVE_TIME_REFRESH_MS);
    cleanups.push(() => window.clearInterval(ticker));

    // L'impression se fait toujours en clair, quel que soit le thème à l'écran.
    let themeBeforePrint = null;
    const beforePrint = () => {
      clear(printLine, `RPC Explorer · Benchmark Lab — bilan imprimé le ${PRINT_DATE.format(new Date())}`);
      themeBeforePrint = getTheme();
      if (themeBeforePrint !== 'light') setTheme('light', { persist: false });
    };
    const afterPrint = () => {
      if (themeBeforePrint && themeBeforePrint !== 'light') setTheme(themeBeforePrint, { persist: false });
      themeBeforePrint = null;
    };
    window.addEventListener('beforeprint', beforePrint);
    window.addEventListener('afterprint', afterPrint);
    cleanups.push(() => {
      window.removeEventListener('beforeprint', beforePrint);
      window.removeEventListener('afterprint', afterPrint);
      afterPrint();
    });

    syncBusy(store.get().stats?.job ?? null);
    load();
    return () => cleanups.forEach((cleanup) => cleanup());
  },
};
