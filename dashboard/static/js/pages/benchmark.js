/**
 * Benchmark (#/benchmark) — le banc d'essai : configurer, lancer, regarder les mesures se
 * remplir en direct, lire le verdict, exporter.
 *
 * La page ne calcule aucune mesure : elle affiche le rapport de `run_full_benchmark`
 * (`GET /api/benchmark/latest`, `GET /api/reports/{nom}`) ou, pendant une exécution, les
 * sections partielles publiées par la tâche de fond (messages WebSocket `job`, doublés par
 * `GET /api/jobs/{id}` quand le flux temps réel n'est pas disponible).
 */

import { refreshStatus } from '../core/api.js';
import { h, clear, on } from '../core/dom.js';
import { fmtNumber } from '../core/format.js';
import { hasBlockingLayer } from '../core/layers.js';
import { Badge, Button, PageHeader } from '../components/ui.js';
import { createConfigPanel } from './benchmark/config.js';
import { createHighlights } from './benchmark/highlights.js';
import { Intro } from './benchmark/intro.js';
import { createLatencySection } from './benchmark/latency.js';
import { SUITE_IDS, fmtDate, sectionSize } from './benchmark/model.js';
import { createNetworkSection } from './benchmark/network.js';
import { createPayloadSection } from './benchmark/payload.js';
import { createReportFooter } from './benchmark/report.js';
import { createSerializationSection } from './benchmark/serialization.js';
import { LoadingState, PageAlert, Toc } from './benchmark/states.js';

const POLL_LIVE_MS = 2500;
const POLL_FALLBACK_MS = 700;
const PARTIAL_KEYS = ['payload', 'serialization', 'latency', 'network', 'highlights'];
export default {
  id: 'benchmark',
  title: 'Benchmark',
  subtitle: 'Tailles, latences et débit mesurés sur cette machine',
  icon: 'gauge',
  /**
   * @param {HTMLElement} container Conteneur vide fourni par la coquille.
   * @param {{store: Object, api: Object, ws: Object, navigate: Function, toast: Object, onCleanup: Function}} ctx
   * @returns {() => void} Nettoyage appelé quand on quitte la page.
   */
  mount(container, ctx) {
    const { api, ws, store, toast } = ctx;
    const cleanups = [];
    const track = (chart) => {
      cleanups.push(() => chart.destroy());
      return chart;
    };
    /** `phase` : chargement du dernier rapport ; `report` : rapport complet ; `live` : rapport partiel d'une mesure en cours. */
    const state = { phase: 'loading', error: null, report: null, live: null, job: null };
    let disposed = false;
    let starting = false;
    let pollTimer = 0;

    /* --- Interface -------------------------------------------------------------------------- */
    const meta = h('div.bench-meta');
    const toc = Toc();
    const header = PageHeader({
      eyebrow: 'Laboratoire',
      title: 'Benchmark',
      icon: 'gauge',
      description:
        'Ce que coûte un appel distant, en octets et en temps. Chaque suite interroge le laboratoire en marche : les valeurs sont celles de cette machine, ce sont les écarts entre protocoles et les ordres de grandeur qui se comparent.',
      meta: [meta, toc],
    });

    const config = createConfigPanel({ store, onRun: () => start() });
    cleanups.push(() => config.destroy());

    const alerts = h('div.bench-alerts', { hidden: true });
    const loading = LoadingState();
    const intro = Intro({
      onQuick: () => {
        config.applyPreset('quick');
        start();
      },
    });
    const requestSuite = (suite) => {
      config.enableSuite(suite);
      config.el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      start();
    };
    const sectionProps = { track, onRequest: requestSuite, onCleanup: (fn) => cleanups.push(fn) };
    const sections = [createHighlights(), createPayloadSection(sectionProps), createLatencySection(sectionProps), createSerializationSection(sectionProps), createNetworkSection(sectionProps)];
    const footer = createReportFooter({
      api,
      onLoad: (report) => {
        state.report = report;
        render();
        toast.success('Rapport chargé', { description: report.id });
      },
    });
    const results = h('div.bench-results', { hidden: true }, sections.map((section) => section.el), footer.el);
    container.append(header, config.el, alerts, loading, intro, results);

    /* --- Rendu ------------------------------------------------------------------------------ */
    const isOffline = () => store.get().api === 'offline';
    const runState = () => ({
      running: Boolean(state.job),
      phase: state.job?.phase ?? '',
      suites: state.live?.config?.suites ?? (state.job ? SUITE_IDS : []),
    });

    function drawMeta(view, run) {
      const config = view?.config;
      clear(
        meta,
        run.running ? Badge({ label: 'Mesure en cours', tone: 'accent', dot: true, pulse: true }) : null,
        !run.running && view?.created_at ? Badge({ label: `Rapport du ${fmtDate(view.created_at)}`, icon: 'history' }) : null,
        !run.running && !view && state.phase === 'ready' ? Badge({ label: 'Aucun rapport' }) : null,
        config?.method ? Badge({ label: config.method, mono: true }) : null,
        config?.iterations ? Badge({ label: `${fmtNumber(config.iterations)} appels × ${fmtNumber(config.protocols?.length ?? 0)} protocoles` }) : null,
      );
      toc.hidden = !view;
    }

    function drawAlerts(view) {
      const content = PageAlert({
        offline: isOffline(),
        failed: state.phase === 'error' && !state.error?.offline,
        hasView: Boolean(view),
        tracking: Boolean(state.job),
        message: state.error?.message,
        onRetry: () => reconnect(),
      });
      clear(alerts, content);
      alerts.hidden = !content;
    }

    function render() {
      if (disposed) return;
      const view = state.live ?? state.report;
      const run = runState();
      const offline = isOffline();
      loading.hidden = state.phase !== 'loading' || Boolean(view) || offline;
      intro.hidden = Boolean(view) || state.phase !== 'ready' || offline;
      intro.setDisabled(run.running || starting);
      results.hidden = !view;
      drawAlerts(view);
      drawMeta(view, run);
      if (!view) return;
      for (const section of sections) section.update(view, run);
      footer.update(view, run);
    }

    /* --- Dernier rapport --------------------------------------------------------------------- */
    async function loadLatest() {
      state.phase = 'loading';
      state.error = null;
      render();
      try {
        const report = await api.get('/api/benchmark/latest');
        if (disposed) return;
        state.report = report ?? state.report;
        state.phase = 'ready';
      } catch (error) {
        if (disposed) return;
        state.phase = 'error';
        state.error = error;
      }
      render();
    }

    /** Reprend le suivi d'un benchmark déjà en cours (page rechargée, autre onglet). */
    async function resume() {
      try {
        const answer = await api.get('/api/jobs');
        if (!disposed && !state.job && answer?.active?.kind === 'benchmark') attach(answer.active, null);
      } catch {
        // Laboratoire injoignable : `loadLatest` porte déjà l'état d'erreur.
      }
    }

    async function reconnect() {
      await refreshStatus();
      if (disposed) return;
      if (state.phase !== 'ready') await loadLatest();
      else render();
      footer.refresh();
      if (!state.job) resume();
    }

    /* --- Suivi d'une mesure ------------------------------------------------------------------ */
    function schedulePoll(delay) {
      window.clearTimeout(pollTimer);
      pollTimer = window.setTimeout(poll, delay ?? (store.get().ws === 'live' ? POLL_LIVE_MS : POLL_FALLBACK_MS));
    }

    async function poll() {
      const job = state.job;
      if (!job || disposed) return;
      try {
        const snapshot = await api.get(`/api/jobs/${encodeURIComponent(job.id)}`);
        if (disposed || state.job !== job) return;
        applyJob(snapshot);
      } catch (error) {
        if (disposed || state.job !== job) return;
        if (error.status === 404) {
          failRun({ message: 'La tâche a disparu : le laboratoire a redémarré pendant la mesure.' });
          return;
        }
      }
      if (state.job === job) schedulePoll();
    }

    function attach(summary, runConfig) {
      state.job = { id: summary.id ?? summary.job_id, phase: summary.phase ?? '', progress: summary.progress ?? 0, message: summary.message ?? '', created_at: summary.created_at ?? null };
      state.live = { id: null, created_at: null, config: runConfig, environment: null, payload: null, serialization: null, latency: null, network: null, highlights: [] };
      config.setNotice(null);
      config.setRunning(state.job, runConfig?.suites);
      render();
      schedulePoll(runConfig ? undefined : 0);
    }

    function applyJob(message) {
      const job = state.job;
      if (message.state === 'error') {
        failRun(message.error ?? { message: message.message });
        return;
      }
      if (message.state === 'done') {
        if (message.result) finishRun(message.result, message.message);
        else schedulePoll(0);
        return;
      }
      let redraw = false;
      if (message.params && !state.live.config) {
        state.live.config = message.params;
        job.created_at = message.created_at ?? job.created_at;
        config.setRunning(job, message.params.suites);
        redraw = true;
      }
      if ((message.progress ?? 0) >= job.progress) {
        redraw = redraw || (Boolean(message.phase) && message.phase !== job.phase);
        job.progress = message.progress ?? job.progress;
        job.phase = message.phase || job.phase;
        job.message = message.message || job.message;
      }
      for (const key of PARTIAL_KEYS) {
        const data = message.partial?.[key];
        if (data && sectionSize(key, data) !== sectionSize(key, state.live[key])) {
          state.live[key] = data;
          redraw = true;
        }
      }
      config.setProgress(job);
      if (redraw) render();
    }

    function stopRun() {
      window.clearTimeout(pollTimer);
      state.job = null;
      state.live = null;
      config.setRunning(null);
    }

    function finishRun(report, message) {
      stopRun();
      state.report = report;
      state.phase = 'ready';
      render();
      footer.refresh();
      toast.success('Benchmark terminé', { description: message || report.id });
    }

    function failRun(error) {
      stopRun();
      const text = error?.message || 'La tâche de fond s’est arrêtée sans résultat.';
      config.setNotice({ tone: 'danger', title: 'La mesure a échoué', text, actions: Button({ label: 'Relancer', icon: 'rotate-ccw', size: 'sm', onClick: () => start() }) });
      render();
      toast.error('La mesure a échoué', { description: text });
    }

    async function start() {
      if (state.job || starting || disposed) return;
      if (!config.valid()) {
        toast.warn('Configuration incomplète', { description: 'Choisissez au moins une suite, un protocole et une latence de balayage.' });
        return;
      }
      if (isOffline()) {
        toast.warn('Laboratoire injoignable', { description: 'Impossible de lancer une mesure pour l’instant.' });
        return;
      }
      starting = true;
      config.setStarting(true);
      config.setNotice(null);
      render();
      const body = config.value();
      try {
        const answer = await api.post('/api/benchmark/run', body);
        if (disposed) return;
        const runConfig = answer.config ?? body;
        if (state.job && state.job.id === answer.job_id) {
          // Le premier message « job » est arrivé avant la réponse HTTP : il ne manquait que la configuration.
          state.live.config = state.live.config ?? runConfig;
          config.setRunning(state.job, runConfig.suites);
          render();
        } else {
          attach(answer.job ?? { id: answer.job_id }, runConfig);
        }
      } catch (error) {
        if (disposed) return;
        const active = error.body?.error?.job;
        if (error.status === 409 && active?.kind === 'benchmark') {
          attach(active, null);
          toast.info('Un benchmark est déjà en cours', { description: 'La page suit sa progression ; relancez à la fin si besoin.' });
        } else if (error.status === 409) {
          config.setNotice({ tone: 'warning', title: 'Le laboratoire est occupé', text: error.message, actions: Button({ label: 'Réessayer', icon: 'refresh-cw', size: 'sm', onClick: () => start() }) });
        } else {
          config.setNotice({
            tone: error.offline ? 'warning' : 'danger',
            title: error.offline ? 'Laboratoire injoignable' : error.status === 400 || error.status === 422 ? 'Le laboratoire a refusé cette configuration' : 'Le lancement a échoué',
            text: error.message,
          });
        }
      } finally {
        starting = false;
        if (!disposed) {
          config.setStarting(false);
          render();
        }
      }
    }

    /* --- Évènements -------------------------------------------------------------------------- */
    cleanups.push(
      ws.on('job', (message) => {
        if (message.kind !== 'benchmark') return;
        if (!state.job) {
          if (message.state === 'running') attach(message, null);
          else if (message.state === 'done' && message.result) finishRun(message.result, message.message);
          return;
        }
        if ((message.job_id ?? message.id) === state.job.id) applyJob(message);
      }),
      ws.on('open', () => {
        if (state.job) schedulePoll(0);
      }),
      store.subscribe('api', (status, previous) => {
        config.setOffline(status === 'offline');
        footer.setOffline(status === 'offline');
        if (status === 'online' && previous === 'offline') reconnect();
        else render();
      }),
      on(window, 'keydown', (event) => {
        if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey || hasBlockingLayer()) return;
        event.preventDefault();
        start();
      }),
      () => window.clearTimeout(pollTimer),
    );

    config.setOffline(isOffline());
    footer.setOffline(isOffline());
    render();
    loadLatest();
    resume();
    footer.refresh();

    return () => {
      disposed = true;
      for (const cleanup of cleanups) cleanup();
    };
  },
};
