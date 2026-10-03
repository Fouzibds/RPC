/**
 * Bilan — contenu éditorial : les explications qui ne dépendent d'aucune mesure.
 * Aucun nombre mesuré ici : les chiffres affichés à côté de ces textes viennent toujours de
 * `GET /api/summary` (voir `data.js`), jamais de ce fichier.
 */

/**
 * @typedef {'full'|'half'|'empty'|'dot'|'na'} Mark
 * `full` point fort · `half` mitigé · `empty` point faible · `dot` simple description · `na` sans objet.
 */

/**
 * Lecture qualitative de la matrice, par critère : une pastille par colonne, dans l'ordre
 * local, JSON-RPC maison, gRPC, REST. Un critère absent de cette table reste descriptif.
 * @type {Readonly<Record<string, Mark[]>>}
 */
export const RATINGS = Object.freeze({
  'Contrat': ['full', 'empty', 'full', 'half'],
  'Format des messages': ['na', 'dot', 'dot', 'dot'],
  'Transport': ['na', 'dot', 'dot', 'dot'],
  'Appels simultanés': ['dot', 'full', 'full', 'empty'],
  'Flux (streaming)': ['full', 'half', 'full', 'half'],
  'Typage': ['full', 'half', 'full', 'empty'],
  'Erreurs': ['dot', 'dot', 'dot', 'dot'],
  'Échéance (timeout)': ['na', 'half', 'full', 'half'],
  'Lisibilité des messages': ['na', 'full', 'empty', 'full'],
  'Depuis un navigateur': ['na', 'empty', 'half', 'full'],
  'Outillage': ['full', 'empty', 'half', 'full'],
  'Couplage au contrat': ['dot', 'empty', 'half', 'full'],
});

/** Légende des pastilles, dans l'ordre d'affichage. */
export const MARK_LABELS = Object.freeze({
  full: 'Point fort',
  half: 'Mitigé',
  empty: 'Point faible',
  dot: 'Descriptif',
  na: 'Sans objet',
});

/**
 * « Quand choisir quoi » : une situation, un choix, sa raison. `proof` désigne la donnée du bilan
 * qui l'appuie — un argument (`item`) ou une case de la matrice (`criterion` + `protocol`).
 */
export const DECISIONS = Object.freeze([
  {
    id: 'internal',
    icon: 'workflow',
    situation: 'Appels entre services internes',
    protocol: 'grpc',
    reasoning:
      'Vous maîtrisez les deux extrémités : l’IDL verrouille les types, stub et squelette sont générés, l’échéance se propage jusqu’au serveur et les flux vont dans les deux sens.',
    proof: { item: 'compact_messages' },
  },
  {
    id: 'public',
    icon: 'globe',
    situation: 'API publique, navigateurs et tiers',
    protocol: 'rest',
    reasoning:
      'Vous ne maîtrisez pas les clients : HTTP et JSON se lisent partout, fetch suffit, caches et proxys du web fonctionnent, et un champ inconnu est ignoré plutôt que fatal.',
    proof: { criterion: 'Depuis un navigateur', protocol: 'rest' },
  },
  {
    id: 'learning',
    icon: 'flask-conical',
    situation: 'Apprendre, prototyper',
    protocol: 'custom',
    reasoning:
      'Peu de code suffit pour voir stub, tramage et squelette à l’œuvre, et chaque message se lit à l’œil nu. À réserver à l’étude : ni contrat, ni garde-fou.',
    proof: { item: 'location_transparency' },
  },
  {
    id: 'avoid',
    icon: 'ban',
    situation: 'Quand ne pas utiliser de RPC',
    protocol: null,
    verdict: 'Appel local ou file de messages',
    reasoning:
      'Dans un même processus, un appel de fonction suffit. Pour un travail long, différé ou diffusé à plusieurs destinataires, une file de messages évite de lier la disponibilité de l’appelant à celle de l’appelé.',
    proof: { item: 'remote_cost' },
  },
]);

/**
 * Règles tirées du laboratoire de pannes. `item` est l'argument du bilan qui en porte la preuve ;
 * `page` et `scenario` disent où la rejouer.
 */
export const RULES = Object.freeze([
  {
    id: 'deadline',
    title: 'Toujours poser une échéance',
    text: 'Sans elle, un pic de latence ou un trou noir bloque l’appelant aussi longtemps que dure la panne.',
    item: 'unknown_outcome',
    page: 'chaos',
    scenario: 'timeout_spike',
  },
  {
    id: 'backoff',
    title: 'Réessayer avec un délai croissant, et seulement si c’est sûr',
    text: 'Une nouvelle tentative immédiate aggrave la panne ; rejouer une écriture sans précaution peut l’appliquer deux fois.',
    item: 'partial_failure',
    page: 'chaos',
    scenario: 'connection_cut',
  },
  {
    id: 'idempotency',
    title: 'Une clé d’idempotence pour chaque écriture',
    text: 'Le serveur reconnaît le rejeu et n’exécute l’opération qu’une fois, quel que soit le nombre de tentatives.',
    item: 'duplicate_execution',
    page: 'chaos',
    scenario: 'duplicate_execution',
  },
  {
    id: 'breaker',
    title: 'Un disjoncteur devant chaque dépendance',
    text: 'Quand le serveur est en panne, échouer tout de suite vaut mieux qu’épuiser ses tentatives à chaque appel.',
    item: 'outage',
    page: 'chaos',
    scenario: 'server_outage',
  },
  {
    id: 'coarse',
    title: 'Des interfaces à gros grain, pas de boucles bavardes',
    text: 'Un appel qui rapporte N résultats plutôt que N allers-retours : la latence se paie une fois, pas N fois.',
    item: 'latency_trap',
    page: 'chaos',
    scenario: 'latency_trap',
  },
  {
    id: 'evolution',
    title: 'Faire évoluer le contrat sans réutiliser de numéro',
    text: 'Ajouter des champs sous de nouveaux numéros ; ne jamais renommer, retyper ni recycler ce que des clients déployés lisent encore.',
    item: 'contract_coupling',
    page: 'contract',
    scenario: null,
  },
]);

/** Les huit illusions de l'informatique répartie (L. Peter Deutsch, puis James Gosling), et ce qui les contredit ici. */
export const FALLACIES = Object.freeze([
  { claim: 'Le réseau est fiable', reality: 'Coupures, réponses perdues, serveur arrêté : les scénarios de pannes.', page: 'chaos' },
  { claim: 'La latence est nulle', reality: 'Chaque aller-retour se paie : le piège de la boucle innocente.', page: 'chaos' },
  { claim: 'La bande passante est infinie', reality: 'La taille des messages compte : JSON face à Protobuf.', page: 'benchmark' },
  { claim: 'Le réseau est sûr', reality: 'Hors du périmètre de ce laboratoire : chiffrement et authentification restent à ajouter.', page: null },
  { claim: 'La topologie ne change pas', reality: 'Adresses et ports bougent : le proxy s’intercale sans que le client change.', page: 'overview' },
  { claim: 'Il n’y a qu’un administrateur', reality: 'Client et serveur évoluent séparément : d’où les ruptures de contrat.', page: 'contract' },
  { claim: 'Le coût du transport est nul', reality: 'Sérialiser, copier, désérialiser : du temps processeur à chaque appel.', page: 'xray' },
  { claim: 'Le réseau est homogène', reality: 'Langages et versions diffèrent : c’est le rôle de l’IDL et du format de fil.', page: 'contract' },
]);

/** Glossaire, dans l'ordre du trajet d'un appel puis des notions de robustesse. */
export const GLOSSARY = Object.freeze([
  {
    term: 'Stub',
    alias: 'souche cliente',
    definition: 'Objet local qui présente l’interface du service distant : il sérialise l’appel, l’envoie, attend la réponse et la rend comme le ferait une fonction ordinaire.',
  },
  {
    term: 'Squelette',
    alias: 'skeleton, dispatcher',
    definition: 'Pendant du stub côté serveur : il reçoit le message, retrouve la procédure visée, l’exécute et renvoie son résultat ou son erreur.',
  },
  {
    term: 'Marshalling',
    alias: 'sérialisation',
    definition: 'Transformation des arguments et des résultats en une suite d’octets transportable ; l’opération inverse est le démarshalling.',
  },
  {
    term: 'Tramage',
    alias: 'framing',
    definition: 'Délimitation des messages dans un flux d’octets continu : ici, un préfixe de longueur placé devant chaque message.',
  },
  {
    term: 'IDL',
    alias: 'Interface Definition Language',
    definition: 'Langage neutre qui décrit procédures et messages ; le code du client et celui du serveur en sont générés.',
  },
  {
    term: 'Protobuf',
    alias: 'Protocol Buffers',
    definition: 'Format binaire où chaque champ est désigné par un numéro et les entiers codés sur une longueur variable ; c’est aussi l’IDL de gRPC.',
  },
  {
    term: 'Idempotence',
    alias: null,
    definition: 'Propriété d’une opération dont la répétition ne change pas le résultat : lire un stock l’est, le décrémenter ne l’est pas.',
  },
  {
    term: 'Échéance',
    alias: 'deadline, timeout',
    definition: 'Durée au-delà de laquelle l’appelant renonce. gRPC la transmet au serveur, qui peut alors abandonner un travail devenu inutile.',
  },
  {
    term: 'Disjoncteur',
    alias: 'circuit breaker',
    definition: 'Garde-fou qui, après une série d’échecs, refuse les appels sans toucher au réseau, puis teste prudemment le retour du service.',
  },
  {
    term: 'Transparence de localisation',
    alias: null,
    definition: 'Appeler une procédure sans savoir où elle s’exécute. Commode pour écrire le code, trompeur pour raisonner sur ses pannes.',
  },
]);

/** Les quatre phases du cahier des charges : où elles vivent dans le code, dans l'application et en ligne de commande. */
export const PHASES = Object.freeze([
  {
    number: 1,
    title: 'RPC fait maison',
    requirement: 'Stub, marshalling, transport et squelette écrits à la main, sur sockets TCP et JSON-RPC 2.0.',
    code: ['rpc_custom/protocol.py', 'rpc_custom/client_stub.py', 'rpc_custom/server_skeleton.py'],
    pages: ['console', 'xray'],
    cli: ['python main.py --demo', 'python main.py --call get_product_details SKU-1001 --inspect'],
  },
  {
    number: 2,
    title: 'gRPC et Protobuf',
    requirement: 'Le même service derrière un contrat IDL : code généré, messages binaires, quatre types d’appels.',
    code: ['rpc_grpc/protos/service.proto', 'rpc_grpc/grpc_server.py', 'rpc_grpc/grpc_client.py'],
    pages: ['console', 'xray', 'contract'],
    cli: ['python main.py --call get_product_details SKU-1001 --protocol grpc'],
  },
  {
    number: 3,
    title: 'Avantages et inconvénients, mesurés',
    requirement: 'Tailles et temps comparés, pannes simulées, ruptures de contrat, transparence de localisation.',
    code: ['benchmark_lab/benchmark_perf.py', 'benchmark_lab/failure_simulation.py', 'benchmark_lab/contract_evolution.py', 'benchmark_lab/transparency_demo.py'],
    pages: ['benchmark', 'chaos', 'contract', 'compare'],
    cli: ['python main.py --benchmark', 'python main.py --simulate-failures', 'python main.py --contract'],
  },
  {
    number: 4,
    title: 'Interface de démonstration',
    requirement: 'Un point d’entrée unique : menu interactif en console, options directes et ce tableau de bord.',
    code: ['main.py', 'cli/cli_runner.py', 'dashboard/server.py'],
    pages: ['overview', 'learn'],
    cli: ['python main.py', 'python main.py --dashboard'],
  },
]);
