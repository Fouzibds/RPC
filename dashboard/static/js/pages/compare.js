/**
 * Transparence (#/compare) — la transparence de localisation, premier avantage du RPC : la même
 * opération métier écrite de quatre façons, ce que chacune laisse à la charge du développeur,
 * la preuve par l'exécution qu'elles sont équivalentes, puis le revers : le code a l'air local,
 * l'appel ne l'est pas.
 *
 * Données : `GET /api/code-compare` (sources réelles), `POST /api/code-compare/run`,
 * `POST /api/call`, `PUT /api/network`. Les sections vivent dans `./compare/*.js`.
 */

import { h, clear, on } from '../core/dom.js';
import { hasBlockingLayer } from '../core/layers.js';
import { createStore } from '../core/store.js';
import { Badge, Button, Callout, EmptyState, PageHeader, Skeleton } from '../components/ui.js';
import { effortSection } from './compare/effort.js';
import { operationStrip } from './compare/hero.js';
import { illusionSection } from './compare/illusion.js';
import { concernMatrix, plural } from './compare/model.js';
import { readProduct } from './compare/ops.js';
import { proofSection } from './compare/proof.js';
import { snippetsSection } from './compare/snippets.js';

function loadingView() {
  const card = (height) => h('div.cmp-skel__card', Skeleton({ variant: 'text', width: '42%' }), Skeleton({ variant: 'block', height }), Skeleton({ variant: 'text', lines: 2 }));
  return h(
    'div.cmp-skel',
    { 'aria-busy': 'true', 'aria-label': 'Chargement de la comparaison' },
    Skeleton({ variant: 'block', height: 92 }),
    h('div.cmp-variants', card(120), card(120), card(160), card(160)),
  );
}

export default {
  id: 'compare',
  title: 'Transparence',
  subtitle: 'Le même appel écrit de quatre façons',
  icon: 'git-compare',
  /**
   * @param {HTMLElement} container Conteneur vide fourni par la coquille.
   * @param {{store: Object, api: Object, ws: Object, navigate: Function, toast: Object, query: Object, onCleanup: Function}} ctx
   * @returns {() => void} Nettoyage appelé quand on quitte la page.
   */
  mount(container, ctx) {
    const { api, store, toast } = ctx;
    const body = h('div.cmp-body');
    const product = createStore({ stock: null, name: '' });
    const loader = new AbortController();
    let sectionCleanups = [];
    let sections = { proof: null, illusion: null };
    let state = 'loading';
    let alive = true;

    const meta = h('div.cmp-meta');
    const runAll = Button({ label: 'Exécuter', variant: 'primary', icon: 'play', kbd: 'mod+enter', disabled: true, onClick: () => runProof() });

    /** Action principale de la page : la preuve par l'exécution, amenée à l'écran puis lancée. */
    function runProof() {
      if (!sections.proof) return;
      const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      sections.proof.scrollIntoView({ behavior: still ? 'auto' : 'smooth', block: 'start' });
      sections.proof.run();
    }

    function syncHeader() {
      runAll.setDisabled(!sections.proof || store.get().api === 'offline');
    }

    function disposeSections() {
      sectionCleanups.forEach((cleanup) => cleanup());
      sectionCleanups = [];
      sections = { proof: null, illusion: null };
      clear(meta);
      syncHeader();
    }

    function failureView(error) {
      if (error.offline) {
        return EmptyState({
          icon: 'wifi-off',
          tone: 'warning',
          size: 'lg',
          title: 'Laboratoire injoignable',
          text: 'Le serveur du tableau de bord ne répond pas. La comparaison se rechargera d’elle-même dès qu’il sera de retour.',
          action: Button({ label: 'Réessayer', icon: 'refresh-cw', onClick: () => load() }),
        });
      }
      return Callout({
        tone: 'danger',
        title: 'La comparaison n’a pas pu être chargée',
        text: error.message,
        actions: Button({ label: 'Réessayer', icon: 'refresh-cw', size: 'sm', onClick: () => load() }),
      });
    }

    function render(data) {
      if (!data?.snippets?.length) {
        state = 'empty';
        clear(
          body,
          EmptyState({
            icon: 'git-compare',
            title: 'Aucune écriture à comparer',
            text: 'Le laboratoire n’a renvoyé aucun extrait de code.',
            action: Button({ label: 'Recharger', icon: 'rotate-ccw', onClick: () => load() }),
          }),
        );
        return;
      }
      state = 'ready';
      const refreshProduct = async () => {
        try {
          const next = await readProduct(api, data.product_id);
          if (alive) product.set(next);
        } catch {
          /* Hors ligne : le bandeau garde la dernière valeur connue. */
        }
      };
      const env = { api, store, toast, product, refreshProduct, alive: () => alive, track: (cleanup) => sectionCleanups.push(cleanup) };
      sections.proof = proofSection(data, env);
      sections.illusion = illusionSection(data, env);
      runAll.setLabel(sections.proof.runLabel);
      syncHeader();
      clear(
        meta,
        Badge({ label: plural(data.snippets.length, 'écriture Python', 'écritures Python'), icon: 'code-xml' }),
        data.javascript_fetch ? Badge({ label: '1 variante navigateur', icon: 'globe' }) : null,
        Badge({ label: plural(concernMatrix(data.snippets).length, 'préoccupation de protocole', 'préoccupations de protocole'), icon: 'list' }),
      );
      clear(
        body,
        operationStrip(data, env),
        snippetsSection(data, env),
        effortSection(data, env),
        sections.proof,
        sections.illusion,
        data.takeaway ? Callout({ tone: 'accent', icon: 'lightbulb', title: 'À retenir', text: data.takeaway }) : null,
      );
      refreshProduct();
    }

    async function load() {
      disposeSections();
      state = 'loading';
      clear(body, loadingView());
      try {
        const data = await api.get('/api/code-compare', { signal: loader.signal });
        if (alive) render(data);
      } catch (error) {
        if (!alive) return;
        state = error.offline ? 'offline' : 'error';
        clear(body, failureView(error));
      }
    }

    container.append(
      PageHeader({
        eyebrow: 'Synthèse',
        title: 'Transparence',
        icon: 'git-compare',
        description:
          'La promesse du RPC : appeler une procédure distante comme une fonction locale. Voici la même opération métier écrite quatre fois, côte à côte — et ce que chaque écriture laisse à la charge de celui qui appelle.',
        meta,
        actions: runAll,
      }),
      body,
    );

    const stopApi = store.subscribe('api', (value) => {
      syncHeader();
      if (value === 'online' && state === 'offline') load();
    });
    const stopKeys = on(document, 'keydown', (event) => {
      if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey) || event.repeat || hasBlockingLayer()) return;
      if (!sections.proof) return;
      event.preventDefault();
      if (sections.illusion?.card.contains(document.activeElement)) sections.illusion.run();
      else runProof();
    });

    load();

    return () => {
      alive = false;
      loader.abort();
      stopApi();
      stopKeys();
      disposeSections();
    };
  },
};
