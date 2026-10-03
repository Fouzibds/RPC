/**
 * Vue d'ensemble — colonne de droite : débit par protocole (courbe alimentée chaque seconde par
 * le message `stats`) et état de l'inventaire partagé par les trois serveurs.
 */

import { h } from '../../core/dom.js';
import { fmtNumber, fmtRate, fmtTime } from '../../core/format.js';
import { PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { href } from '../../core/router.js';
import { LineChart } from '../../components/charts.js';
import { Button, Card, CountUp } from '../../components/ui.js';
import { sumRates } from './model.js';

const WINDOW = 60;

const WAITING = { icon: 'activity', title: 'En attente de la première mesure', text: 'Le laboratoire publie le débit de chaque protocole une fois par seconde.' };
const OFFLINE = { icon: 'unplug', title: 'Laboratoire hors ligne', text: 'La courbe reprendra dès que le flux temps réel sera rétabli.' };

/**
 * Carte « Débit par protocole ».
 * @returns {HTMLElement & {tick: (stats: Object) => void, setOffline: (offline: boolean) => void, destroy: () => void}}
 *   `tick` ajoute le point de la dernière seconde (appels terminés par seconde, par protocole).
 */
export function createThroughput() {
  let points = 0;
  const chart = LineChart({
    ariaLabel: 'Appels terminés par seconde, par protocole, sur les 60 dernières secondes',
    area: true,
    curve: 'monotone',
    points: false,
    height: 188,
    maxPoints: WINDOW,
    slideMs: 1000,
    xFormat: (value) => fmtTime(value),
    yFormat: (value) => fmtRate(value, 'appels/s'),
    yTickFormat: (value) => fmtNumber(value, { compact: true }),
    yLabel: 'appels/s',
    loading: true,
    empty: WAITING,
    series: PROTOCOL_IDS.map((id) => ({ id, label: protocol(id).short, protocol: id, points: [] })),
  });
  const el = Card({ title: 'Débit par protocole', subtitle: 'Appels terminés par seconde, fenêtre glissante de 60\u202Fs', icon: 'chart-line', class: 'ov-throughput' }, chart);

  el.tick = (message) => {
    const at = Number(message?.ts) * 1000;
    if (!Number.isFinite(at)) return;
    if (points === 0) chart.update({ loading: false });
    points += 1;
    chart.push(at, sumRates(message).byProtocol);
  };
  el.setOffline = (offline) => {
    if (points === 0) chart.update({ loading: false, empty: offline ? OFFLINE : WAITING });
  };
  el.destroy = () => chart.destroy();
  return el;
}

const euros = (value) => `${fmtNumber(value, { decimals: 0 })} €`;

const TILES = [
  { key: 'total_units', label: 'Unités en stock', hint: (inventory) => `sur ${fmtNumber(inventory.products)} références` },
  { key: 'inventory_value', label: 'Valeur du stock', format: euros, hint: () => 'stock × prix unitaire' },
  { key: 'low_stock_count', label: 'Stocks bas', hint: () => 'sous le seuil d’alerte', alert: true },
  { key: 'operations', label: 'Écritures', hint: () => 'appliquées au stock' },
  { key: 'deduplicated', label: 'Dédupliqués', hint: () => 'rejeux sans effet' },
];

/**
 * Carte « Inventaire » : l'état métier unique que les trois middlewares exposent.
 * @returns {HTMLElement & {update: (inventory: Object|null) => void}}
 */
export function createInventory() {
  const tiles = TILES.map((tile) => {
    const counter = CountUp({ value: 0, format: tile.format ?? ((value) => fmtNumber(Math.round(value))) });
    const hint = h('span.ov-tile__hint');
    const node = h('div.ov-tile', h('span.ov-tile__label.t-label', tile.label), h('span.ov-tile__value', counter), hint);
    return { ...tile, counter, hintEl: hint, node };
  });
  const grid = h('div.ov-tiles', tiles.map((tile) => tile.node));
  const el = Card(
    {
      title: 'Inventaire',
      subtitle: 'Un seul objet métier derrière les trois serveurs',
      icon: 'boxes',
      class: 'ov-inventory',
      footer: [
        h('span', 'Écrit par gRPC, lu par REST : c’est le même objet.'),
        Button({ label: 'Essayer', variant: 'ghost', size: 'sm', iconRight: 'arrow-right', href: href('console', { protocol: 'grpc', method: 'update_stock' }) }),
      ],
    },
    grid,
  );
  let seen = false;

  el.update = (inventory) => {
    grid.toggleAttribute('data-empty', !inventory);
    for (const tile of tiles) {
      const value = Number(inventory?.[tile.key]);
      if (!inventory || !Number.isFinite(value)) {
        tile.counter.textContent = '—';
        tile.hintEl.textContent = '';
        continue;
      }
      tile.counter.set(value, { animate: seen });
      tile.hintEl.textContent = tile.hint(inventory);
      tile.node.toggleAttribute('data-alert', Boolean(tile.alert) && value > 0);
    }
    seen = seen || Boolean(inventory);
  };
  el.update(null);
  return el;
}
