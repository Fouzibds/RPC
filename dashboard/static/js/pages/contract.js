/**
 * Contrat & IDL — couplage fort et évolution du contrat : que se passe-t-il quand la signature
 * du serveur change et que le stub généré du client ne change pas ? La page montre le diff des
 * deux `.proto`, rejoue chaque rupture sur le serveur « contrat v2 » et classe ce qui en résulte :
 * rejet, plantage, corruption silencieuse ou compatibilité.
 *
 * Données : `GET /api/contract` (contrats, changements, règles, scénarios, derniers résultats)
 * et `POST /api/contract/run` (un scénario ou tous).
 */

import { h, clear } from '../core/dom.js';
import { registerShortcut } from '../core/shortcuts.js';
import { Badge, Button, Callout, EmptyState, PageHeader, Skeleton } from '../components/ui.js';
import { diffSection } from './contract/diff.js';
import { introSection, rulesSection } from './contract/facts.js';
import { labSection } from './contract/lab.js';
import { outcomeCatalog, plural } from './contract/model.js';

const TITLE = 'Contrat & IDL';

function skeleton() {
  const block = (height) => Skeleton({ variant: 'block', height });
  return h(
    'div.contract-skeleton',
    { 'aria-busy': 'true', 'aria-label': 'Chargement des contrats' },
    h('div.contract-facts', block(188), block(188), block(188)),
    h('div.contract-diff', block(420), block(420)),
    block(168),
    h('div.contract-skeleton__rows', block(92), block(92), block(92), block(92)),
  );
}

export default {
  id: 'contract',
  title: TITLE,
  subtitle: 'Faire évoluer un contrat sans casser ses clients',
  icon: 'file-code',
  /**
   * @param {HTMLElement} container Conteneur vide fourni par la coquille.
   * @param {{store: Object, api: Object, toast: Object, onCleanup: Function}} ctx
   * @returns {() => void} Nettoyage appelé quand on quitte la page.
   */
  mount(container, ctx) {
    const { api, store, toast } = ctx;
    const controller = new AbortController();
    const body = h('div.contract');
    const meta = h('div.contract-meta');
    let sections = [];
    let lab = null;
    let status = 'loading';

    const runAll = Button({ label: 'Tout exécuter', variant: 'primary', icon: 'play', kbd: 'mod+enter', disabled: true, onClick: () => lab?.runAll() });
    const header = PageHeader({
      eyebrow: 'Laboratoire',
      title: TITLE,
      icon: 'file-code',
      description:
        'Client et serveur sont liés par un contrat. Quand le serveur le fait évoluer seul, le stub du client, lui, ne change pas : certaines ruptures déclenchent une erreur, d’autres ne se voient même pas.',
      meta,
      actions: runAll,
    });

    function release() {
      sections.forEach((section) => section.destroy?.());
      sections = [];
      lab = null;
    }

    function render(overview) {
      release();
      const outcomes = outcomeCatalog(overview.outcomes);
      const stats = overview.stats ?? {};
      const diff = diffSection({ overview, onScenario: (id) => lab?.focusScenario(id) });
      lab = labSection({
        overview,
        outcomes,
        api,
        toast,
        signal: controller.signal,
        onChange: (id) => diff.focusChange(id, { page: true }),
        onBusy: (busy) => runAll.setLoading(busy),
      });
      sections = [diff, lab];
      clear(
        meta,
        Badge({ label: plural(stats.breaking ?? 0, 'changement cassant', 'changements cassants'), tone: 'danger', dot: true }),
        Badge({ label: plural(stats.compatible ?? 0, 'compatible'), tone: 'success', dot: true }),
        Badge({ label: plural((overview.scenarios ?? []).length, 'scénario rejouable', 'scénarios rejouables'), tone: 'neutral', icon: 'flask-conical' }),
      );
      clear(body, introSection(overview, outcomes), diff.el, lab.el, rulesSection(overview.rules));
      runAll.setDisabled(false);
    }

    function fail(error) {
      release();
      clear(meta);
      runAll.setDisabled(true);
      const retry = Button({ label: 'Réessayer', icon: 'refresh-cw', onClick: () => load() });
      if (error.offline) {
        status = 'offline';
        clear(
          body,
          EmptyState({
            icon: 'unplug',
            size: 'lg',
            tone: 'warning',
            title: 'Laboratoire injoignable',
            text: 'Les contrats et les scénarios viennent du laboratoire. La page se rechargera d’elle-même dès qu’il répondra — lancez-le avec « python main.py --dashboard ».',
            action: retry,
          }),
        );
        return;
      }
      status = 'error';
      clear(body, Callout({ tone: 'danger', title: 'Les contrats n’ont pas pu être chargés', text: error.message, actions: retry }));
    }

    async function load() {
      status = 'loading';
      clear(body, skeleton());
      try {
        const overview = await api.get('/api/contract', { signal: controller.signal });
        if (controller.signal.aborted) return;
        render(overview);
        status = 'ready';
      } catch (error) {
        if (controller.signal.aborted) return;
        fail(error);
      }
    }

    const offStore = store.subscribe('api', (value) => {
      if (value === 'online' && status === 'offline') load();
    });
    const offShortcut = registerShortcut({
      keys: 'mod+enter',
      description: 'Exécuter tous les scénarios de contrat',
      group: 'Contrat & IDL',
      run: () => lab?.runAll(),
    });

    container.append(header, body);
    load();

    return () => {
      controller.abort();
      offStore();
      offShortcut();
      release();
    };
  },
};
