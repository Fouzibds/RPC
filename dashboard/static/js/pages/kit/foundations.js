/**
 * Guide de style — fondations : couleurs, typographie, espacement, rayons, ombres, icônes.
 */

import { h, clear } from '../../core/dom.js';
import { fmtBytes, fmtMs, fmtNumber, fmtRate } from '../../core/format.js';
import { icon, iconNames } from '../../core/icons.js';
import { PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import { cssVar, onThemeChange } from '../../core/theme.js';
import { Card, Col, Grid, Input, Section } from '../../components/ui.js';
import { DemoCard, Specimen } from './demo.js';

function Swatch(token, label) {
  const value = h('span.kit-swatch__value.num');
  const el = h(
    'div.kit-swatch',
    h('span.kit-swatch__chip', { style: { background: `var(${token})` } }),
    h('span.kit-swatch__meta', h('span.kit-swatch__name', label ?? token.replace(/^--/, '')), value),
  );
  el.refresh = () => {
    const raw = cssVar(token);
    value.textContent = raw.startsWith('#') ? raw.toUpperCase() : raw.replace(/\s+/g, '').replace(/0\./g, '.');
  };
  el.refresh();
  return el;
}

function ToneRow(name, label) {
  return h(
    'div.kit-tone',
    { style: { '--c': `var(--${name})`, '--c-fg': `var(--${name}-fg)`, '--c-soft': `var(--${name}-soft)`, '--c-line': `var(--${name}-line)` } },
    h('span.kit-tone__solid'),
    h('span.kit-tone__soft', h('span.kit-tone__sample', label)),
    h('span.kit-tone__tokens.num', `--${name} · -fg · -soft · -line`),
  );
}

function colors(cleanups) {
  const swatches = [];
  const add = (token, label) => {
    const swatch = Swatch(token, label);
    swatches.push(swatch);
    return swatch;
  };
  cleanups.push(onThemeChange(() => swatches.forEach((swatch) => swatch.refresh())));

  return Section(
    { title: 'Couleurs', description: 'Toutes les couleurs sont des variables CSS définies dans css/tokens.css — jamais de valeur en dur dans une page.' },
    Grid(
      DemoCard(
        { title: 'Surfaces', subtitle: 'De l’application (bg-0) au relief (bg-4)', span: 6 },
        h('div.kit-swatches', ['--bg-0', '--bg-1', '--bg-2', '--bg-3', '--bg-4', '--bg-inset', '--bg-hover', '--bg-active', '--bg-overlay'].map((token) => add(token))),
      ),
      DemoCard(
        { title: 'Texte, bordures et accent', subtitle: 'Quatre niveaux de texte, trois de bordure, l’iris de marque', span: 6 },
        h(
          'div.kit-swatches',
          ['--fg-0', '--fg-1', '--fg-2', '--fg-3', '--border-1', '--border-2', '--accent', '--accent-fg', '--accent-solid'].map((token) => add(token)),
        ),
      ),
      DemoCard(
        { title: 'Protocoles', subtitle: 'Identiques partout : graphiques, puces, pipelines', span: 6 },
        h(
          'div.kit-tones',
          PROTOCOL_IDS.map((id) => ToneRow(`proto-${id}`, protocol(id).label)),
        ),
      ),
      DemoCard(
        { title: 'États', subtitle: 'Succès, avertissement, danger, information', span: 6 },
        h(
          'div.kit-tones',
          [
            ['success', 'Appel réussi'],
            ['warning', 'Latence élevée'],
            ['danger', 'Délai dépassé'],
            ['info', 'Nouvelle tentative'],
          ].map(([name, label]) => ToneRow(name, label)),
        ),
        h('div.kit-brand', h('span.kit-brand__bar'), h('span.fg-2.t-small', 'Dégradé de marque (--gradient-brand) — réservé au logo et aux moments forts')),
      ),
    ),
  );
}

function typography() {
  const scale = [
    ['40', 't-display', '1 284 req/s'],
    ['28', 't-title', 'Sous le capot d’un appel distant'],
    ['20', 't-heading', 'Distribution des latences'],
    ['16', 't-subheading', 'Conditions réseau simulées'],
    ['14', 't-body-lg', 'Le stub sérialise le nom de la procédure et ses arguments en une suite d’octets.'],
    ['13', 't-body', 'Texte courant de l’interface : dense, lisible, 13 px sur 20 px d’interligne.'],
    ['12', 't-small', 'Texte secondaire, aides de champ, légendes de graphique.'],
    ['11', 't-micro', 'Micro-texte : horodatages, compteurs, annotations.'],
  ];
  return Section(
    { title: 'Typographie', description: 'Inter pour l’interface, JetBrains Mono pour le code, les octets et tous les nombres mesurés.' },
    Grid(
      Col(
        { span: 7, md: 12 },
        Card(
          { title: 'Échelle', subtitle: '11 · 12 · 13 · 14 · 16 · 20 · 28 · 40 px' },
          h(
            'div.kit-type',
            scale.map(([size, className, sample]) => h('div.kit-type__row', h('span.kit-type__size.num', `${size} px`), h(`span.${className}.truncate`, sample), h('code.kit-type__class', `.${className}`))),
            h('div.kit-type__row', h('span.kit-type__size.num', '11 px'), h('span.t-label', 'Libellé de section'), h('code.kit-type__class', '.t-label')),
          ),
        ),
      ),
      Col(
        { span: 5, md: 12 },
        Card(
          { title: 'Nombres mesurés', subtitle: 'Classe .num : chasse fixe, chiffres tabulaires' },
          h(
            'div.kit-numbers',
            [
              ['Latence médiane', fmtMs(0.142)],
              ['p99', fmtMs(12.84)],
              ['Charge utile JSON-RPC', fmtBytes(318)],
              ['Charge utile Protobuf', fmtBytes(121)],
              ['Catalogue (1 000 produits)', fmtBytes(184320)],
              ['Débit', fmtRate(6890)],
              ['Appels', fmtNumber(1284056)],
            ].map(([label, value]) => h('div.kit-numbers__row', h('span.fg-1', label), h('span.num', value))),
          ),
          h('pre.kit-mono.mono', '{"jsonrpc":"2.0","id":"custom-000042",\n "method":"update_stock",\n "params":{"product_id":"SKU-1001","delta":-3}}'),
        ),
      ),
    ),
  );
}

function shapes() {
  const spacing = [1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16];
  const radii = [
    ['--r-xs', '4'],
    ['--r-chip', '6'],
    ['--r-sm', '8'],
    ['--r-control', '10'],
    ['--r-pop', '12'],
    ['--r-card', '14'],
  ];
  return Section(
    { title: 'Espacement, rayons, reliefs', description: 'Échelle de 4 px ; rayons 14 (cartes), 10 (contrôles), 6 (puces) ; ombres très douces.' },
    Grid(
      DemoCard(
        { title: 'Espacement', subtitle: 'Variables --sp-1 à --sp-16', span: 5 },
        h(
          'div.kit-spacing',
          spacing.map((step) => h('div.kit-spacing__row', h('code', `--sp-${step}`), h('span.kit-spacing__bar', { style: { width: `var(--sp-${step})` } }), h('span.num.fg-2', `${step * 4} px`))),
        ),
      ),
      DemoCard(
        { title: 'Rayons et reliefs', subtitle: 'Bordure d’un pixel, reflet supérieur, ombre douce', span: 7 },
        Specimen('Rayons', h('div.kit-radii', radii.map(([token, px]) => h('div.kit-radius', h('span.kit-radius__box', { style: { borderRadius: `var(${token})` } }), h('code', token.replace('--', '')), h('span.num.fg-2', `${px} px`))))),
        Specimen(
          'Reliefs',
          h(
            'div.kit-shadows',
            [
              ['--shadow-xs', 'xs'],
              ['--shadow-card', 'card'],
              ['--shadow-pop', 'pop'],
              ['--shadow-modal', 'modal'],
            ].map(([token, name]) => h('div.kit-shadow', { style: { boxShadow: `var(--highlight-top), var(${token})` } }, h('code', `shadow-${name}`))),
          ),
        ),
        Specimen('Mouvement', h('p.fg-1', 'Transitions de 120 à 200 ms, courbe ', h('code', 'cubic-bezier(.2, .8, .2, 1)'), ', apparition échelonnée des cartes ; prefers-reduced-motion respecté.')),
      ),
    ),
  );
}

function icons() {
  const names = iconNames();
  const grid = h('div.kit-icons');
  const count = h('span.fg-2.t-small.num');
  function render(query) {
    const needle = query.trim().toLowerCase();
    const shown = names.filter((name) => name.includes(needle));
    clear(
      grid,
      shown.length
        ? shown.map((name) => h('div.kit-icon', { title: name }, icon(name, { size: 18 }), h('span.kit-icon__name.truncate', name)))
        : h('p.fg-2', 'Aucune icône ne correspond.'),
    );
    count.textContent = `${shown.length} / ${names.length}`;
  }
  render('');
  return Section(
    { title: 'Icônes', description: 'Jeu Lucide embarqué : icon(nom, { size, stroke, class }) renvoie un SVG en currentColor.' },
    Card(
      {
        title: 'Bibliothèque',
        subtitle: 'core/icons.js — aucune requête réseau',
        actions: [count, Input({ icon: 'search', placeholder: 'Filtrer…', size: 'sm', width: 200, ariaLabel: 'Filtrer les icônes', onInput: render })],
      },
      grid,
    ),
  );
}

/**
 * Sections « fondations » du guide de style.
 * @param {Array<() => void>} cleanups Reçoit les fonctions de nettoyage à appeler au démontage.
 * @returns {HTMLElement[]}
 */
export function foundations(cleanups) {
  return [colors(cleanups), typography(), shapes(), icons()];
}
