/**
 * Banc d'essai — tuile de latence d'un protocole (débit, moyenne, surcoût, stabilité).
 */

import { h, clear } from '../../core/dom.js';
import { fmtNumber, fmtRate } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';
import { Sparkline } from '../../components/charts.js';
import { Card, Col, Skeleton, Stat } from '../../components/ui.js';
import { fmtDur, fmtFactor, protocolLabel } from './model.js';

/**
 * Tuile d'un protocole : débit, temps moyen, surcoût et série des échantillons dans l'ordre.
 * @param {(chart: HTMLElement) => HTMLElement} track Enregistre le `destroy` de la mini-courbe.
 * @returns {{el: HTMLElement, card: HTMLElement, set: (result: Object|null, measured: boolean) => void, dispose: () => void}}
 *   `set(null)` affiche le squelette d'une mesure attendue ; `dispose` arrête le suivi de largeur.
 */
export function ProtocolTile(track) {
  const stat = Stat({ label: '', value: null, format: (value) => fmtRate(value, 'appels/s') });
  const spark = track(Sparkline({ values: [], height: 40, width: 240, dot: false, format: fmtDur, ariaLabel: 'Durée de chaque appel, dans l’ordre de la mesure' }));
  const sparkBox = h('div.bench-tile__spark', spark);
  const caption = h('p.bench-tile__caption');
  const card = Card({ padding: 'md', class: 'bench-tile' }, stat, sparkBox, caption);
  const skeleton = h('div.bench-tile__loading', Skeleton({ width: '45%' }), Skeleton({ variant: 'block', height: 30, width: '70%' }), Skeleton({ variant: 'block', height: 40 }));
  card.body.append(skeleton);

  // La mini-courbe a une largeur fixe : on la cale sur celle de la tuile.
  const observer = new ResizeObserver(([entry]) => {
    const width = Math.floor(entry.contentRect.width);
    if (width > 0) spark.update({ width });
  });
  observer.observe(sparkBox);
  const dispose = () => observer.disconnect();

  function set(result, measured) {
    card.hidden = false;
    const ready = Boolean(result);
    skeleton.hidden = ready;
    stat.hidden = sparkBox.hidden = caption.hidden = !ready;
    if (!ready) return;
    const info = protocol(result.protocol);
    card.style.setProperty('--edge', info.color);
    card.classList.add('card--edge');
    card.dataset.protocol = result.protocol;
    const overhead = Number.isFinite(result.overhead_vs_local_x) && result.protocol !== 'local' ? ` · ${fmtFactor(result.overhead_vs_local_x)} l’appel local` : '';
    stat.update({
      label: protocolLabel(result),
      value: measured ? result.rps : '—',
      hint: measured ? `moyenne ${fmtDur(result.mean_ms)}${overhead}` : 'aucun appel réussi',
    });
    spark.update({ values: result.samples_ms ?? [], protocol: result.protocol });
    const samples = result.samples_ms?.length ?? 0;
    clear(
      caption,
      samples ? `${fmtNumber(samples)} appels dans l’ordre` : 'Aucun échantillon',
      measured ? [' · σ ', h('span.num', fmtDur(result.stdev_ms))] : null,
      result.errors ? h('span.bench-tile__errors', ` · ${fmtNumber(result.errors)} erreur${result.errors > 1 ? 's' : ''}`) : null,
    );
  }

  return { el: Col({ span: 3, md: 6, sm: 6 }, card), card, set, dispose };
}
