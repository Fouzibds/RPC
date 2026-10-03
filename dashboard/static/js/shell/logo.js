/**
 * Logo : deux nœuds (client plein, serveur en anneau) reliés par un arc que parcourt un
 * paquet — l'aller-retour d'un appel distant. Dégradé de marque iris → cyan.
 */

import { svg, uid } from '../core/dom.js';

const ARC = 'M8.5 23.5C8.5 14.5 15 8.5 23.5 8.5';

/**
 * Crée le logo de l'application.
 * @param {{size?: number, animated?: boolean}} [options] `size` : côté en pixels (30) ; `animated` : paquet en mouvement.
 * @returns {SVGSVGElement}
 */
export function LogoMark({ size = 30, animated = true } = {}) {
  const gradient = uid('logo-gradient');
  return svg(
    'svg',
    { class: 'logo', viewBox: '0 0 32 32', width: size, height: size, fill: 'none', role: 'img', 'aria-label': 'RPC Explorer' },
    svg(
      'defs',
      null,
      svg(
        'linearGradient',
        { id: gradient, x1: '4', y1: '28', x2: '28', y2: '4', gradientUnits: 'userSpaceOnUse' },
        svg('stop', { offset: '0', 'stop-color': 'var(--brand-from)' }),
        svg('stop', { offset: '1', 'stop-color': 'var(--brand-to)' }),
      ),
    ),
    svg('path', { class: 'logo__arc', d: ARC, stroke: `url(#${gradient})`, 'stroke-width': '2', 'stroke-linecap': 'round' }),
    svg('circle', { class: 'logo__client', cx: '8.5', cy: '23.5', r: '3.25', fill: 'var(--brand-from)' }),
    svg('circle', { class: 'logo__server', cx: '23.5', cy: '8.5', r: '2.6', stroke: 'var(--brand-to)', 'stroke-width': '1.8' }),
    animated ? svg('circle', { class: 'logo__packet', r: '1.7', style: `offset-path: path('${ARC}')` }) : null,
  );
}
