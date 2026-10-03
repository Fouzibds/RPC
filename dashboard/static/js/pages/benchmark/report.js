/**
 * Banc d'essai — pied de page : environnement de la mesure, exports (JSON, Markdown, CSV) et
 * rechargement d'un rapport enregistré.
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtNumber } from '../../core/format.js';
import { Button, ButtonGroup, Callout, Card, IconButton, KeyValue, Section, Select, Skeleton } from '../../components/ui.js';
import { toast } from '../../components/toast.js';
import { SUITES, fmtDate } from './model.js';

const EXPORTS = [
  { format: 'json', label: 'JSON', icon: 'file-json', hint: 'Le rapport complet, tel que le serveur l’a enregistré' },
  { format: 'md', label: 'Markdown', icon: 'file-text', hint: 'Tableaux et faits marquants, prêts à coller dans un compte rendu' },
  { format: 'csv', label: 'CSV', icon: 'table', hint: 'Une mesure par ligne, pour un tableur ou pandas' },
];

const fileName = (report) => `${report.id}.json`;

function save(name, blob) {
  const url = URL.createObjectURL(blob);
  const link = h('a', { href: url, download: name, hidden: true });
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * Pied de page du banc d'essai.
 * @param {Object} props
 * @param {Object} props.api Client HTTP du laboratoire (`ctx.api`).
 * @param {(report: Object) => void} props.onLoad Appelé avec un rapport enregistré à afficher.
 * @returns {{el: HTMLElement, update: (view: Object|null, run: import('./model.js').RunState) => void, refresh: () => Promise<void>, setOffline: (on: boolean) => void}}
 */
export function createReportFooter({ api, onLoad }) {
  let report = null;
  let running = false;
  let offline = false;
  let listState = 'loading';
  let reports = [];
  let loadingName = null;

  /* --- Environnement ---------------------------------------------------------------------- */
  const environment = KeyValue({ items: [], columns: 3, layout: 'stacked', dense: true });
  const pending = h('div.bench-report__pending', Skeleton({ lines: 3 }));

  /* --- Exports ---------------------------------------------------------------------------- */
  const exportButtons = EXPORTS.map((item) =>
    Button({ label: item.label, icon: item.icon, size: 'sm', title: item.hint, class: `bench-export bench-export--${item.format}`, onClick: () => download(item) }),
  );

  async function download(item) {
    if (!report) return;
    const button = exportButtons[EXPORTS.indexOf(item)];
    button.setLoading(true);
    try {
      const response = await fetch(`/api/reports/${encodeURIComponent(fileName(report))}?format=${item.format}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(String(response.status));
      save(`${report.id}.${item.format}`, await response.blob());
      toast.success(`Export ${item.label} prêt`, { description: `${report.id}.${item.format}` });
    } catch {
      if (item.format === 'json') {
        // Rapport non enregistré sur disque (ou laboratoire injoignable) : on exporte celui que la page affiche.
        save(`${report.id}.json`, new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
        toast.info('Export JSON prêt', { description: 'Généré à partir du rapport affiché.' });
      } else {
        toast.error(`Export ${item.label} impossible`, { description: 'Le laboratoire n’a pas pu fournir ce rapport : il n’est peut-être pas enregistré sur disque.' });
      }
    } finally {
      button.setLoading(false);
      sync();
    }
  }

  /* --- Rapports enregistrés --------------------------------------------------------------- */
  const picker = Select({
    options: [],
    placeholder: 'Chargement…',
    size: 'sm',
    width: 264,
    disabled: true,
    ariaLabel: 'Rapport enregistré à afficher',
    onChange: (name) => load(name),
  });
  const retry = IconButton({ icon: 'refresh-cw', label: 'Recharger la liste des rapports', size: 'sm', onClick: () => refresh() });
  const notice = h('div.bench-report__notice');

  async function load(name) {
    if (report && name === fileName(report)) return;
    loadingName = name;
    sync();
    try {
      onLoad(await api.get(`/api/reports/${encodeURIComponent(name)}`));
    } catch (error) {
      toast.error('Rapport illisible', { description: error.message });
      picker.setValue(report ? fileName(report) : undefined);
    } finally {
      loadingName = null;
      sync();
    }
  }

  async function refresh() {
    listState = reports.length ? listState : 'loading';
    sync();
    try {
      const answer = await api.get('/api/reports');
      reports = (answer?.reports ?? []).filter((entry) => entry.kind === 'benchmark');
      listState = 'ready';
    } catch (error) {
      listState = error.offline ? 'offline' : 'error';
    }
    sync();
  }

  function sync() {
    const saved = Boolean(report) && reports.some((entry) => entry.name === fileName(report));
    picker.setOptions(
      reports.map((entry) => ({
        value: entry.name,
        label: fmtDate(entry.created_at),
        description: entry.name,
        hint: fmtBytes(entry.size),
      })),
    );
    picker.setValue(saved ? fileName(report) : undefined);
    picker.setDisabled(running || offline || Boolean(loadingName) || listState !== 'ready' || !reports.length);
    // Select ne sait pas changer son texte d'attente après coup : on le réécrit sur place.
    const placeholder = picker.querySelector('.select__placeholder');
    if (placeholder) {
      placeholder.textContent =
        { loading: 'Chargement…', error: 'Liste indisponible', offline: 'Laboratoire injoignable' }[listState] ?? (reports.length ? 'Choisir un rapport…' : 'Aucun rapport enregistré');
    }
    retry.hidden = listState !== 'error' && listState !== 'offline';
    for (const button of exportButtons) button.setDisabled(!report || running);

    clear(
      notice,
      listState === 'error' ? Callout({ tone: 'warning', title: 'La liste des rapports n’a pas pu être lue', text: 'Le rapport affiché reste exportable en JSON.', actions: Button({ label: 'Réessayer', size: 'sm', icon: 'refresh-cw', onClick: () => refresh() }) }) : null,
      report && !running && listState === 'ready' && !saved
        ? Callout({ tone: 'info', title: 'Ce rapport n’est pas dans la liste des rapports enregistrés', text: 'Il reste affiché et exportable en JSON ; les exports Markdown et CSV sont produits par le serveur à partir du fichier enregistré.' })
        : null,
    );
  }

  /* --- Carte ------------------------------------------------------------------------------ */
  const card = Card(
    {
      title: 'Rapport affiché',
      subtitle: '',
      icon: 'scroll-text',
      actions: [h('span.bench-report__label', 'Exporter'), ButtonGroup(...exportButtons)],
      footer: [
        h('span.bench-report__saved', 'Les rapports sont enregistrés dans le dossier ', h('code.mono', 'reports/'), ' du projet à la fin de chaque mesure.'),
        h('span.bench-report__picker', h('span.bench-report__label', 'Rapports enregistrés'), picker, retry),
      ],
    },
    environment,
    pending,
    notice,
  );
  const el = Section({ id: 'bench-report', title: 'Environnement et rapport', description: 'Sans la machine et les versions des bibliothèques, une mesure ne se compare à rien.' }, card);

  function drawEnvironment() {
    const env = report?.environment;
    const config = report?.config;
    environment.hidden = !env;
    pending.hidden = Boolean(env);
    card.setSubtitle(report?.id ? `${report.id} · ${fmtDate(report.created_at)}` : 'Mesure en cours : le rapport sera enregistré à la fin');
    if (!env) return;
    const suites = SUITES.filter((suite) => config?.suites?.includes(suite.id)).map((suite) => suite.label);
    environment.set([
      { label: 'Date de la mesure', value: fmtDate(env.timestamp ?? report.created_at) },
      { label: 'Python', value: env.python ?? '—', mono: true },
      { label: 'Plateforme', value: env.platform ?? '—', mono: true },
      { label: 'Processeur', value: env.processor || 'inconnu' },
      { label: 'Cœurs logiques', value: Number.isFinite(env.cpu_count) ? fmtNumber(env.cpu_count) : '—', mono: true },
      { label: 'Bibliothèques', value: `grpcio ${env.grpcio ?? '?'} · protobuf ${env.protobuf ?? '?'}`, mono: true },
      { label: 'Suites mesurées', value: suites.length ? suites.join(' · ') : '—' },
      { label: 'Procédure', value: config?.method ?? '—', mono: true },
      { label: 'Identifiant', value: report.id ?? '—', mono: true, copy: true },
    ]);
  }

  return {
    el,
    refresh,
    update(view, run) {
      const next = run.running ? null : view;
      if (next === report && run.running === running) return;
      report = next;
      running = run.running;
      drawEnvironment();
      sync();
    },
    setOffline(on) {
      if (on === offline) return;
      offline = on;
      if (!on && listState !== 'ready') refresh();
      else sync();
    },
  };
}
