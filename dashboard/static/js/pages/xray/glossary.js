/**
 * Sous le capot — glossaire discret : six termes du middleware RPC en puces. Survol ou focus :
 * définition en info-bulle ; clic : définition épinglée sous la rangée, avec un renvoi vers
 * l'étape du voyage où le terme entre en jeu.
 */

import { h, clear } from '../../core/dom.js';
import { href } from '../../core/router.js';
import { Button, Tooltip } from '../../components/ui.js';

/** Définitions (texte d'explication, pas des mesures). `stage` : étape qui illustre le terme. */
const TERMS = [
  {
    id: 'stub',
    term: 'Stub',
    text: 'Objet local qui imite la procédure distante. Il en a la signature exacte, mais son corps ne calcule rien : il sérialise l’appel, l’envoie au serveur et attend la réponse.',
    stage: 'client.call',
  },
  {
    id: 'marshalling',
    term: 'Marshalling',
    text: 'Transformation d’arguments en mémoire (objets, entiers, chaînes) en une suite d’octets transportable. L’opération inverse, à la réception, est le démarshalling.',
    stage: 'client.marshal',
  },
  {
    id: 'framing',
    term: 'Tramage',
    text: 'Une connexion TCP est un flux continu : le tramage délimite les messages. Ici, un préfixe de longueur (JSON-RPC maison, gRPC) ou l’en-tête Content-Length (HTTP).',
    stage: 'client.send',
  },
  {
    id: 'skeleton',
    term: 'Squelette',
    text: 'Le pendant du stub, côté serveur. Il reçoit les octets, les décode, appelle la vraie procédure puis sérialise son résultat — ou son erreur.',
    stage: 'server.unmarshal',
  },
  {
    id: 'dispatcher',
    term: 'Dispatcher',
    text: 'Table de correspondance entre le nom reçu sur le fil et la fonction à exécuter. Un nom inconnu, et l’appel échoue avant même d’atteindre le code métier.',
    stage: 'server.dispatch',
  },
  {
    id: 'idl',
    term: 'IDL',
    text: 'Interface Definition Language : le contrat (un fichier .proto pour gRPC) qui décrit procédures et messages. Stub et squelette sont générés à partir de lui.',
    link: { label: 'Voir le contrat', route: 'contract' },
  },
];

/**
 * Glossaire en puces.
 * @param {{onStage: (stage: string) => void}} props `onStage` : montrer une étape dans le schéma.
 * @returns {HTMLElement}
 */
export function Glossary({ onStage }) {
  const panel = h('div.xr-glossary__panel', { 'aria-live': 'polite', hidden: true });
  let open = null;

  const chips = TERMS.map((item) => {
    const chip = h('button.xr-term', { type: 'button', 'aria-expanded': 'false', 'aria-controls': 'xr-glossary-panel' }, item.term);
    Tooltip(chip, item.text, { placement: 'top' });
    chip.addEventListener('click', () => show(open === item ? null : item));
    return { item, chip };
  });

  function show(item) {
    open = item;
    for (const entry of chips) entry.chip.setAttribute('aria-expanded', String(entry.item === item));
    panel.hidden = !item;
    if (!item) return;
    clear(
      panel,
      h('dl.xr-glossary__entry', h('dt', item.term), h('dd', item.text)),
      item.stage ? Button({ label: 'Voir dans le voyage', size: 'sm', variant: 'ghost', icon: 'crosshair', onClick: () => onStage(item.stage) }) : null,
      item.link ? Button({ label: item.link.label, size: 'sm', variant: 'ghost', iconRight: 'arrow-right', href: href(item.link.route) }) : null,
    );
  }

  panel.id = 'xr-glossary-panel';
  return h('div.xr-glossary', h('div.xr-glossary__row', h('span.t-label', 'Glossaire'), chips.map((entry) => entry.chip)), panel);
}
