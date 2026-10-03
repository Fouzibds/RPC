/**
 * Sous le capot — « Chronologie réelle » : cascade des étapes avec leurs durées en microsecondes
 * (couloirs client, transport, serveur) et décomposition du temps total de l'appel.
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtPercent, fmtUs } from '../../core/format.js';
import { protocol } from '../../core/protocols.js';
import { LANE_COLORS, StackedBar, Waterfall } from '../../components/charts.js';
import { Card, Col, Grid, ProtocolChip, Segmented } from '../../components/ui.js';
import { buildBreakdown, buildTimeline } from './model.js';

const LANES = [
  { id: 'client', label: 'Client · stub', color: LANE_COLORS.client },
  { id: 'network', label: 'Transport et réseau', color: LANE_COLORS.network },
  { id: 'server', label: 'Serveur · squelette et procédure', color: LANE_COLORS.server },
];

/**
 * Sélecteur de protocole synchronisé entre les sections : segments aux couleurs des protocoles,
 * ou simple puce quand une seule trace est disponible.
 * @param {{onChange: (protocolId: string) => void}} props
 * @returns {HTMLElement & {set: (protocols: string[], active: string) => void}}
 */
export function ProtocolSwitch({ onChange }) {
  const el = h('span.xr-switch');
  let signature = '';
  let control = null;
  el.set = (protocols, active) => {
    const next = protocols.join(',');
    if (next === signature) {
      control?.setValue?.(active);
      return;
    }
    signature = next;
    control =
      protocols.length > 1
        ? Segmented({
            options: protocols.map((id) => ({ value: id, label: protocol(id).short, color: protocol(id).color })),
            value: active,
            size: 'sm',
            ariaLabel: 'Protocole affiché',
            onChange,
          })
        : protocols.length
          ? ProtocolChip(protocols[0], { short: true, size: 'sm' })
          : null;
    clear(el, control);
  };
  return el;
}

/** Phrase calculée : la part du temps passée hors de la procédure métier. */
function verdict(model, parts) {
  if (!parts.total) return 'La trace ne porte pas de durée totale : impossible de décomposer cet appel.';
  if (model.protocol === 'local') {
    return `Cet appel local a duré ${fmtUs(parts.total)}, tout entier dans le processus de l’appelant : ni stub, ni marshalling, ni réseau. C’est la référence à laquelle se mesure un appel distant.`;
  }
  const name = protocol(model.protocol).label;
  if (!model.byStage.has('server.execute') && !model.byStage.has('server.error')) {
    return `Cet appel ${name} a duré ${fmtUs(parts.total)} sans jamais atteindre la procédure : tout ce temps est celui du middleware et du réseau.`;
  }
  const outside = fmtPercent(parts.outside, { decimals: parts.outside > 0.995 ? 2 : 1 });
  const shuttle = parts.serialization + parts.deserialization;
  return `${outside} du temps de cet appel ${name} s’écoule hors de la procédure : sur ${fmtUs(parts.total)}, le code métier n’en occupe que ${fmtUs(parts.execution)}. Le reste est le prix de la distance — ${fmtUs(parts.network)} de réseau et de transport, ${fmtUs(shuttle)} de marshalling et de démarshalling.`;
}

/**
 * Section « Chronologie réelle ».
 * @param {Object} props
 * @param {(stage: string) => void} props.onSelect Étape choisie dans la cascade.
 * @param {(protocolId: string) => void} props.onProtocol Protocole choisi dans le sélecteur.
 * @returns {HTMLElement & {update: (model: Object, protocols: string[]) => void,
 *   setActive: (stage: string, playing: boolean) => void, destroy: () => void}}
 */
export function Timeline({ onSelect, onProtocol }) {
  let rows = [];
  const protocolSwitch = ProtocolSwitch({ onChange: onProtocol });

  const waterfall = Waterfall({
    lanes: LANES,
    ariaLabel: 'Cascade des étapes de l’appel',
    onSelect: (stage) => onSelect(stage.stage),
    empty: { icon: 'chart-bar', title: 'Aucune étape', text: 'La trace de cet appel ne contient aucun évènement.' },
  });
  const breakdown = StackedBar({ format: fmtUs, thickness: 14, ariaLabel: 'Décomposition du temps de l’appel' });
  const total = h('span.xr-total__value.num');
  const sentence = h('p.xr-verdict', { 'aria-live': 'polite' });

  const el = Grid(
    Col(
      { span: 7, md: 12 },
      Card(
        {
          title: 'Cascade des étapes',
          subtitle: 'Chaque barre : la durée mesurée par le middleware lui-même. Cliquez une étape pour l’ouvrir dans le schéma.',
          actions: protocolSwitch,
          class: 'xr-fill',
        },
        waterfall,
      ),
    ),
    Col(
      { span: 5, md: 12 },
      Card(
        { title: 'Où passe le temps ?', subtitle: 'La durée totale vue par l’appelant, répartie par nature de travail.', class: 'xr-fill' },
        h('div.xr-total', h('span.t-label', 'Durée totale de l’appel'), total),
        breakdown,
        sentence,
      ),
    ),
  );

  el.update = (model, protocols) => {
    protocolSwitch.set(protocols, model.protocol);
    const timeline = buildTimeline(model);
    rows = timeline.stages;
    waterfall.update({
      total: timeline.total,
      selected: null,
      playhead: null,
      stages: rows.map((row) => ({
        ...row,
        detail: row.terminal
          ? `${row.stage} · durée totale de l’appel : ${fmtUs(model.totalUs)}`
          : `${row.stage}${row.size ? ` · ${fmtBytes(row.size, { exact: true })}` : ''}`,
      })),
    });

    const parts = buildBreakdown(model);
    total.textContent = fmtUs(parts.total);
    breakdown.update({
      total: parts.total,
      segments: [
        { id: 'serialization', label: 'Marshalling', value: parts.serialization, color: LANE_COLORS.client, hint: 'client.marshal + server.marshal' },
        {
          id: 'network',
          label: 'Réseau et transport',
          value: parts.network,
          color: LANE_COLORS.network,
          hint: parts.answered
            ? 'De la fin de client.marshal à client.receive, moins le travail propre du serveur (démarshalling, dispatch, exécution, marshalling)'
            : 'De la fin de client.marshal au retour de l’erreur, moins le travail propre du serveur : aucun message de réponse n’a été reçu',
        },
        { id: 'execution', label: 'Exécution de la procédure', value: parts.execution, color: LANE_COLORS.server, hint: 'server.execute' },
        {
          id: 'deserialization',
          label: 'Démarshalling',
          value: parts.deserialization,
          color: 'color-mix(in srgb, var(--lane-client) 52%, transparent)',
          hint: 'server.unmarshal + client.unmarshal',
        },
        {
          id: 'other',
          label: 'Stub et dispatch',
          value: parts.other,
          color: 'color-mix(in srgb, var(--fg-2) 42%, transparent)',
          hint: 'Le reste : code du stub avant le marshalling et après la réception, dispatch côté serveur',
        },
      ],
    });
    clear(sentence, verdict(model, parts));
  };

  el.setActive = (stage, playing) => {
    const row = rows.find((item) => item.id === stage);
    waterfall.select(row ? row.id : null);
    waterfall.setPlayhead(playing && row ? row.start + row.duration : null);
  };

  el.destroy = () => {
    waterfall.destroy();
    breakdown.destroy();
  };
  return el;
}
