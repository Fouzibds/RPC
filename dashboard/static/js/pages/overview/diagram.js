/**
 * Vue d'ensemble — « Architecture en direct » : le schéma du laboratoire, dessiné à la main en SVG.
 * À gauche l'appelant ; trois voies (stub → proxy de chaos → serveur), une par middleware ; toutes
 * aboutissent au même `InventoryService`. Un appel local court-circuite le tout. Chaque appel
 * réel (message WebSocket `call`) envoie un paquet sur sa voie.
 */

import { h, svg, clear, on } from '../../core/dom.js';
import { fmtMs, fmtNumber } from '../../core/format.js';
import { icon } from '../../core/icons.js';
import { describeNetwork } from '../../core/network.js';
import { REMOTE_PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { href } from '../../core/router.js';
import { Tooltip } from '../../components/ui.js';
import { isTransportError, serversById } from './model.js';
import { createPackets } from './packets.js';

/** Ce que chaque middleware place aux deux bouts du fil (explications, pas des mesures). */
const LANES = {
  custom: { stub: 'Stub maison', wire: 'JSON · trame TCP', server: 'Squelette maison' },
  grpc: { stub: 'Stub généré', wire: 'Protobuf · HTTP/2', server: 'Servicer gRPC' },
  rest: { stub: 'Client HTTP', wire: 'JSON · HTTP/1.1', server: 'Serveur REST' },
};

const MIN_WIDTH = 680;
const HEIGHT = 394;
const CENTER_Y = 194;
const LANE_GAP = 104;
const NODE_H = 46;
const CALLER_H = 88;
const SERVICE_H = 104;
const LOCAL_Y = 376;
const CAPTION_Y = 14;
const ROUND_TRIP_MS = 1300;
const LOCAL_TRIP_MS = 760;
const FLASH_MS = 900;

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const laneStyle = (info) => `--lane:${info.color};--lane-fg:${info.fg};--lane-soft:${info.soft};--lane-line:${info.line}`;

function geometry(width) {
  const W = Math.max(MIN_WIDTH, Math.floor(width));
  const s = clamp((W - 720) / 440, 0, 1);
  const callerW = Math.round(108 + 44 * s);
  const serviceW = Math.round(134 + 48 * s);
  const nodeW = Math.round(120 + 52 * s);
  const gap = (W - 2 - callerW - serviceW - 3 * nodeW) / 4;
  const stubX = Math.round(1 + callerW + gap);
  const proxyX = Math.round(stubX + nodeW + gap);
  const serverX = Math.round(proxyX + nodeW + gap);
  return { W, gap, callerW, serviceW, nodeW, callerX: 1, stubX, proxyX, serverX, serviceX: W - 1 - serviceW, wide: s > 0.55 };
}

function box({ x, y, w, height, kind, title, sub }) {
  const titleEl = svg('text', { class: 'ov-node__title', x: x + 12, y: y + 19 }, title);
  const subEl = svg('text', { class: 'ov-node__sub', x: x + 12, y: y + 35 }, sub);
  const g = svg(
    'g',
    { class: `ov-node ov-node--${kind}` },
    svg('rect', { class: 'ov-node__base', x: x + 0.5, y: y + 0.5, width: w, height, rx: 10 }),
    svg('rect', { class: 'ov-node__box', x: x + 0.5, y: y + 0.5, width: w, height, rx: 10 }),
    titleEl,
    subEl,
  );
  return { g, titleEl, subEl };
}

function glyph(name, x, y) {
  const el = icon(name, { size: 16 });
  el.setAttribute('x', String(x));
  el.setAttribute('y', String(y));
  el.classList.add('ov-node__icon');
  return el;
}

/** Fraction du tracé (0 à 1) à laquelle il atteint l'abscisse `x` (le tracé va de gauche à droite). */
function fractionAt(path, length, x) {
  let low = 0;
  let high = length;
  for (let i = 0; i < 18; i += 1) {
    const mid = (low + high) / 2;
    if (path.getPointAtLength(mid).x < x) low = mid;
    else high = mid;
  }
  return length ? low / length : 0;
}

function countLabel(row) {
  const calls = Number(row?.calls) || 0;
  if (!calls) return 'aucun appel';
  return `${fmtNumber(calls)} ${calls > 1 ? 'appels' : 'appel'} · ${fmtMs(row.avg_ms)} en moyenne`;
}

function proxyState(network) {
  if (!network) return { tone: 'neutral', text: '—' };
  if (network.down) return { tone: 'danger', text: 'panne simulée' };
  if (network.blackhole) return { tone: 'danger', text: 'trou noir' };
  const info = describeNetwork(network);
  return info.tone === 'neutral' ? { tone: 'neutral', text: 'transparent' } : { tone: info.tone, text: info.detail };
}

/**
 * Schéma d'architecture animé.
 * @returns {HTMLElement & {update: (state: {status?: Object|null, network?: Object|null, offline?: boolean}) => void,
 *   packet: (call: Object) => void, destroy: () => void}}
 *   `update` rafraîchit ports, états et compteurs ; `packet` anime un appel terminé (résumé d'appel).
 */
export function createDiagram() {
  const root = svg('svg', { class: 'ov-diagram__svg', role: 'img' });
  const hits = h('div.ov-diagram__hits');
  const el = h('div.ov-diagram', root, hits);
  const packets = createPackets();
  const timers = new Set();

  let parts = null;
  let pulse = null;
  let width = 0;
  let state = { status: null, network: null, offline: false };
  let detachers = [];

  function flash(target, tone) {
    target.dataset.flash = tone;
    const timer = window.setTimeout(() => {
      timers.delete(timer);
      delete target.dataset.flash;
    }, FLASH_MS);
    timers.add(timer);
  }

  function track(g, layer, path, duration, color) {
    return { layer, path, length: path.getTotalLength(), duration, color, setBusy: (busy) => g.toggleAttribute('data-busy', busy), onArrive: () => pulse?.() };
  }

  function buildLane(id, index, geo, layers) {
    const info = protocol(id);
    const texts = LANES[id];
    const y = CENTER_Y + (index - 1) * LANE_GAP;
    const top = y - NODE_H / 2;
    const { stubX, proxyX, serverX, nodeW } = geo;
    const x0 = geo.callerX + geo.callerW;
    const x2 = serverX + nodeW;
    const x3 = geo.serviceX;
    const k = geo.gap * 0.55;
    const fromY = CENTER_Y + (index - 1) * 20;
    const toY = CENTER_Y + (index - 1) * 22;
    const d = `M${x0},${fromY} C${x0 + k},${fromY} ${stubX - k},${y} ${stubX},${y} H${x2} C${x2 + k},${y} ${x3 - k},${toY} ${x3},${toY}`;

    const path = svg('path', { class: 'ov-wire', d });
    const layer = svg('g');
    const count = svg('tspan', { class: 'ov-lane__count' });
    const errors = svg('tspan', { class: 'ov-lane__errors' });
    const stub = box({ x: stubX, y: top, w: nodeW, height: NODE_H, kind: 'end', title: texts.stub, sub: texts.wire });
    const proxy = box({ x: proxyX, y: top, w: nodeW, height: NODE_H, kind: 'proxy', title: 'Proxy de chaos', sub: '—' });
    const server = box({ x: serverX, y: top, w: nodeW, height: NODE_H, kind: 'end', title: texts.server, sub: '—' });
    server.g.append(
      svg('circle', { class: 'ov-node__ring', cx: x2 - 14, cy: top + 31, r: 3.5 }),
      svg('circle', { class: 'ov-node__dot', cx: x2 - 14, cy: top + 31, r: 3.5 }),
    );
    const proxyPort = svg('text', { class: 'ov-port', x: proxyX + 12, y: top + NODE_H + 15 });
    const serverPort = svg('text', { class: 'ov-port', x: serverX + 12, y: top + NODE_H + 15 });

    const g = svg(
      'g',
      { class: 'ov-lane', style: laneStyle(info) },
      svg('rect', { class: 'ov-lane__band', x: stubX - 12.5, y: y - 53.5, width: x2 - stubX + 25, height: 98, rx: 14 }),
      path,
      svg('path', { class: 'ov-wire ov-wire--glow', d }),
      layer,
      svg('circle', { class: 'ov-lane__swatch', cx: stubX + 4, cy: y - 40, r: 3.5 }),
      svg('text', { class: 'ov-lane__label', x: stubX + 14, y: y - 36 }, info.label),
      svg('text', { class: 'ov-lane__meta', x: x2, y: y - 36, 'text-anchor': 'end' }, count, errors),
      stub.g,
      proxy.g,
      server.g,
      proxyPort,
      serverPort,
    );
    layers.lanes.append(g);
    layers.pins.append(
      svg('circle', { class: 'ov-pin', cx: x0, cy: fromY, r: 2.5, style: laneStyle(info) }),
      svg('circle', { class: 'ov-pin', cx: x3, cy: toY, r: 2.5, style: laneStyle(info) }),
    );

    const anchor = h('a.ov-lane-hit', {
      href: href('xray', { protocol: id }),
      style: { left: `${stubX - 12}px`, top: `${y - 53}px`, width: `${x2 - stubX + 24}px`, height: '97px' },
    });
    const hover = (active) => () => g.toggleAttribute('data-hover', active);
    detachers.push(
      on(anchor, 'pointerenter', hover(true)),
      on(anchor, 'pointerleave', hover(false)),
      on(anchor, 'focus', hover(true)),
      on(anchor, 'blur', hover(false)),
      Tooltip(anchor, `Ouvrir ${info.label} sous le capot`, { placement: 'top' }),
    );
    hits.append(anchor);

    const lane = { g, count, errors, proxy, server, proxyPort, serverPort, hit: anchor, track: track(g, layer, path, ROUND_TRIP_MS, info.color) };
    lane.proxyAt = fractionAt(path, lane.track.length, proxyX + nodeW / 2);
    return lane;
  }

  /** Appel local : aucun middleware, la fonction est appelée directement. */
  function buildLocal(geo, layers) {
    const info = protocol('local');
    const x0 = geo.callerX + geo.callerW / 2;
    const x1 = geo.serviceX + geo.serviceW / 2;
    const r = 14;
    const d = `M${x0},${CENTER_Y + CALLER_H / 2} V${LOCAL_Y - r} Q${x0},${LOCAL_Y} ${x0 + r},${LOCAL_Y} H${x1 - r} Q${x1},${LOCAL_Y} ${x1},${LOCAL_Y - r} V${CENTER_Y + SERVICE_H / 2}`;
    const path = svg('path', { class: 'ov-wire ov-wire--local', d });
    const layer = svg('g');
    const count = svg('tspan', { class: 'ov-lane__count' });
    const errors = svg('tspan', { class: 'ov-lane__errors' });
    const g = svg(
      'g',
      { class: 'ov-lane ov-lane--local', style: laneStyle(info) },
      path,
      svg('path', { class: 'ov-wire ov-wire--glow', d }),
      layer,
      svg('circle', { class: 'ov-lane__swatch', cx: geo.stubX + 4, cy: LOCAL_Y - 16, r: 3.5 }),
      svg(
        'text',
        { class: 'ov-lane__label', x: geo.stubX + 14, y: LOCAL_Y - 12 },
        info.label,
        svg('tspan', { class: 'ov-lane__note', dx: 8 }, geo.wide ? 'la fonction en mémoire : ni sérialisation, ni réseau' : 'ni sérialisation, ni réseau'),
      ),
      svg('text', { class: 'ov-lane__meta', x: geo.serverX + geo.nodeW, y: LOCAL_Y - 12, 'text-anchor': 'end' }, count, errors),
    );
    layers.lanes.append(g);
    return { g, count, errors, proxyAt: 1, track: track(g, layer, path, LOCAL_TRIP_MS, info.color) };
  }

  function build() {
    const geo = geometry(width);
    packets.clear();
    detachers.forEach((detach) => detach());
    detachers = [];
    clear(hits);
    root.setAttribute('width', String(geo.W));
    root.setAttribute('height', String(HEIGHT));
    root.setAttribute('viewBox', `0 0 ${geo.W} ${HEIGHT}`);
    const layers = { lanes: svg('g'), ends: svg('g'), pins: svg('g') };
    clear(root, layers.lanes, layers.ends, layers.pins);

    // Appelant et service : les deux extrémités communes aux quatre chemins.
    const callerTop = CENTER_Y - CALLER_H / 2;
    const serviceTop = CENTER_Y - SERVICE_H / 2;
    const caller = box({ x: geo.callerX, y: callerTop, w: geo.callerW, height: CALLER_H, kind: 'caller', title: 'Appelant', sub: 'CLI · dashboard' });
    caller.titleEl.setAttribute('y', String(callerTop + 52));
    caller.subEl.setAttribute('y', String(callerTop + 69));
    caller.g.append(glyph('square-terminal', geo.callerX + 12, callerTop + 13));

    const service = box({ x: geo.serviceX, y: serviceTop, w: geo.serviceW, height: SERVICE_H, kind: 'service', title: 'InventoryService', sub: 'le même objet métier' });
    service.titleEl.setAttribute('y', String(serviceTop + 51));
    service.subEl.setAttribute('y', String(serviceTop + 68));
    const stock = svg('text', { class: 'ov-node__fact', x: geo.serviceX + 12, y: serviceTop + 87 });
    const ring = svg('rect', { class: 'ov-node__pulse', x: geo.serviceX + 0.5, y: serviceTop + 0.5, width: geo.serviceW, height: SERVICE_H, rx: 10 });
    service.g.append(glyph('boxes', geo.serviceX + 12, serviceTop + 13), stock, ring);
    layers.ends.append(caller.g, service.g);
    pulse = () => ring.animate([{ opacity: 0.85 }, { opacity: 0 }], { duration: 520, easing: 'ease-out' });

    // Qui possède quoi : le code appelant, le middleware côté client, le réseau, le middleware côté serveur, le métier.
    const caption = (x, text) => svg('text', { class: 'ov-zone', x, y: CAPTION_Y }, text);
    layers.ends.append(
      caption(geo.callerX, 'Votre code'),
      caption(geo.stubX, 'Côté client'),
      caption(geo.proxyX, 'Réseau simulé'),
      caption(geo.serverX, 'Côté serveur'),
      caption(geo.serviceX, 'Code métier'),
    );

    const lanes = { local: buildLocal(geo, layers) };
    REMOTE_PROTOCOL_IDS.forEach((id, index) => {
      lanes[id] = buildLane(id, index, geo, layers);
    });
    parts = { geo, lanes, stock };
    paint();
  }

  function paintCounters(lane, row) {
    const errors = Number(row?.errors) || 0;
    lane.count.textContent = state.status ? countLabel(row) : '—';
    lane.errors.textContent = errors ? ` · ${fmtNumber(errors)} ${errors > 1 ? 'erreurs' : 'erreur'}` : '';
    lane.g.toggleAttribute('data-idle', !(Number(row?.calls) > 0));
  }

  function paint() {
    if (!parts) return;
    const { status, network, offline } = state;
    const servers = serversById(status);
    const totals = status?.totals ?? {};
    const proxy = offline ? { tone: 'neutral', text: '—' } : proxyState(network);
    el.toggleAttribute('data-offline', offline);

    for (const id of REMOTE_PROTOCOL_IDS) {
      const lane = parts.lanes[id];
      const server = servers[id];
      const known = Boolean(server) && !offline;
      const up = known && Boolean(server.up);
      lane.proxy.subEl.textContent = proxy.text;
      lane.proxy.g.dataset.tone = proxy.tone;
      lane.proxyPort.textContent = server?.proxy_port ? `:${server.proxy_port}` : '';
      lane.server.subEl.textContent = !known ? '—' : up ? 'en service' : 'arrêté';
      lane.server.g.dataset.state = !known ? 'unknown' : up ? 'up' : 'down';
      lane.serverPort.textContent = server?.port ? `:${server.port}` : '';
      paintCounters(lane, totals[id]);
      lane.hit.setAttribute(
        'aria-label',
        `${protocol(id).label} : ${countLabel(totals[id])}${known ? `, serveur ${up ? 'en service' : 'arrêté'} sur le port ${server.port}` : ''}. Ouvrir sous le capot.`,
      );
    }
    paintCounters(parts.lanes.local, totals.local);

    const inventory = status?.inventory;
    parts.stock.textContent = inventory ? `${fmtNumber(inventory.products)} produits${parts.geo.wide ? ' en stock' : ''}` : '';
    const up = REMOTE_PROTOCOL_IDS.filter((id) => servers[id]?.up).length;
    const outline = 'Schéma du laboratoire : un appelant, trois voies (stub, proxy de chaos, serveur) pour JSON-RPC maison, gRPC et REST, et un seul service d’inventaire.';
    root.setAttribute('aria-label', offline || !status ? `${outline} État inconnu.` : `${outline} ${up} serveurs sur 3 en service ; proxy : ${proxy.text}.`);
  }

  const observer = new ResizeObserver((entries) => {
    const next = Math.floor(entries[0].contentRect.width);
    if (next === width || next === 0) return;
    width = next;
    build();
  });
  observer.observe(el);

  el.update = (next) => {
    state = { ...state, ...next };
    paint();
  };

  el.packet = (call) => {
    const lane = parts?.lanes[call.protocol];
    if (!lane || document.hidden) return;
    const error = call.status === 'error';
    if (error) flash(lane.g, 'error');
    if (reducedMotion.matches) {
      if (!error) flash(lane.g, 'ok');
      return;
    }
    packets.launch(lane.track, { error, reach: error && isTransportError(call) ? lane.proxyAt : 1 });
  };

  el.destroy = () => {
    observer.disconnect();
    packets.clear();
    detachers.forEach((detach) => detach());
    detachers = [];
    timers.forEach((timer) => window.clearTimeout(timer));
    timers.clear();
    parts = null;
  };

  return el;
}
