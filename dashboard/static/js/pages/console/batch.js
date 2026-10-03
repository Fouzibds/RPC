/**
 * Console RPC — résultat d'un lot asynchrone : temps réel contre somme des durées individuelles,
 * accélération obtenue, et une barre par appel pour VOIR que les appels se chevauchent.
 */

import { h } from '../../core/dom.js';
import { fmtMs, fmtNumber } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';
import { chartTooltip, tipContent } from '../../components/charts.js';
import { Callout, Stat } from '../../components/ui.js';
import { Banner, Insight, errorBadges, strong } from './parts.js';

const fmtFactor = (value) => `${fmtNumber(value, { decimals: value >= 10 ? 1 : 2 })} ×`;

function median(values) {
  if (!values.length) return NaN;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

/** Phrase tirée des trois nombres mesurés : chevauchement, succession ou surcoût. */
function verdict(batch) {
  const { count, wall_ms: wall, sum_ms: sum, speedup } = batch;
  const calls = `${fmtNumber(count)} appels`;
  if (!Number.isFinite(speedup)) return [`Les ${calls} ont été lancés, mais le temps réel est trop court pour être comparé.`];
  if (speedup >= 1.15) {
    return ['Mis bout à bout, les ', strong(calls), ' auraient pris ', strong(fmtMs(sum)), ' ; lancés ensemble, ils se terminent en ', strong(fmtMs(wall)), ' : ils se sont chevauchés, soit ', strong(fmtFactor(speedup)), ' plus vite qu’en file indienne.'];
  }
  if (speedup > 0.87) {
    return ['Temps réel ', strong(fmtMs(wall)), ' pour ', strong(fmtMs(sum)), ' de durées individuelles : les ', strong(calls), ' se sont pratiquement succédé, sans gain à les lancer ensemble.'];
  }
  return ['Le lot a pris ', strong(fmtMs(wall)), ' alors que les ', strong(calls), ' ne totalisent que ', strong(fmtMs(sum)), ' : ici, organiser le lot coûte plus cher que les appels eux-mêmes (', strong(fmtFactor(speedup)), ').'];
}

/** Deux pistes à la même échelle : le temps réel, et les durées individuelles mises bout à bout. */
function overlap(batch, color) {
  const scale = Math.max(batch.wall_ms, batch.sum_ms, 1e-9);
  const calls = batch.calls ?? [];
  return h(
    'div.console-overlap',
    { style: { '--c': color } },
    h(
      'div.console-overlap__row',
      h('span.console-overlap__label', 'Temps réel du lot'),
      h('div.console-overlap__track', h('span.console-overlap__bar.console-overlap__bar--wall', { style: { width: `${Math.max(0.6, (batch.wall_ms / scale) * 100)}%` } })),
      h('span.console-overlap__value.num', fmtMs(batch.wall_ms)),
    ),
    h(
      'div.console-overlap__row',
      h('span.console-overlap__label', 'Durées mises bout à bout'),
      h(
        'div.console-overlap__track',
        h(
          'span.console-overlap__chain',
          { style: { width: `${Math.max(0.6, (batch.sum_ms / scale) * 100)}%` } },
          calls.map((call) => h('span.console-overlap__link', { dataset: { ok: String(call.ok) }, style: { flexGrow: Math.max(call.duration_ms, 1e-6) } })),
        ),
      ),
      h('span.console-overlap__value.num', fmtMs(batch.sum_ms)),
    ),
    h('p.console-overlap__caption', 'Même échelle de temps pour les deux pistes. La seconde est une reconstruction : les durées mesurées de chaque appel, placées l’une après l’autre.'),
  );
}

/** Une colonne par appel : hauteur = durée mesurée, couleur = issue. */
function bars(batch, color) {
  const calls = batch.calls ?? [];
  const durations = calls.map((call) => call.duration_ms);
  const top = Math.max(...durations, batch.wall_ms, 1e-9);
  const el = h('div.console-bars', { style: { '--c': color, '--n': calls.length }, role: 'img', 'aria-label': `Durée de chacun des ${calls.length} appels` });
  const wallLine = h('span.console-bars__wall', { style: { bottom: `${(batch.wall_ms / top) * 100}%` } }, h('span.console-bars__wall-label.num', `temps réel ${fmtMs(batch.wall_ms)}`));
  const columns = calls.map((call, index) =>
    h('span.console-bars__col', {
      dataset: { ok: String(call.ok) },
      style: { height: `${Math.max(2, (call.duration_ms / top) * 100)}%`, '--i': index },
      onPointermove: (event) =>
        chartTooltip.show({
          owner: el,
          key: index,
          x: event.clientX,
          y: event.clientY,
          content: () =>
            tipContent({
              title: `Appel ${index + 1} sur ${calls.length}`,
              subtitle: call.call_id || undefined,
              rows: [
                { label: 'Durée', value: fmtMs(call.duration_ms), color: call.ok ? color : 'var(--danger)', shape: 'rect' },
                { label: 'Issue', value: call.ok ? 'succès' : (call.error?.code ?? 'erreur') },
                ...(call.attempts ? [{ label: 'Tentatives', value: String(call.attempts) }] : []),
              ],
              note: call.error?.message,
            }),
        }),
      onPointerleave: () => chartTooltip.hide(el),
    }),
  );
  el.append(h('div.console-bars__plot', columns, wallLine));
  return h(
    'div.console-bars-wrap',
    h(
      'div.console-bars__head',
      h('span.t-label', 'Durée de chaque appel'),
      h('span.console-bars__legend', h('span.console-bars__key', { dataset: { ok: 'true' }, style: { '--c': color } }), 'succès', h('span.console-bars__key', { dataset: { ok: 'false' } }), 'erreur'),
    ),
    el,
    h(
      'dl.console-bars__facts',
      h('div', h('dt', 'le plus court'), h('dd.num', fmtMs(Math.min(...durations)))),
      h('div', h('dt', 'médiane'), h('dd.num', fmtMs(median(durations)))),
      h('div', h('dt', 'le plus long'), h('dd.num', fmtMs(Math.max(...durations)))),
      h('div', h('dt', 'erreurs'), h('dd.num', `${fmtNumber(batch.errors ?? 0)} / ${fmtNumber(calls.length)}`)),
    ),
  );
}

/**
 * Vue du résultat d'un lot d'appels lancés ensemble.
 * @param {Object} props
 * @param {Object} props.response Réponse `async` de `POST /api/call`.
 * @returns {HTMLElement}
 */
export function BatchResult({ response }) {
  const info = protocol(response.protocol);
  const errors = response.errors ?? 0;
  const firstError = (response.calls ?? []).find((call) => !call.ok)?.error;
  const tone = errors === 0 ? 'success' : errors === response.count ? 'danger' : 'warning';
  return h(
    'div.console-result',
    Banner({
      tone,
      title: errors === 0 ? `${fmtNumber(response.count)} appels réussis` : `${fmtNumber(errors)} appel${errors > 1 ? 's' : ''} en échec sur ${fmtNumber(response.count)}`,
      text: errors === 0 ? [h('span.mono', response.method), ` lancé ${fmtNumber(response.count)} fois de front par ${info.label}${response.via_proxy ? ', à travers le proxy de chaos' : ''}.`] : firstError?.message,
      badges: firstError ? errorBadges(firstError) : null,
    }),
    h(
      'div.console-stats.console-stats--big',
      Stat({ label: 'Temps réel', icon: 'clock', value: response.wall_ms, format: fmtMs, size: 'lg', hint: 'du premier départ à la dernière réponse' }),
      Stat({ label: 'Somme des durées', icon: 'list', value: response.sum_ms, format: fmtMs, size: 'lg', hint: `${fmtNumber(response.count)} durées individuelles` }),
      Stat({ label: 'Accélération', icon: 'zap', value: response.speedup ?? '—', format: (v) => fmtFactor(v), size: 'lg', hint: 'somme ÷ temps réel', protocol: response.protocol }),
    ),
    Insight(verdict(response)),
    overlap(response, info.color),
    bars(response, info.color),
    response.strategy ? Callout({ tone: 'neutral', icon: info.icon, title: `Comment ${info.label} mène plusieurs appels de front`, text: response.strategy }) : null,
  );
}
