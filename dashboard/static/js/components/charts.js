/**
 * Graphiques du laboratoire — point d'entrée unique (SVG et DOM maison, sans dépendance).
 *
 *     import { BarChart, LineChart, Timeline } from '../components/charts.js';
 *
 * Chaque graphique est une fonction qui renvoie un nœud DOM portant `update(patch)` (fusionne
 * de nouvelles propriétés et retrace) et `destroy()` (à appeler dans le nettoyage de la page).
 * Tous suivent la largeur de leur conteneur, les deux thèmes et `prefers-reduced-motion`.
 * La page `#/kit` montre chacun d'eux avec des données réalistes.
 */

export {
  CHART_COLORS,
  LANE_COLORS,
  Legend,
  chartTooltip,
  linearScale,
  logScale,
  logScaleTicks,
  niceScale,
  seriesColor,
  ticks,
  tipContent,
} from './charts/core.js';
export { BarChart, GroupedBarChart } from './charts/bar.js';
export { LineChart } from './charts/line.js';
export { Histogram, RangeChart } from './charts/distribution.js';
export { Donut, MiniBars, Sparkline, StackedBar } from './charts/spark.js';
export { TIMELINE_KINDS, Timeline } from './charts/timeline.js';
export { Waterfall } from './charts/waterfall.js';
