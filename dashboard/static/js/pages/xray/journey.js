/**
 * Sous le capot — « Le voyage d'un appel » : schéma animé des douze étapes canoniques. La requête
 * parcourt le rail du haut (client → réseau → serveur), la réponse revient par celui du bas.
 * Un paquet avance d'étape en étape ; chaque station affiche la durée réellement mesurée.
 */

import { h, svg, clear } from '../../core/dom.js';
import { fmtBytes, fmtUs } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { IconButton, Segmented, Slider, Tooltip } from '../../components/ui.js';

/** Place de chaque étape canonique dans le schéma : colonne (1 à 8, la 4 est le réseau) et rail. */
const SLOTS = {
  'client.call': { col: 1, rail: 'request' },
  'client.marshal': { col: 2, rail: 'request' },
  'client.send': { col: 3, rail: 'request' },
  'server.receive': { col: 5, rail: 'request' },
  'server.unmarshal': { col: 6, rail: 'request' },
  'server.dispatch': { col: 7, rail: 'request' },
  'server.execute': { col: 8, rail: 'request' },
  'server.marshal': { col: 6, rail: 'response' },
  'server.send': { col: 5, rail: 'response' },
  'client.receive': { col: 3, rail: 'response' },
  'client.unmarshal': { col: 2, rail: 'response' },
  'client.return': { col: 1, rail: 'response' },
};

/** Composants de l'architecture, colonne par colonne : le même composant sert à l'aller et au retour. */
const PARTS = [
  { col: 1, label: 'Application', icon: 'code' },
  { col: 2, label: 'Stub', icon: 'package' },
  { col: 3, label: 'Transport', icon: 'cable' },
  { col: 5, label: 'Transport', icon: 'cable' },
  { col: 6, label: 'Squelette', icon: 'package-search' },
  { col: 7, label: 'Dispatcher', icon: 'route' },
  { col: 8, label: 'Procédure', icon: 'cpu' },
];

const SPEEDS = [0.5, 1, 2];
const MOVE_MS = 620;
const JUMP_MS = 260;
const DWELL_MS = 950;
const TURN_RADIUS = 14;

const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const easeInOut = (t) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);

/**
 * Schéma animé du pipeline d'un appel.
 * @param {Object} props
 * @param {(step: Object, model: Object) => void} props.onStep Étape active (à chaque arrivée du paquet).
 * @param {(playing: boolean) => void} [props.onPlaying] Lecture démarrée ou arrêtée.
 * @returns {HTMLElement & {update: (model: Object, options?: {autoplay?: boolean, keepStage?: boolean}) => void,
 *   goToStage: (stage: string) => void, toggle: () => void, next: () => void, previous: () => void,
 *   restart: () => void, destroy: () => void}}
 */
export function Journey({ onStep, onPlaying }) {
  let model = null;
  let stations = [];
  let zones = {};
  let position = 0; // rang de l'étape active dans model.reach
  let playing = false;
  let speed = 1;
  let frame = 0;
  let timer = 0;
  let lengths = [];
  let total = 0;
  let failLength = Infinity;
  let cursor = 0; // longueur parcourue sur le rail

  const base = svg('path', { class: 'xr-rail__base' });
  const progress = svg('path', { class: 'xr-rail__progress' });
  const failed = svg('path', { class: 'xr-rail__failed' });
  const wireOut = svg('line', { class: 'xr-rail__wire' });
  const wireBack = svg('line', { class: 'xr-rail__wire' });
  const rail = svg('svg', { class: 'xr-rail', 'aria-hidden': 'true' }, base, progress, failed, wireOut, wireBack);
  const packet = h('span.xr-packet', { 'aria-hidden': 'true' });
  const grid = h('div.xr-pipe__grid');
  const pipe = h('div.xr-pipe', rail, grid, packet);

  const restartButton = IconButton({ icon: 'rotate-ccw', label: 'Rejouer depuis le début', onClick: () => el.restart() });
  const previousButton = IconButton({ icon: 'skip-back', label: 'Étape précédente', kbd: 'arrowleft', onClick: () => el.previous() });
  const playButton = IconButton({ icon: 'play', label: 'Lire', variant: 'primary', kbd: 'space', class: 'xr-transport__play', onClick: () => el.toggle() });
  const nextButton = IconButton({ icon: 'skip-forward', label: 'Étape suivante', kbd: 'arrowright', onClick: () => el.next() });
  const counter = h('span.xr-transport__count.num');
  const scrubSlot = h('div.xr-transport__scrub');
  let scrubber = null;
  const speedControl = Segmented({
    options: SPEEDS.map((value) => ({ value, label: `${String(value).replace('.', ',')}×` })),
    value: 1,
    size: 'sm',
    ariaLabel: 'Vitesse de lecture',
    onChange: (value) => {
      speed = value;
    },
  });
  const transport = h(
    'div.xr-transport',
    { role: 'group', 'aria-label': 'Lecture du voyage' },
    h('div.xr-transport__buttons', restartButton, previousButton, playButton, nextButton),
    scrubSlot,
    counter,
    speedControl,
  );
  const el = h('div.xr-journey', pipe, transport);

  /* --- Construction du schéma ------------------------------------------- */

  function zone(name, title, caption, from, to) {
    return h(`div.xr-zone.xr-zone--${name}`, { style: { gridColumn: `${from} / ${to}` } }, h('span.xr-zone__title.t-label', title), h('span.xr-zone__caption', caption));
  }

  function wireLabel(message, railName) {
    const size = message ? fmtBytes(message.size, { exact: true }) : '—';
    return h(
      `div.xr-wire.xr-wire--${railName}`,
      h('span.xr-wire__size.num', railName === 'request' ? `${size} →` : `← ${size}`),
      h('span.xr-wire__caption', railName === 'request' ? 'requête' : 'réponse'),
    );
  }

  function station(step) {
    const slot = SLOTS[step.slot];
    if (!slot) return null;
    const reachable = Boolean(step.event);
    const duration = step.event && Number.isFinite(step.event.duration_us) ? fmtUs(step.event.duration_us) : null;
    const broken = step.state === 'failed' || step.stage === 'client.error';
    const node = h(
      reachable ? 'button.xr-station' : 'div.xr-station',
      {
        type: reachable ? 'button' : null,
        class: `xr-station--${slot.rail}`,
        style: { gridColumn: String(slot.col) },
        dataset: { kind: step.state, stage: step.stage },
        'aria-label': `Étape ${step.index + 1} : ${step.info.label}${reachable ? '' : ' (non parcourue)'}`,
        onClick: reachable ? () => el.goToStage(step.stage) : null,
      },
      h('span.xr-station__dot.num', broken ? icon('x', { size: 13, stroke: 2.5 }) : String(step.index + 1)),
      h('span.xr-station__label', step.info.label),
      h('span.xr-station__time.num', reachable ? (duration ?? 'jalon') : 'non parcourue'),
    );
    if (!reachable) Tooltip(node, 'Étape non parcourue : l’appel a échoué avant de l’atteindre.');
    return { step, node, dot: node.firstChild };
  }

  function build() {
    stations = model.steps.map(station).filter(Boolean);
    zones = {
      client: zone('client', 'Client', 'processus appelant', 1, 4),
      network: zone('network', 'Réseau', model.viaProxy ? 'proxy de chaos' : 'boucle locale', 4, 5),
      server: zone('server', 'Serveur', 'autre espace mémoire', 5, 10),
    };
    clear(
      grid,
      Object.values(zones),
      PARTS.map((part) => h('div.xr-part', { style: { gridColumn: String(part.col) } }, icon(part.icon, { size: 13 }), h('span', part.label))),
      stations.map((item) => item.node),
      wireLabel(model.request, 'request'),
      wireLabel(model.response, 'response'),
    );
    const last = Math.max(0, model.reach.length - 1);
    scrubber = Slider({
      value: 0,
      min: 0,
      max: Math.max(1, last),
      step: 1,
      showValue: false,
      disabled: last === 0,
      format: (value) => model.reach[value]?.info.label ?? '',
      ariaLabel: 'Position dans le voyage',
      onInput: (value) => {
        pause();
        goTo(value, { quick: true });
      },
    });
    clear(scrubSlot, scrubber);
  }

  /* --- Géométrie : rail, longueurs des stations ------------------------- */

  const lengthOf = (rank) => lengths[stations.findIndex(({ step }) => step === model.reach[rank])] ?? 0;

  function layout() {
    if (!model || !stations.length || !pipe.isConnected) return;
    const frameRect = pipe.getBoundingClientRect();
    if (!frameRect.width) return;
    const centers = stations.map(({ dot }) => {
      const rect = dot.getBoundingClientRect();
      return { x: rect.left - frameRect.left + rect.width / 2, y: rect.top - frameRect.top + rect.height / 2 };
    });
    const top = centers[0];
    const bottom = centers[centers.length - 1];
    const turn = zones.server.getBoundingClientRect().right - frameRect.left - 20;
    const r = Math.min(TURN_RADIUS, (bottom.y - top.y) / 2);
    const d = `M ${top.x} ${top.y} H ${turn - r} Q ${turn} ${top.y} ${turn} ${top.y + r} V ${bottom.y - r} Q ${turn} ${bottom.y} ${turn - r} ${bottom.y} H ${bottom.x}`;
    rail.setAttribute('viewBox', `0 0 ${frameRect.width} ${frameRect.height}`);
    for (const path of [base, progress, failed]) path.setAttribute('d', d);
    total = base.getTotalLength();
    lengths = stations.map(({ step }, index) => (SLOTS[step.slot].rail === 'request' ? centers[index].x - top.x : total - (centers[index].x - bottom.x)));

    // Le fil : la portion des deux rails qui traverse la zone réseau, tracée dans le sens du trajet.
    const wire = zones.network.getBoundingClientRect();
    const [left, right] = [wire.left - frameRect.left + 1, wire.right - frameRect.left - 1];
    const setWire = (line, from, to, y) => {
      line.setAttribute('x1', from);
      line.setAttribute('x2', to);
      line.setAttribute('y1', y);
      line.setAttribute('y2', y);
    };
    setWire(wireOut, left, right, top.y);
    setWire(wireBack, right, left, bottom.y);

    const broken = model.failure && model.failure.at >= 0 ? stations.findIndex(({ step }) => step.index === model.failure.at) : -1;
    failLength = broken >= 0 ? lengths[broken] : Infinity;
    draw(lengthOf(position));
  }

  function draw(length) {
    cursor = length;
    if (!total) return;
    const point = base.getPointAtLength(Math.min(total, Math.max(0, length)));
    packet.style.transform = `translate(${point.x}px, ${point.y}px)`;
    const healthy = Math.min(length, failLength);
    const broken = Math.max(0, length - failLength);
    progress.setAttribute('stroke-dasharray', `${healthy} ${total}`);
    failed.setAttribute('stroke-dasharray', broken > 0 ? `0 ${failLength} ${broken} ${total}` : `0 ${total}`);
    packet.dataset.tone = length >= failLength ? 'danger' : 'ok';
  }

  /* --- Déplacement et lecture ------------------------------------------- */

  /** États des stations ; `settled` est faux pendant un trajet : la station visée ne s'allume qu'à l'arrivée. */
  function paint(rank, settled) {
    for (const { step, node } of stations) {
      const own = model.reach.indexOf(step);
      node.dataset.state = own < 0 ? 'skipped' : own < rank || (own === rank && !settled) ? 'done' : own === rank ? 'active' : 'pending';
      if (step.event) node.setAttribute('aria-current', settled && own === rank ? 'step' : 'false');
    }
  }

  function mark() {
    const active = model.reach[position];
    paint(position, true);
    counter.textContent = `${String((active?.index ?? 0) + 1).padStart(2, '0')} / ${String(model.steps.length).padStart(2, '0')}`;
    scrubber?.setValue(position);
    previousButton.disabled = position === 0;
    nextButton.disabled = position >= model.reach.length - 1;
    if (active) onStep(active, model);
  }

  /** Va à l'étape de rang `rank` : le paquet parcourt le rail, la station s'allume à son arrivée. */
  function goTo(rank, { animate = true, quick = false, then } = {}) {
    window.cancelAnimationFrame(frame);
    const target = Math.min(model.reach.length - 1, Math.max(0, rank));
    const from = cursor;
    const to = lengthOf(target);
    const departure = Math.min(position, target);
    position = target;
    const arrive = () => {
      packet.dataset.moving = 'false';
      draw(to);
      mark();
      then?.();
    };
    const duration = animate && !reducedMotion() ? (quick ? JUMP_MS : MOVE_MS / speed) : 0;
    if (!duration || from === to) {
      arrive();
      return;
    }
    paint(departure, false);
    packet.dataset.moving = 'true';
    const started = performance.now();
    const tick = (now) => {
      const t = Math.min(1, (now - started) / duration);
      draw(from + (to - from) * easeInOut(t));
      if (t < 1) frame = window.requestAnimationFrame(tick);
      else arrive();
    };
    frame = window.requestAnimationFrame(tick);
  }

  function setPlaying(on) {
    if (playing === on) return;
    playing = on;
    playButton.setIcon(on ? 'pause' : 'play');
    playButton.setLabel(on ? 'Mettre en pause' : 'Lire');
    el.dataset.playing = String(on);
    onPlaying?.(on);
  }

  function pause() {
    window.clearTimeout(timer);
    setPlaying(false);
  }

  function advance() {
    window.clearTimeout(timer);
    if (!playing) return;
    if (position >= model.reach.length - 1) {
      pause();
      return;
    }
    timer = window.setTimeout(() => goTo(position + 1, { then: advance }), DWELL_MS / speed);
  }

  function play() {
    if (!model || model.reach.length < 2) return;
    if (position >= model.reach.length - 1) goTo(0, { animate: false });
    setPlaying(true);
    advance();
  }

  el.toggle = () => (playing ? pause() : play());
  el.next = () => {
    pause();
    goTo(position + 1, { quick: true });
  };
  el.previous = () => {
    pause();
    goTo(position - 1, { quick: true });
  };
  el.restart = () => {
    pause();
    goTo(0, { animate: false });
    play();
  };
  el.goToStage = (stage) => {
    const rank = model?.reach.findIndex((step) => step.stage === stage || step.slot === stage) ?? -1;
    if (rank < 0) return;
    pause();
    goTo(rank, { quick: true });
  };

  el.update = (next, { autoplay = false, keepStage = false } = {}) => {
    const previous = keepStage && model ? model.reach[position]?.slot : null;
    const wasPlaying = playing;
    pause();
    window.cancelAnimationFrame(frame);
    model = next;
    build();
    const kept = previous ? model.reach.findIndex((step) => step.slot === previous) : -1;
    position = kept >= 0 ? kept : 0;
    cursor = 0;
    packet.dataset.moving = 'false';
    mark();
    layout();
    if (autoplay || (keepStage && wasPlaying)) play();
  };

  const observer = new ResizeObserver(() => layout());
  observer.observe(pipe);

  el.destroy = () => {
    observer.disconnect();
    window.cancelAnimationFrame(frame);
    window.clearTimeout(timer);
  };
  return el;
}
