/**
 * Contrat & IDL — bilan des scénarios joués : la phrase calculée (ruptures détectées par une
 * erreur / passées inaperçues), l'anneau des issues et la matrice scénario × issue.
 */

import { h, svg, clear } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { protocol } from '../../core/protocols.js';
import { Button, Callout, Card, EmptyState, Skeleton } from '../../components/ui.js';
import { plural, summarize } from './model.js';

const RING_SIZE = 148;
const RING_THICKNESS = 14;
const RING_GAP = 3;

/** Anneau à plusieurs arcs : un arc par issue observée, proportionnel au nombre de scénarios. */
function ring(outcomes, counts, played) {
  const radius = (RING_SIZE - RING_THICKNESS) / 2;
  const circumference = 2 * Math.PI * radius;
  const parts = outcomes.list.filter((outcome) => (counts.get(outcome.id) ?? 0) > 0);
  const gap = parts.length > 1 ? RING_GAP : 0;
  let offset = 0;
  const arcs = parts.map((outcome, index) => {
    const length = (circumference * counts.get(outcome.id)) / played;
    const arc = svg('circle', {
      class: 'contract-ring__arc',
      cx: RING_SIZE / 2,
      cy: RING_SIZE / 2,
      r: radius,
      'stroke-width': RING_THICKNESS,
      'stroke-dasharray': `${Math.max(0, length - gap).toFixed(2)} ${circumference.toFixed(2)}`,
      'stroke-dashoffset': (-offset).toFixed(2),
      style: { '--c': `var(--${outcome.tone})`, '--i': index },
      dataset: { outcome: outcome.id },
    });
    offset += length;
    return arc;
  });
  const label = parts.map((outcome) => `${outcome.short} : ${counts.get(outcome.id)}`).join(', ');
  return h(
    'div.contract-ring',
    { role: 'img', 'aria-label': `Issues observées — ${label}`, style: { width: `${RING_SIZE}px`, height: `${RING_SIZE}px` } },
    svg(
      'svg',
      { width: RING_SIZE, height: RING_SIZE, viewBox: `0 0 ${RING_SIZE} ${RING_SIZE}`, 'aria-hidden': 'true' },
      svg('circle', { class: 'contract-ring__track', cx: RING_SIZE / 2, cy: RING_SIZE / 2, r: radius, 'stroke-width': RING_THICKNESS }),
      arcs,
    ),
    h('div.contract-ring__center', h('span.contract-ring__number.num', String(played)), h('span.contract-ring__label', played > 1 ? 'scénarios joués' : 'scénario joué')),
  );
}

function legend(outcomes, counts) {
  return h(
    'ul.contract-legend',
    outcomes.list.map((outcome) =>
      h(
        'li.contract-legend__item',
        { dataset: { tone: outcome.tone, zero: String(!(counts.get(outcome.id) > 0)) }, title: outcome.text },
        h('span.contract-legend__key', { 'aria-hidden': 'true' }),
        h('span.contract-legend__label', outcome.short),
        h('span.contract-legend__count.num', String(counts.get(outcome.id) ?? 0)),
      ),
    ),
  );
}

/** La phrase du bilan, entièrement calculée à partir des résultats affichés. */
function sentence(stats) {
  const figure = (count, tone) => h('strong.contract-sum__figure.num', { dataset: { tone: count ? tone : 'neutral' } }, String(count));
  const lines = [];
  if (stats.ruptures > 0) {
    const detail = [stats.rejected ? plural(stats.rejected, 'rejet') : null, stats.crashed ? plural(stats.crashed, 'plantage') : null].filter(Boolean).join(', ');
    lines.push(
      h(
        'p.contract-sum__headline',
        'Sur ',
        figure(stats.ruptures, 'neutral'),
        stats.ruptures > 1 ? ' ruptures de contrat rejouées, ' : ' rupture de contrat rejouée, ',
        figure(stats.detected, 'info'),
        stats.detected > 1 ? ' se signalent par une erreur' : ' se signale par une erreur',
        detail ? ` (${detail})` : '',
        ' et ',
        figure(stats.silent.length, 'danger'),
        stats.silent.length > 1 ? ' passent inaperçues.' : ' passe inaperçue.',
      ),
    );
  } else {
    lines.push(h('p.contract-sum__headline', 'Aucune rupture parmi les scénarios joués pour l’instant.'));
  }
  const notes = [];
  if (stats.silent.length) {
    const origins = [...new Set(stats.silent.map((result) => protocol(result.protocol).label))];
    notes.push(
      `${stats.silent.length > 1 ? 'Les corruptions silencieuses viennent' : 'La corruption silencieuse vient'} de ${origins.join(' et ')} : l’appel répond OK, les données sont fausses, et seul un contrôle extérieur le révélera.`,
    );
  }
  if (stats.compatible) notes.push(`${plural(stats.compatible, 'évolution compatible', 'évolutions compatibles')} : l’ancien client ne remarque rien.`);
  if (stats.unexpected) notes.push(`${plural(stats.unexpected, 'scénario')} ne se ${stats.unexpected > 1 ? 'terminent' : 'termine'} pas comme prévu.`);
  if (notes.length) lines.push(h('p.contract-sum__notes', notes.join(' ')));
  return lines;
}

function matrix(scenarios, results, outcomes, onPick) {
  const cell = (scenario, outcome) => {
    const result = results.get(scenario.id);
    const observed = result?.outcome === outcome.id;
    const expected = scenario.expected_outcome === outcome.id;
    let mark = h('span.contract-matrix__dot', { 'aria-hidden': 'true' });
    let text = '';
    if (observed) {
      mark = h('span.contract-matrix__hit', { 'aria-hidden': 'true' }, icon(outcome.icon, { size: 12, stroke: 2.25 }));
      text = `Observé : ${outcome.label}`;
    } else if (expected) {
      mark = h('span.contract-matrix__ring', { 'aria-hidden': 'true' });
      text = result ? `Attendu, non observé : ${outcome.label}` : `Attendu : ${outcome.label}`;
    }
    return h('td.contract-matrix__cell', { dataset: { tone: outcome.tone, state: observed ? 'hit' : expected ? 'expected' : 'none' }, title: text || null }, mark, text ? h('span.sr-only', text) : null);
  };
  return h(
    'table.contract-matrix',
    h('caption.sr-only', 'Issue de chaque scénario : observée (pastille pleine) ou attendue (anneau).'),
    h(
      'thead',
      h(
        'tr',
        h('th.contract-matrix__corner', { scope: 'col' }, 'Scénario'),
        outcomes.list.map((outcome) => h('th.contract-matrix__outcome', { scope: 'col', dataset: { tone: outcome.tone }, title: outcome.text }, outcome.short)),
      ),
    ),
    h(
      'tbody',
      scenarios.map((scenario) =>
        h(
          'tr',
          { dataset: { played: String(results.has(scenario.id)) } },
          h(
            'th.contract-matrix__name',
            { scope: 'row' },
            h(
              'button.contract-matrix__pick',
              { type: 'button', title: 'Aller à ce scénario', onClick: () => onPick(scenario.id) },
              h('span.contract-matrix__proto', { style: { '--c': protocol(scenario.protocol).color }, title: protocol(scenario.protocol).label }),
              h('span.truncate', scenario.title),
            ),
          ),
          outcomes.list.map((outcome) => cell(scenario, outcome)),
        ),
      ),
    ),
  );
}

/**
 * Carte « Bilan » de la section des scénarios.
 * @param {Object} options
 * @param {Array<Object>} options.scenarios `scenarios` de `GET /api/contract`.
 * @param {{list: Array<Object>, get: Function}} options.outcomes Catalogue des issues.
 * @param {() => void} options.onRunAll Lance tous les scénarios.
 * @param {(scenarioId: string) => void} options.onPick Amène un scénario à l'écran.
 * @returns {{el: HTMLElement, update: (results: Map<string, Object>) => void, setBusy: (busy: boolean) => void,
 *   setError: (error: Error|null) => void}}
 */
export function summaryCard({ scenarios, outcomes, onRunAll, onPick }) {
  const body = h('div.contract-sum', { 'aria-live': 'polite' });
  const card = Card({ title: 'Bilan des ruptures', subtitle: '', icon: 'chart-pie', class: 'contract-sum-card' }, body);
  let results = new Map();
  let busy = false;
  let failure = null;

  function render() {
    const stats = summarize(scenarios, results, outcomes);
    const played = stats.played.length;
    card.setSubtitle(played ? `${played} scénario${played > 1 ? 's' : ''} sur ${stats.total} joué${played > 1 ? 's' : ''} sur le serveur « contrat v2 »` : 'Rien n’a encore été joué');
    body.dataset.busy = String(busy);
    const error = failure
      ? Callout({
          tone: 'danger',
          title: failure.offline ? 'Laboratoire injoignable' : 'Les scénarios n’ont pas pu être joués',
          text: failure.message,
          actions: Button({ label: 'Réessayer', size: 'sm', icon: 'refresh-cw', onClick: onRunAll }),
        })
      : null;
    if (!played) {
      clear(
        body,
        error,
        busy
          ? h('div.contract-sum__loading', Skeleton({ variant: 'circle', width: RING_SIZE, height: RING_SIZE }), h('div', Skeleton({ lines: 3 }), Skeleton({ variant: 'block', height: 132 })))
          : EmptyState({
              icon: 'flask-conical',
              size: 'sm',
              title: 'Aucun scénario joué',
              text: `Rejouez les ${stats.total} ruptures face au serveur « contrat v2 » : le bilan compte celles qu’une erreur signale et celles qui passent inaperçues.`,
              action: Button({ label: 'Tout exécuter', variant: 'primary', icon: 'play', kbd: 'mod+enter', onClick: onRunAll }),
            }),
      );
      return;
    }
    clear(
      body,
      error,
      h('div.contract-sum__lead', sentence(stats)),
      h(
        'div.contract-sum__viz',
        h('div.contract-sum__donut', ring(outcomes, stats.counts, played), legend(outcomes, stats.counts)),
        h('div.contract-sum__matrix.scroll-x', matrix(scenarios, results, outcomes, onPick)),
      ),
    );
  }

  render();
  return {
    el: card,
    update: (next) => {
      results = next;
      render();
    },
    setBusy: (next) => {
      busy = next;
      if (next) failure = null;
      render();
    },
    setError: (error) => {
      failure = error;
      render();
    },
  };
}
