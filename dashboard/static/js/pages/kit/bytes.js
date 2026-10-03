/**
 * Guide de style — code et octets : vue hexadécimale annotée, tableau des segments, blocs de
 * code colorés, diff de contrat et arbre JSON.
 */

import { h } from '../../core/dom.js';
import { CodeBlock, DiffView } from '../../components/codeblock.js';
import { HexView, SegmentList } from '../../components/hexview.js';
import { JsonTree } from '../../components/jsontree.js';
import { Badge, Button, ButtonGroup, Grid, Section, Segmented } from '../../components/ui.js';
import { DemoCard, Usage } from './demo.js';
import {
  GRPC_PRODUCT,
  GRPC_REQUEST,
  GRPC_REQUEST_READINGS,
  HTTP_REQUEST,
  HTTP_RESPONSE,
  JSONRPC_FRAME,
  JSON_DIFF_ROWS,
  JS_FETCH,
  PROTO_NOTES,
  PROTO_V1,
  PROTO_V2,
  PYTHON_STUB,
  RPC_RESULT,
  SHELL_COMMANDS,
} from './codedata.js';

/** Vue hexadécimale et tableau des segments reliés par leurs survols. */
function linked(track, message, hexProps = {}, listProps = {}) {
  const list = track(SegmentList({ ...message, ...listProps, onHover: (indices) => view.highlight(indices) }));
  const view = track(HexView({ ...message, ...hexProps, onSegmentHover: (_, index) => list.highlight(index) }));
  return { view, list };
}

function bytes(track) {
  // Deux lectures des mêmes octets : le sélecteur de la barre recolore le vidage, le tableau suit.
  const request = linked(track, GRPC_REQUEST, {
    title: 'UpdateStockRequest · gRPC',
    readings: GRPC_REQUEST_READINGS,
    onReadingChange: () => request.list.update({ segments: request.view.segments }),
  });
  const product = linked(track, GRPC_PRODUCT, { title: 'Product · gRPC', maxBytes: 64, legend: false }, { maxHeight: 336 });
  const frame = track(HexView({ ...JSONRPC_FRAME, title: 'update_stock · JSON-RPC maison', maxHeight: 216 }));
  const http = linked(track, HTTP_REQUEST, { title: 'POST /api/stock · REST', maxBytes: 96, legend: false });
  const raw = track(HexView({ hex: GRPC_REQUEST.hex, title: 'Mêmes octets', legend: false }));
  const colouring = Segmented({
    size: 'sm',
    value: 'none',
    ariaLabel: 'Découpage affiché',
    options: [
      { value: 'none', label: 'Brut' },
      { value: 'frame', label: 'Trame' },
      { value: 'fields', label: 'Champs' },
    ],
    onChange: (value) => raw.setSegments(value === 'none' ? [] : GRPC_REQUEST.segments.filter((segment) => value === 'fields' || segment.kind === 'frame')),
  });

  return Section(
    {
      title: 'Octets',
      description:
        'HexView : décalage, octets, gouttière ASCII. Les segments des évènements de trace teintent leurs octets — une teinte par champ, plus soutenue pour le tag et la longueur. SegmentList : le même découpage, champ par champ.',
    },
    Grid(
      DemoCard(
        { title: 'HexView et SegmentList reliés', subtitle: 'update_stock("SKU-1001", -3) : 5 octets de préfixe gRPC, puis 12 de Protobuf · readings : les mêmes octets lus par deux contrats', span: 7, padding: 'none' },
        h('div', { style: { padding: '0 20px' } }, request.view),
        request.list,
      ),
      DemoCard(
        { title: 'Trame JSON-RPC', subtitle: 'Le même appel : 4 octets de longueur, puis le JSON lisible · maxHeight', span: 5 },
        frame,
        Usage('HexView({ hex: event.payload_hex, segments: event.detail.segments, onSegmentHover })'),
      ),
      DemoCard(
        { title: 'Message imbriqué, « afficher la suite »', subtitle: 'Product : champs répétés et sous-messages — survolez une ligne du tableau ou un octet', span: 12, padding: 'none' },
        h('div', { style: { padding: '0 20px' } }, product.view),
        product.list,
      ),
      DemoCard(
        { title: 'Requête HTTP', subtitle: 'Segments header · header · body, vidage tronqué à 96 octets · un libellé long passe sur deux lignes', span: 7, padding: 'none' },
        h('div', { style: { padding: '0 20px' } }, http.view),
        http.list,
      ),
      DemoCard(
        { title: 'Vidage brut', subtitle: 'Sans segments : zéros estompés, octets non imprimables colorés · setSegments() recolore sans recréer', span: 5, actions: colouring },
        raw,
        Usage("view.setSegments(segments)  ·  HexView({ hex, readings: [{ id, label, segments }], onReadingChange })\nSegmentList({ hex, segments, onHover: (indices) => view.highlight(indices) })"),
      ),
    ),
  );
}

function code(track) {
  return Section(
    {
      title: 'Code',
      description: 'CodeBlock : coloration par petit analyseur lexical (JSON, Python, Protobuf, JavaScript, HTTP, shell), numéros de ligne, lignes mises en avant, copie.',
    },
    Grid(
      DemoCard(
        { title: 'Protobuf', subtitle: 'Titre, lignes 6 à 8 mises en avant, hauteur limitée', span: 6 },
        track(CodeBlock({ title: 'rpc_grpc/protos/service.proto', language: 'protobuf', code: PROTO_V1, highlightLines: [[6, 8]], maxHeight: 360 })),
      ),
      DemoCard(
        { title: 'Python et shell', subtitle: 'Sans barre de titre : la copie apparaît au survol', span: 6 },
        track(CodeBlock({ language: 'python', code: PYTHON_STUB })),
        track(CodeBlock({ language: 'shell', code: SHELL_COMMANDS, lineNumbers: false })),
        Usage("CodeBlock({ code, language: 'python', title, highlightLines: [[6, 8]], wrap, maxHeight })"),
      ),
      DemoCard(
        { title: 'HTTP', subtitle: 'Requête telle qu’émise, réponse avec corps JSON', span: 6 },
        track(CodeBlock({ title: 'Requête', language: 'http', code: HTTP_REQUEST.text, lineNumbers: false, wrap: true })),
        track(CodeBlock({ title: 'Réponse', language: 'http', code: HTTP_RESPONSE, lineNumbers: false, wrap: true })),
      ),
      DemoCard(
        { title: 'JavaScript et JSON', subtitle: 'L’appel REST écrit à la main, puis la requête JSON-RPC équivalente', span: 6 },
        track(CodeBlock({ language: 'javascript', code: JS_FETCH })),
        track(CodeBlock({ title: 'Requête JSON-RPC 2.0', language: 'json', code: JSON.stringify(JSONRPC_FRAME.message, null, 2), lineNumbers: false })),
      ),
      DemoCard(
        { title: 'Retour à la ligne et ton de mise en avant', subtitle: 'wrap : la suite d’une ligne repliée reprend sous son indentation (hangingIndent) · highlightTone : un ton ou une couleur de protocole', span: 12 },
        h(
          'div.kit-code-row',
          track(CodeBlock({ title: 'Stub maison', language: 'python', code: PYTHON_STUB, wrap: true, highlightLines: [7], highlightTone: 'success' })),
          track(CodeBlock({ title: 'REST à la main', language: 'javascript', code: JS_FETCH, wrap: true, hangingIndent: 4, highlightLines: [[1, 5]], highlightTone: 'var(--proto-rest)' })),
          track(CodeBlock({ title: 'Échec', language: 'python', code: PYTHON_STUB, wrap: true, highlightLines: [[9, 11]], highlightTone: 'danger' })),
        ),
        Usage("CodeBlock({ code, wrap: true, hangingIndent: 2, highlightLines: [7], highlightTone: 'success' })   // ou highlightTone: 'var(--proto-rest)'"),
      ),
    ),
  );
}

function diffs(track) {
  const v1 = { product_id: 'SKU-1001', name: 'Clavier mécanique 75 %', price: 89.9, stock: 117 };
  const v2 = { product_id: 'SKU-1001', name: 'Clavier mécanique 75 %', price_cents: 8990, quantity: 117, algorithm: 'iterative' };

  // onRender, highlightLine, scrollToLine : parcourir les ruptures annotées sans toucher au DOM du diff.
  const shown = Badge({ label: '', size: 'sm', mono: true });
  const contract = track(
    DiffView({
      left: PROTO_V1,
      right: PROTO_V2,
      language: 'protobuf',
      leftTitle: 'service.proto · v1',
      rightTitle: 'service_v2.proto · v2',
      annotations: PROTO_NOTES,
      context: 2,
      maxHeight: 440,
      onRender: (el) => shown.update({ label: `${el.querySelectorAll('tr.diff__row').length} lignes affichées` }),
    }),
  );
  const breaking = PROTO_NOTES.filter((note) => note.label === 'BREAKING');
  let cursor = -1;
  const nextBreak = Button({
    label: 'Rupture suivante',
    size: 'sm',
    iconRight: 'arrow-down',
    id: 'kit-diff-next',
    onClick: () => {
      cursor = (cursor + 1) % breaking.length;
      contract.highlightLine(breaking[cursor].line, 'right');
      contract.scrollToLine(breaking[cursor].line, 'right');
    },
  });
  const reset = Button({
    label: 'Effacer',
    size: 'sm',
    variant: 'ghost',
    onClick: () => {
      cursor = -1;
      contract.highlightLine(null);
    },
  });

  return Section(
    {
      title: 'Diff',
      description: 'DiffView : diff ligne à ligne (plus longue sous-séquence commune), mots modifiés surlignés, zones identiques repliables, annotations par ligne.',
    },
    Grid(
      DemoCard(
        { title: 'Contrat v1 → v2', subtitle: 'Chaque rupture est annotée sur la ligne qui la provoque · highlightLine() et scrollToLine() la mettent en avant', span: 12, padding: 'sm', actions: [shown, reset, nextBreak] },
        contract,
      ),
      DemoCard(
        { title: 'Mode unifié', subtitle: 'Résultat de get_product_details vu par un client v1 puis v2 · lignes de diff fournies par le serveur (rows)', span: 7 },
        track(
          DiffView({
            left: JSON.stringify(v1, null, 2),
            right: JSON.stringify(v2, null, 2),
            language: 'json',
            leftTitle: 'réponse v1',
            rightTitle: 'réponse v2',
            mode: 'unified',
            rows: JSON_DIFF_ROWS,
            annotations: [{ line: 5, side: 'right', tone: 'danger', label: 'KeyError', text: 'Le code client lit product["stock"] : la clé n’existe plus.' }],
          }),
        ),
      ),
      DemoCard(
        { title: 'Usage', span: 5 },
        Usage("DiffView({\n  left, right, language: 'protobuf',\n  leftTitle, rightTitle,\n  mode: 'split',   // ou 'unified'\n  context: 2,      // replie les zones identiques\n  rows,            // diff déjà calculé par le serveur\n  annotations: [\n    { line: 31, side: 'right', tone: 'danger',\n      label: 'BREAKING', text: '…' },\n  ],\n  onRender: (el) => { … },\n})\n  →  .highlightLine(31, 'right') · .scrollToLine(31, 'right')"),
      ),
    ),
  );
}

function trees(track) {
  const tree = track(JsonTree(RPC_RESULT, { collapsedDepth: 3, rootLabel: 'réponse', maxString: 36 }));
  const controls = ButtonGroup(
    Button({ label: 'Tout déplier', size: 'sm', icon: 'maximize-2', onClick: () => tree.expandAll() }),
    Button({ label: 'Tout replier', size: 'sm', icon: 'minimize-2', onClick: () => tree.collapseAll() }),
  );
  return Section(
    { title: 'Arbre JSON', description: 'JsonTree : nœuds repliables, couleurs par type, nombre d’éléments, longues chaînes tronquées, copie d’un sous-arbre au survol.' },
    Grid(
      DemoCard({ title: 'JsonTree', subtitle: 'Double-clic sur une ligne pour la replier', span: 7, actions: controls }, tree),
      DemoCard(
        { title: 'Valeurs simples et tableaux', span: 5 },
        track(JsonTree([1, 2, 3, 5, 8, 13, 21], { collapsedDepth: 1, rootLabel: 'fibonacci' })),
        track(JsonTree({ ok: false, error: { code: 'TIMEOUT', message: 'Aucune réponse en 5,00 s', retryable: true, attempts: 3, cause: null } }, { collapsedDepth: 3 })),
        Usage("JsonTree(value, { collapsedDepth: 2, rootLabel: 'result' })  →  .update(value) · .expandAll() · .collapseAll()"),
      ),
    ),
  );
}

/**
 * Sections « code et octets » du guide de style.
 * @param {Array<() => void>} cleanups Reçoit le `destroy` de chaque composant créé.
 * @returns {HTMLElement[]}
 */
export function bytesAndCode(cleanups) {
  const track = (component) => {
    cleanups.push(() => component.destroy());
    return component;
  };
  return [bytes(track), code(track), diffs(track), trees(track)];
}
