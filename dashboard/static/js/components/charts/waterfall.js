/**
 * Waterfall : les étapes d'un appel distant dans l'ordre, chacune avec son décalage et sa durée,
 * colorée selon son couloir (client, réseau, serveur). Une tête de lecture facultative permet de
 * « rejouer » l'appel dans la page « Sous le capot ».
 */

import { h, clear } from '../../core/dom.js';
import { fmtPercent, fmtUs } from '../../core/format.js';
import { LANE_COLORS, Legend, chartTooltip, createChart, measureText, ticks as niceTicks, tidyTickLabels, tipContent } from './core.js';

/** Couloirs par défaut d'un appel distant. */
const DEFAULT_LANES = Object.freeze([
  { id: 'client', label: 'Client', color: LANE_COLORS.client },
  { id: 'network', label: 'Réseau', color: LANE_COLORS.network },
  { id: 'server', label: 'Serveur', color: LANE_COLORS.server },
]);

/**
 * @typedef {Object} WaterfallStage
 * @property {string} id
 * @property {string} label Nom de l'étape (`client.marshal`, « Sérialisation »…).
 * @property {string} lane Identifiant du couloir : `client`, `network` ou `server` par défaut.
 * @property {number} start Décalage depuis le début de l'appel, dans l'unité de `format` (µs par défaut).
 * @property {number} [duration=0] Durée ; à 0, l'étape est un jalon (losange).
 * @property {string} [detail] Texte de l'info-bulle (taille du message, cible du dispatch…).
 */

/**
 * Cascade des étapes d'un appel : une ligne par étape, barre positionnée sur un axe du temps
 * commun, durée à droite. `playhead` grise les étapes à venir et met en avant l'étape en cours.
 *
 *     const waterfall = Waterfall({ stages, onSelect: (stage) => showBytes(stage.id) });
 *     waterfall.setPlayhead(120);   // µs depuis le début de l'appel ; null pour retirer la tête de lecture
 *
 * @param {Object} [props]
 * @param {WaterfallStage[]} [props.stages]
 * @param {Array<{id: string, label: string, color: string}>} [props.lanes] Couloirs (client / réseau / serveur par défaut).
 * @param {(value: number) => string} [props.format=fmtUs]
 * @param {number} [props.total] Durée totale représentée (par défaut la fin de la dernière étape).
 * @param {number|null} [props.playhead] Position de la tête de lecture.
 * @param {string|null} [props.selected] Identifiant de l'étape sélectionnée.
 * @param {(stage: WaterfallStage) => void} [props.onSelect] Rend les lignes cliquables.
 * @param {boolean} [props.legend=true] Légende des couloirs.
 * @param {string} [props.ariaLabel]
 * @param {{icon?: string, title?: string, text?: string}} [props.empty]
 * @param {boolean} [props.loading]
 * @returns {HTMLElement & {update: (patch: Object) => void, setPlayhead: (position: number|null) => void, select: (id: string|null) => void, setLoading: (on: boolean) => void, destroy: () => void}}
 */
export function Waterfall(props = {}) {
  let rows = [];
  let head = null;
  let total = 1;

  const stagesOf = (state) => (state.stages ?? []).filter((stage) => Number.isFinite(Number(stage.start)));
  const lanesOf = (state) => state.lanes ?? DEFAULT_LANES;

  function applyPlayhead(state) {
    const position = Number.isFinite(state.playhead) ? state.playhead : null;
    if (head) {
      head.hidden = position === null;
      if (position !== null) head.style.left = `${Math.min(100, Math.max(0, (position / total) * 100))}%`;
    }
    for (const { node, stage } of rows) {
      const end = stage.start + (stage.duration ?? 0);
      node.dataset.state = position === null ? 'idle' : position >= end ? 'done' : position >= stage.start ? 'active' : 'pending';
    }
  }

  function applySelection(state) {
    for (const { node, stage } of rows) node.setAttribute('aria-current', String(stage.id === state.selected));
  }

  function draw({ plot, width, state }) {
    const stages = stagesOf(state);
    const lanes = new Map(lanesOf(state).map((lane) => [lane.id, lane]));
    total = Number(state.total) || Math.max(1, ...stages.map((stage) => stage.start + (stage.duration ?? 0)));
    const labelWidth = Math.min(Math.round(width * 0.36), Math.ceil(Math.max(...stages.map((stage) => measureText(stage.label, { size: 12, mono: true })))) + 22);
    const durationWidth = Math.ceil(Math.max(...stages.map((stage) => measureText(state.format(stage.duration ?? 0), { mono: true })))) + 4;
    const trackWidth = width - labelWidth - durationWidth - 24;
    const marks = niceTicks(0, total, Math.max(2, Math.round(trackWidth / 110)));
    const markLabels = tidyTickLabels(marks.map((tick) => state.format(tick)));

    head = h('div.waterfall__playhead', { hidden: true });
    const overlay = h(
      'div.waterfall__overlay',
      marks.map((tick) => h('span.waterfall__gridline', { style: { left: `${(tick / total) * 100}%` } })),
      head,
    );
    const axis = h(
      'div.waterfall__row.waterfall__row--head',
      h('span.t-label', 'Étape'),
      h(
        'div.waterfall__axis',
        marks.map((tick, index) =>
          h('span.waterfall__tick.num', { class: { 'waterfall__tick--first': index === 0, 'waterfall__tick--last': tick / total > 0.94 }, style: { left: `${(tick / total) * 100}%` } }, markLabels[index]),
        ),
      ),
      h('span.t-label.waterfall__duration', 'Durée'),
    );

    rows = stages.map((stage, index) => {
      const lane = lanes.get(stage.lane) ?? { id: stage.lane, label: stage.lane, color: 'var(--neutral)' };
      const duration = stage.duration ?? 0;
      const interactive = Boolean(state.onSelect);
      const bar = duration > 0
        ? h('span.waterfall__bar', { style: { left: `${(stage.start / total) * 100}%`, width: `${(duration / total) * 100}%` } })
        : h('span.waterfall__milestone', { style: { left: `${(stage.start / total) * 100}%` } });
      const node = h(
        interactive ? 'button.waterfall__row' : 'div.waterfall__row',
        {
          type: interactive ? 'button' : null,
          style: { '--c': lane.color, '--i': index },
          dataset: { lane: lane.id },
          onClick: interactive ? () => state.onSelect(stage) : null,
          onPointermove: (event) => {
            chartTooltip.show({
              owner: el,
              key: stage.id,
              x: event.clientX,
              y: event.clientY,
              content: () =>
                tipContent({
                  title: stage.label,
                  subtitle: lane.label,
                  rows: [
                    { label: 'Début', value: `+${state.format(stage.start)}` },
                    ...(duration > 0
                      ? [
                          { label: 'Durée', value: state.format(duration), color: lane.color, shape: 'rect' },
                          { label: 'Part de l’appel', value: fmtPercent(duration / total), muted: true },
                        ]
                      : []),
                  ],
                  note: stage.detail,
                }),
            });
          },
          onPointerleave: () => chartTooltip.hide(el),
        },
        h('span.waterfall__label', h('span.waterfall__rail'), h('span.waterfall__name.mono.truncate', stage.label)),
        h('span.waterfall__track', bar),
        h('span.waterfall__duration.num', duration > 0 ? state.format(duration) : '—'),
      );
      return { node, stage };
    });

    const used = new Set(stages.map((stage) => stage.lane));
    clear(
      plot,
      h(
        'div.waterfall',
        { style: { '--wf-label': `${labelWidth}px`, '--wf-duration': `${durationWidth}px` } },
        state.legend
          ? Legend({ items: lanesOf(state).filter((lane) => used.has(lane.id)).map((lane) => ({ id: lane.id, label: lane.label, color: lane.color, shape: 'rect' })) })
          : null,
        h('div.waterfall__body', axis, h('div.waterfall__rows', rows.map((row) => row.node)), overlay),
      ),
    );
    applyPlayhead(state);
    applySelection(state);
  }

  const el = createChart(
    'waterfall',
    { stages: [], format: fmtUs, playhead: null, selected: null, legend: true, height: 220, ...props },
    { isEmpty: (state) => stagesOf(state).length === 0, draw },
  );

  el.setPlayhead = (position) => {
    el.state.playhead = position;
    applyPlayhead(el.state);
  };
  el.select = (id) => {
    el.state.selected = id;
    applySelection(el.state);
  };
  return el;
}
