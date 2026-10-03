/**
 * Guide de style — contrôles : boutons, champs, sélection, curseurs, bascules, onglets, étapes.
 */

import { h } from '../../core/dom.js';
import { fmtMs, fmtNumber, fmtPercent } from '../../core/format.js';
import { PROTOCOL_IDS, protocol } from '../../core/protocols.js';
import {
  Badge,
  Button,
  ButtonGroup,
  Checkbox,
  Chip,
  CopyButton,
  Field,
  Grid,
  IconButton,
  Input,
  NumberInput,
  Section,
  Segmented,
  Select,
  Slider,
  Stepper,
  Tabs,
  Textarea,
  Toggle,
  openMenu,
} from '../../components/ui.js';
import { DemoCard, Specimen, Usage } from './demo.js';

const METHOD_OPTIONS = [
  { value: 'calculate_factorial', label: 'calculate_factorial', icon: 'calculator', hint: 'unaire', description: 'Calcul pur, sans état' },
  { value: 'get_product_details', label: 'get_product_details', icon: 'package-search', hint: 'unaire', description: 'Lecture d’une fiche produit' },
  { value: 'update_stock', label: 'update_stock', icon: 'warehouse', hint: 'unaire', description: 'Écriture non idempotente' },
  { value: 'list_products', label: 'list_products', icon: 'list', hint: 'unaire', description: 'Catalogue paginé' },
  { value: 'stream_analytics', label: 'stream_analytics', icon: 'activity', hint: 'flux', description: 'Streaming serveur' },
  { value: 'check_stock', label: 'check_stock', icon: 'arrow-left-right', hint: 'gRPC', description: 'Flux bidirectionnel', disabled: true },
];

function buttons() {
  const loading = Button({ label: 'Lancer le benchmark', variant: 'primary', icon: 'play' });
  loading.addEventListener('click', () => {
    loading.setLoading(true);
    loading.setLabel('Mesure en cours…');
    window.setTimeout(() => {
      loading.setLoading(false);
      loading.setLabel('Lancer le benchmark');
    }, 1800);
  });

  const menuButton = Button({
    label: 'Exporter',
    icon: 'download',
    iconRight: 'chevron-down',
    onClick: () =>
      openMenu(menuButton, [
        { heading: 'Rapport' },
        { label: 'JSON', icon: 'file-json', hint: '12,4 ko' },
        { label: 'Markdown', icon: 'file-text', hint: '3,1 ko' },
        { label: 'CSV', icon: 'table', hint: '1,8 ko' },
        'divider',
        { label: 'Copier le lien', icon: 'link', kbd: 'mod+c' },
        { label: 'Supprimer le rapport', icon: 'trash-2', tone: 'danger' },
      ]),
  });

  // setDisabled() et ariaLabel : un verrou commun désactive un Button et un IconButton après leur création.
  const dissect = Button({ label: 'Disséquer', icon: 'scan-search', ariaLabel: 'Disséquer l’appel gRPC dans Sous le capot' });
  const stop = IconButton({ icon: 'square', label: 'Arrêter', ariaLabel: 'Arrêter le flux en cours', variant: 'secondary' });
  const print = Button({ label: 'Imprimer le bilan', icon: 'printer', variant: 'ghost' });
  const busy = Toggle({
    size: 'sm',
    label: 'Laboratoire occupé',
    title: 'Pendant un benchmark, le laboratoire refuse les autres actions : les boutons se verrouillent',
    onChange: (on) => [dissect, stop, print].forEach((button) => button.setDisabled(on)),
  });

  return Section(
    { title: 'Boutons', description: 'Quatre variantes, trois tailles ; états survol, actif, focus, désactivé et chargement.' },
    Grid(
      DemoCard(
        { title: 'Button', subtitle: 'primary · secondary · ghost · danger', span: 7 },
        Specimen('Variantes', Button({ label: 'Exécuter l’appel', variant: 'primary', icon: 'play' }), Button({ label: 'Inspecter', icon: 'scan-search' }), Button({ label: 'Annuler', variant: 'ghost' }), Button({ label: 'Réinitialiser', variant: 'danger', icon: 'rotate-ccw' })),
        Specimen('Tailles', Button({ label: 'Petit', size: 'sm', variant: 'primary' }), Button({ label: 'Moyen', variant: 'primary' }), Button({ label: 'Grand', size: 'lg', variant: 'primary' }), Button({ label: 'Petit', size: 'sm' }), Button({ label: 'Moyen' }), Button({ label: 'Grand', size: 'lg' })),
        Specimen('États', loading, Button({ label: 'Chargement', loading: true }), Button({ label: 'Désactivé', variant: 'primary', disabled: true }), Button({ label: 'Désactivé', disabled: true })),
        Specimen('Raccourcis', Button({ label: 'Rechercher', icon: 'search', kbd: 'mod+k' }), Button({ label: 'Envoyer', variant: 'primary', iconRight: 'corner-down-left' }), Button({ label: 'Documentation', variant: 'ghost', iconRight: 'external-link', href: '#/learn' })),
        Specimen('setDisabled', dissect, stop, print, busy),
        Usage("Button({ label: 'Exécuter', variant: 'primary', icon: 'play', ariaLabel, onClick })  →  .setLoading(true) · .setLabel() · .setDisabled(true)\nIconButton({ icon, label, ariaLabel })  →  .setDisabled(true)"),
      ),
      DemoCard(
        { title: 'IconButton, ButtonGroup, menu', subtitle: 'Le libellé d’un IconButton sert de nom accessible et d’info-bulle', span: 5 },
        Specimen('IconButton', IconButton({ icon: 'refresh-cw', label: 'Actualiser' }), IconButton({ icon: 'pause', label: 'Mettre en pause', active: true }), IconButton({ icon: 'settings', label: 'Réglages', variant: 'secondary' }), IconButton({ icon: 'play', label: 'Lancer', variant: 'primary' }), IconButton({ icon: 'trash-2', label: 'Effacer', disabled: true })),
        Specimen('Tailles', IconButton({ icon: 'copy', label: 'Copier', size: 'sm', variant: 'secondary' }), IconButton({ icon: 'copy', label: 'Copier', variant: 'secondary' }), IconButton({ icon: 'copy', label: 'Copier', size: 'lg', variant: 'secondary' }), CopyButton({ text: 'custom-000042', label: 'Copier l’identifiant' })),
        Specimen('ButtonGroup', ButtonGroup(Button({ label: 'Précédent', icon: 'step-back' }), Button({ label: 'Lecture', icon: 'play' }), Button({ label: 'Suivant', iconRight: 'step-forward' }))),
        Specimen('Menu', menuButton),
        Usage('openMenu(ancre, [{ label, icon, hint, kbd, tone, onSelect }, \'divider\', { heading }])'),
      ),
    ),
  );
}

function forms() {
  const latency = Slider({
    value: 80,
    min: 0,
    max: 500,
    step: 5,
    format: (v) => fmtMs(v, { decimals: 0 }),
    ticks: [0, 100, 200, 300, 400, 500],
    ariaLabel: 'Latence ajoutée',
  });
  const resets = Slider({ value: 0.15, min: 0, max: 1, step: 0.01, tone: 'danger', format: (v) => fmtPercent(v, { decimals: 0 }), ariaLabel: 'Probabilité de coupure' });
  const iterations = Slider({ value: 1000, min: 100, max: 5000, step: 100, tone: 'info', format: (v) => fmtNumber(v), ariaLabel: 'Itérations' });

  const params = Textarea({ mono: true, rows: 4, value: '{\n  "product_id": "SKU-1001",\n  "delta": -3\n}', ariaLabel: 'Paramètres JSON' });
  const paramsField = Field({ label: 'Paramètres (JSON)', control: params, hint: 'Envoyés tels quels à la procédure.', aside: 'facultatif' });
  params.addEventListener('input', () => {
    try {
      JSON.parse(params.value);
      params.setInvalid(false);
      paramsField.setError('');
    } catch {
      params.setInvalid(true);
      paramsField.setError('JSON invalide : vérifiez les guillemets et les virgules.');
    }
  });

  return Section(
    { title: 'Champs', description: 'Tous les contrôles partagent hauteurs (28 / 32 / 40 px), rayon de 10 px et anneau de focus iris.' },
    Grid(
      DemoCard(
        { title: 'Input, Textarea, NumberInput', span: 6 },
        Specimen('Input', Input({ placeholder: 'SKU-1001', icon: 'search', width: 200, ariaLabel: 'Référence' }), Input({ value: 'SKU-1001', mono: true, width: 150, ariaLabel: 'Référence' }), Input({ value: '127.0.0.1', prefix: 'tcp://', suffix: ':9101', mono: true, width: 220, ariaLabel: 'Adresse' })),
        Specimen('États', Input({ value: 'sku 1001', invalid: true, width: 170, ariaLabel: 'Invalide' }), Input({ value: 'Lecture seule', disabled: true, width: 170, ariaLabel: 'Désactivé' }), Input({ placeholder: 'Petit', size: 'sm', width: 110, ariaLabel: 'Petit' }), Input({ placeholder: 'Grand', size: 'lg', width: 140, ariaLabel: 'Grand' })),
        Specimen('NumberInput', NumberInput({ value: 1000, min: 10, max: 10000, step: 100, ariaLabel: 'Itérations' }), NumberInput({ value: 80, min: 0, max: 2000, step: 10, suffix: 'ms', ariaLabel: 'Latence' }), NumberInput({ value: 2.5, min: 0, max: 30, step: 0.5, suffix: 's', width: 120, ariaLabel: 'Délai' }), NumberInput({ value: 3, min: 1, max: 5, disabled: true, width: 104, ariaLabel: 'Tentatives' })),
        paramsField,
      ),
      DemoCard(
        { title: 'Select, Segmented, Field', span: 6 },
        Specimen(
          'Select',
          Select({ value: 'update_stock', options: METHOD_OPTIONS, width: 240, ariaLabel: 'Procédure' }),
          Select({ value: 'grpc', width: 190, ariaLabel: 'Protocole', options: PROTOCOL_IDS.map((id) => ({ value: id, label: protocol(id).label, color: protocol(id).color })) }),
          Select({ placeholder: 'Catégorie…', size: 'sm', width: 140, ariaLabel: 'Catégorie', options: ['peripherals', 'audio', 'storage', 'network'].map((value) => ({ value, label: value })) }),
        ),
        Specimen(
          'Segmented',
          Segmented({ value: 'sync', ariaLabel: 'Mode d’appel', options: [{ value: 'sync', label: 'Synchrone', icon: 'arrow-right' }, { value: 'async', label: 'Asynchrone', icon: 'shuffle' }, { value: 'stream', label: 'Flux', icon: 'activity' }] }),
          Segmented({ size: 'sm', value: 'grpc', ariaLabel: 'Protocole', options: [{ value: 'all', label: 'Tous' }, ...PROTOCOL_IDS.slice(1).map((id) => ({ value: id, label: protocol(id).short, color: protocol(id).color }))] }),
          Segmented({ size: 'sm', value: 'hex', ariaLabel: 'Affichage', options: [{ value: 'json', icon: 'braces', title: 'JSON' }, { value: 'hex', icon: 'binary', title: 'Hexadécimal' }, { value: 'tree', icon: 'list-tree', title: 'Arbre' }] }),
        ),
        Grid(
          { gap: 16 },
          h('div.col.col-6', Field({ label: 'Procédure', control: Select({ value: 'get_product_details', options: METHOD_OPTIONS, block: true }), hint: 'Issue du catalogue du laboratoire.' })),
          h('div.col.col-6', Field({ label: 'Délai maximal', control: NumberInput({ value: 5, min: 0.1, max: 30, step: 0.5, suffix: 's', width: '100%' }), aside: 'par appel' })),
        ),
        Field({ inline: true, label: 'Passer par le proxy de chaos', hint: 'Les conditions réseau simulées s’appliquent à l’appel.', control: Toggle({ checked: true, ariaLabel: 'Passer par le proxy de chaos' }) }),
      ),
      dynamicCard(),
      DemoCard(
        { title: 'Slider', subtitle: 'Piste remplie, bulle de valeur, graduations cliquables', span: 6 },
        Field({ label: 'Latence ajoutée (aller-retour)', control: latency }),
        Field({ label: 'Probabilité de coupure', control: resets, hint: 'Ton « danger » : la piste prend la couleur de l’état.' }),
        Field({ label: 'Itérations', control: iterations }),
        Usage("Slider({ value, min, max, step, format: (v) => fmtMs(v), ticks: [0, 100, 200], onInput, onChange })"),
      ),
      DemoCard(
        { title: 'Toggle, Checkbox', span: 6 },
        Specimen('Toggle', Toggle({ checked: true, label: 'Disjoncteur', description: 'S’ouvre après 3 échecs consécutifs.' }), Toggle({ label: 'Clé d’idempotence automatique' })),
        Specimen('États', Toggle({ size: 'sm', checked: true, label: 'Étapes internes' }), Toggle({ checked: true, disabled: true, label: 'Verrouillé' }), Toggle({ disabled: true, ariaLabel: 'Désactivé' })),
        Specimen('Checkbox', Checkbox({ checked: true, label: 'JSON-RPC maison' }), Checkbox({ checked: true, label: 'gRPC' }), Checkbox({ label: 'REST' }), Checkbox({ indeterminate: true, label: 'Tous' }), Checkbox({ checked: true, disabled: true, label: 'Local' })),
        Specimen('Détaillée', Checkbox({ checked: true, label: 'Rejouer les appels non idempotents', description: 'Dangereux sans clé d’idempotence : l’effet peut être appliqué deux fois.' })),
      ),
    ),
  );
}

/** Procédures du sélecteur dynamique : une étiquette par option, et une option désactivée qui dit pourquoi. */
const CATALOG = [
  { value: 'get_product_details', label: 'get_product_details', icon: 'package-search', description: 'Lecture d’une fiche produit', stream: false },
  { value: 'update_stock', label: 'update_stock', icon: 'warehouse', description: 'Écriture non idempotente', badge: { label: 'écriture', tone: 'warning' }, stream: false },
  { value: 'stream_analytics', label: 'stream_analytics', icon: 'activity', description: 'Streaming serveur', badge: { label: 'flux', tone: 'info' }, stream: true },
  { value: 'check_stock', label: 'check_stock', icon: 'arrow-left-right', description: 'Flux bidirectionnel', badge: 'gRPC seul', disabled: true, title: 'Indisponible : le protocole choisi (REST) n’a pas de flux bidirectionnel', stream: true },
];

/** Modes d'appel permis par une procédure : les segments interdits restent visibles, avec la raison au survol. */
function modeOptions(procedure) {
  const stream = Boolean(procedure?.stream);
  return [
    { value: 'sync', label: 'Synchrone', icon: 'arrow-right', disabled: stream, title: stream ? 'Une procédure à flux ne s’appelle qu’en mode Flux' : undefined },
    { value: 'async', label: 'Asynchrone × 8', icon: 'shuffle', disabled: stream, title: stream ? 'Une procédure à flux ne s’appelle qu’en mode Flux' : undefined },
    { value: 'stream', label: 'Flux', icon: 'activity', disabled: !stream, title: stream ? undefined : 'Réservé aux procédures à flux' },
  ];
}

/**
 * Carte « mises à jour après création » : Select (setPlaceholder, étiquette et option désactivée
 * expliquée), Segmented (setOptions, setDisabled), Field nommant un groupe de boutons.
 */
function dynamicCard() {
  const mode = Segmented({ value: 'sync', block: true, options: modeOptions(null) });
  const procedure = Select({
    options: [],
    placeholder: 'Chargement du catalogue…',
    block: true,
    disabled: true,
    onChange: (_, option) => mode.setOptions(modeOptions(option)),
  });
  const protocols = h(
    'div.kit-chips',
    PROTOCOL_IDS.slice(1).map((id, index) => Chip({ label: protocol(id).short, color: protocol(id).color, selected: index < 2, onToggle: () => {} })),
  );
  const state = Badge({ label: 'Chargement', tone: 'info', dot: true, pulse: true, size: 'sm' });

  let timer = 0;
  function load() {
    window.clearTimeout(timer);
    procedure.setOptions([]);
    procedure.setValue(undefined);
    procedure.setPlaceholder('Chargement du catalogue…');
    procedure.setDisabled(true);
    mode.setDisabled(true);
    state.update({ label: 'Chargement', tone: 'info', dot: true, pulse: true, icon: null });
    timer = window.setTimeout(() => {
      procedure.setOptions(CATALOG);
      procedure.setPlaceholder('Choisir une procédure…');
      procedure.setDisabled(false);
      mode.setDisabled(false);
      mode.setOptions(modeOptions(null), 'sync');
      state.update({ label: `${CATALOG.length} procédures`, tone: 'success', dot: false, icon: 'check' });
    }, 1200);
  }
  load();

  return DemoCard(
    {
      title: 'Mises à jour après création',
      subtitle: 'setPlaceholder · setOptions · setDisabled · options désactivées qui disent pourquoi',
      span: 12,
      actions: [state, Button({ label: 'Recharger', icon: 'refresh-cw', size: 'sm', id: 'kit-dynamic-reload', onClick: load })],
    },
    Grid(
      { gap: 16 },
      h('div.col.col-4.col-md-12', Field({ label: 'Procédure', control: procedure, hint: 'Étiquette par option (badge) ; « check_stock » est désactivée et son info-bulle explique pourquoi.' })),
      h('div.col.col-5.col-md-12', Field({ label: 'Mode d’appel', control: mode, hint: 'Choisir « stream_analytics » : setOptions() verrouille les modes interdits. Le libellé nomme le groupe, il n’active aucun segment.' })),
      h('div.col.col-3.col-md-12', Field({ label: 'Protocoles', control: protocols, hint: 'Groupe de puces : aria-labelledby, pas de <label for>.' })),
    ),
    Usage("select.setPlaceholder('Choisir une procédure…')  ·  options: [{ value, label, badge: { label: 'flux', tone: 'info' }, disabled: true, title: 'Pourquoi…' }]\nsegmented.setOptions([{ value, label, disabled, title }], valeur?)  ·  segmented.setDisabled(true)  ·  Toggle({ label, title })"),
  );
}

function navigation() {
  const stepper = Stepper({
    steps: [
      { title: 'Stub client', description: 'Appel + marshalling' },
      { title: 'Transport', description: 'Trame sur la socket' },
      { title: 'Squelette', description: 'Dispatch + exécution' },
      { title: 'Retour', description: 'Résultat démarshallé' },
    ],
  });
  let step = 2;
  stepper.setStep(step);
  const advance = Button({
    label: 'Étape suivante',
    size: 'sm',
    iconRight: 'arrow-right',
    onClick: () => {
      step = (step + 1) % 5;
      stepper.setStep(step);
    },
  });

  return Section(
    { title: 'Onglets et étapes' },
    Grid(
      DemoCard(
        { title: 'Tabs', subtitle: 'Soulignement animé, compteurs, panneaux créés à la demande', span: 6 },
        Tabs({
          tabs: [
            { id: 'result', label: 'Résultat', icon: 'braces', content: () => h('p.fg-1', 'Le panneau « Résultat » : valeur renvoyée par la procédure, mise en forme.') },
            { id: 'wire', label: 'Sur le fil', icon: 'binary', badge: 4, content: () => h('p.fg-1', 'Le panneau « Sur le fil » : les quatre messages échangés pour cet appel.') },
            { id: 'timeline', label: 'Chronologie', icon: 'timer', badge: 12, content: () => h('p.fg-1', 'Le panneau « Chronologie » : les douze étapes et leur durée.') },
            { id: 'errors', label: 'Erreurs', icon: 'bug', disabled: true },
          ],
          actions: IconButton({ icon: 'maximize-2', label: 'Agrandir', size: 'sm' }),
        }),
        Specimen('Variante pill', Tabs({ variant: 'pill', value: 'p95', tabs: [{ id: 'mean', label: 'Moyenne' }, { id: 'p95', label: 'p95' }, { id: 'p99', label: 'p99' }] })),
        Specimen(
          'Débordement',
          h(
            'div.kit-narrow',
            Tabs({
              value: 'wire',
              tabs: [
                { id: 'result', label: 'Résultat', icon: 'braces' },
                { id: 'wire', label: 'Sur le fil', icon: 'binary', badge: 4 },
                { id: 'timeline', label: 'Chronologie', icon: 'timer', badge: 12 },
                { id: 'code', label: 'Code équivalent', icon: 'code' },
                { id: 'history', label: 'Historique', icon: 'history', badge: 38 },
                { id: 'errors', label: 'Erreurs', icon: 'bug' },
              ],
              actions: [IconButton({ icon: 'scan-search', label: 'Ouvrir dans Sous le capot', size: 'sm' }), IconButton({ icon: 'maximize-2', label: 'Agrandir', size: 'sm' })],
            }),
          ),
        ),
      ),
      DemoCard(
        { title: 'Stepper', subtitle: 'États : en attente, actif, fait, erreur', span: 6, actions: advance },
        stepper,
        h(
          'div.kit-split',
          Stepper({
            orientation: 'vertical',
            steps: [
              { title: 'Premier essai', description: 'TIMEOUT après 500 ms', state: 'error' },
              { title: 'Attente', description: 'Recul exponentiel : 100 ms', state: 'done' },
              { title: 'Deuxième essai', description: 'En cours…', state: 'active' },
              { title: 'Résultat', state: 'pending' },
            ],
          }),
        ),
      ),
    ),
  );
}

/**
 * Sections « contrôles » du guide de style.
 * @returns {HTMLElement[]}
 */
export function controls() {
  return [buttons(), forms(), navigation()];
}
