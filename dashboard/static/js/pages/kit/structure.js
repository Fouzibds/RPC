/**
 * Guide de style — structure : cartes, grille 12 colonnes, piles et filets.
 */

import { h } from '../../core/dom.js';
import { fmtBytes, fmtMs } from '../../core/format.js';
import { PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { Badge, Button, Card, Col, Divider, Grid, IconButton, ProtocolChip, Section, Stack, StatusDot } from '../../components/ui.js';
import { DemoCard, Specimen, Usage } from './demo.js';

function cards() {
  return Section(
    {
      title: 'Cartes',
      description: 'Card : bordure d’un pixel, reflet supérieur, ombre douce. En-tête, actions, pied, marges et liseré sont facultatifs.',
    },
    Grid(
      Col(
        { span: 6, md: 12 },
        Card(
          {
            title: 'Serveur gRPC',
            subtitle: 'HTTP/2 + Protobuf binaire (contrat IDL)',
            icon: 'server',
            actions: [Badge({ label: 'En service', tone: 'success', dot: true, pulse: true }), IconButton({ icon: 'ellipsis', label: 'Plus d’actions', size: 'sm' })],
            footer: [h('span', 'Port direct :50051 · proxy :50151'), Button({ label: 'Inspecter', size: 'sm', variant: 'ghost', iconRight: 'arrow-right' })],
          },
          h('p.fg-1', 'Une carte complète : icône, titre, sous-titre, actions à droite, corps et pied séparé par un filet.'),
        ),
      ),
      Col(
        { span: 3, md: 6 },
        Card({ title: 'Cliquable', subtitle: 'interactive: true', interactive: true }, h('p.fg-2.t-small', 'Réagit au survol et à l’appui : pour une carte qui mène quelque part.')),
      ),
      Col({ span: 3, md: 6 }, Card({ title: 'Simple cadre', subtitle: 'flush: true', flush: true }, h('p.fg-2.t-small', 'Sans fond ni ombre : pour regrouper sans alourdir.'))),
      ...PROTOCOL_IDS.map((id) =>
        Col(
          { span: 3, md: 6 },
          Card(
            { protocol: id, padding: 'md' },
            Stack(
              { gap: 10, align: 'start' },
              ProtocolChip(id, { variant: 'plain' }),
              h('p.fg-2.t-small', protocol(id).transport),
              h('code', `protocol: '${id}'`),
            ),
          ),
        ),
      ),
      ...[
        ['accent', 'Mise en avant'],
        ['success', 'Compatible'],
        ['warning', 'À surveiller'],
        ['danger', 'Rupture de contrat'],
      ].map(([tone, label]) =>
        Col({ span: 3, md: 6 }, Card({ accent: tone, padding: 'sm' }, Stack({ gap: 4 }, h('span.t-label', `accent: '${tone}'`), h('span', label), h('span.fg-2.t-small', 'padding: sm')))),
      ),
    ),
  );
}

function grid() {
  const cell = (span, md) => Col({ span, md }, h('div.kit-cell.num', md ? `${span} → ${md}` : String(span)));
  return Section(
    { title: 'Grille, piles, filets', description: 'Grid à 12 colonnes (gouttière 16 px) ; les colonnes peuvent s’élargir quand la zone de contenu rétrécit (md < 1100 px, sm < 840 px).' },
    Grid(
      DemoCard(
        { title: 'Grid et Col', subtitle: 'Les valeurs « n → m » passent à m colonnes sous 1100 px', span: 7 },
        Grid({ gap: 8 }, cell(3, 6), cell(3, 6), cell(3, 6), cell(3, 6)),
        Grid({ gap: 8 }, cell(4), cell(8)),
        Grid({ gap: 8 }, cell(6), cell(2), cell(2), cell(2)),
        Usage("Grid(Col({ span: 3, md: 6, sm: 12 }, Card(…)), Col({ span: 9, md: 6, sm: 12 }, Card(…)))"),
      ),
      DemoCard(
        { title: 'Stack et Divider', span: 5 },
        Specimen(
          'Rangée',
          Stack(
            { direction: 'row', gap: 12 },
            StatusDot({ tone: 'success', label: '3 serveurs' }),
            Divider({ vertical: true }),
            h('span.num', fmtMs(0.318)),
            Divider({ vertical: true }),
            h('span.num', fmtBytes(121)),
          ),
        ),
        Specimen('Colonne', Stack({ gap: 6 }, h('span', 'Requête'), h('span.fg-2', 'Réponse'), h('span.fg-3', 'Erreur'))),
        Divider(),
        Divider({ label: 'Étapes côté serveur' }),
        Usage("Stack({ direction: 'row', gap: 12, align: 'center', justify: 'between', wrap: true }, …)\nDivider({ vertical, label })"),
      ),
    ),
  );
}

/**
 * Sections « structure » du guide de style.
 * @returns {HTMLElement[]}
 */
export function structure() {
  return [cards(), grid()];
}
