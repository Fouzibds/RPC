/**
 * Vue d'ensemble — paquets animés du schéma d'architecture. Chaque paquet représente un appel
 * réel : il part de l'appelant, suit le tracé de sa voie jusqu'au point atteint, puis revient.
 * Une seule boucle `requestAnimationFrame` anime tous les paquets ; elle s'arrête d'elle-même.
 */

import { svg } from '../../core/dom.js';

/** Écart entre deux paquets d'une même voie : une rafale devient un « train » lisible. */
const SPACING_MS = 70;
/** Au-delà de ce retard, les appels d'une rafale ne sont plus rejoués un par un. */
const MAX_BACKLOG_MS = 1100;

const smooth = (t) => t * t * (3 - 2 * t);

/**
 * @typedef {Object} PacketTrack Voie parcourue par les paquets.
 * @property {SVGGElement} layer Calque SVG qui reçoit les paquets de la voie (entre son fil et ses nœuds).
 * @property {SVGPathElement} path Tracé complet, de l'appelant au service.
 * @property {number} length Longueur du tracé.
 * @property {number} duration Durée d'un aller-retour, en millisecondes.
 * @property {string} color Couleur CSS du paquet.
 * @property {(busy: boolean) => void} setBusy Appelé quand la voie commence ou cesse d'être parcourue.
 * @property {() => void} [onArrive] Appelé quand un paquet atteint le bout du tracé.
 */

/**
 * Moteur d'animation des paquets.
 * @returns {{launch: (track: PacketTrack, options?: {error?: boolean, reach?: number}) => boolean, clear: () => void}}
 *   `launch` renvoie `false` si le paquet a été écarté (voie déjà saturée) ; `reach` (0 à 1) est la
 *   fraction du tracé parcourue avant le demi-tour ; `error` colore le retour en rouge.
 */
export function createPackets() {
  const packets = [];
  const queues = new WeakMap();
  let frame = 0;

  function retire(index) {
    const [packet] = packets.splice(index, 1);
    packet.node.remove();
    const queue = queues.get(packet.track);
    queue.inFlight -= 1;
    if (queue.inFlight === 0) packet.track.setBusy(false);
  }

  function step(now) {
    frame = 0;
    for (let index = packets.length - 1; index >= 0; index -= 1) {
      const packet = packets[index];
      const t = (now - packet.start) / packet.track.duration;
      if (t < 0) continue;
      if (t >= 1) {
        retire(index);
        continue;
      }
      const outbound = t < 0.5;
      if (!outbound && !packet.turned) {
        packet.turned = true;
        if (packet.error) packet.node.dataset.error = 'true';
        else if (packet.reach === 1) packet.track.onArrive?.();
      }
      const progress = outbound ? smooth(t * 2) : 1 - smooth((t - 0.5) * 2);
      const point = packet.track.path.getPointAtLength(progress * packet.reach * packet.track.length);
      packet.node.setAttribute('transform', `translate(${point.x.toFixed(1)},${point.y.toFixed(1)})`);
      if (!packet.shown) {
        packet.shown = true;
        packet.node.removeAttribute('visibility');
      }
    }
    if (packets.length) frame = requestAnimationFrame(step);
  }

  function launch(track, { error = false, reach = 1 } = {}) {
    let queue = queues.get(track);
    if (!queue) {
      queue = { next: 0, inFlight: 0 };
      queues.set(track, queue);
    }
    const now = performance.now();
    const start = Math.max(now, queue.next);
    if (start - now > MAX_BACKLOG_MS) return false;
    queue.next = start + SPACING_MS;

    const node = svg(
      'g',
      { class: 'ov-packet', style: `--c:${track.color}`, visibility: 'hidden' },
      svg('circle', { class: 'ov-packet__halo', r: 8 }),
      svg('circle', { class: 'ov-packet__core', r: 3.2 }),
    );
    track.layer.append(node);
    packets.push({ node, track, start, error, reach, turned: false, shown: false });
    queue.inFlight += 1;
    if (queue.inFlight === 1) track.setBusy(true);
    if (!frame) frame = requestAnimationFrame(step);
    return true;
  }

  function clear() {
    cancelAnimationFrame(frame);
    frame = 0;
    while (packets.length) retire(packets.length - 1);
  }

  return { launch, clear };
}
