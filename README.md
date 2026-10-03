# RPC Explorer & Benchmark Lab

Démonstrateur pédagogique du **Remote Procedure Call** : le même service métier
(calcul et gestion d'inventaire) est exposé par un middleware RPC écrit à la main,
par gRPC et par une API REST de référence, puis mesuré, mis en panne et confronté à
une évolution de contrat. Tout s'observe depuis la ligne de commande ou depuis un
dashboard web.

## Ce que le projet démontre

1. **Comment fonctionne un RPC.** Un stub client, le marshalling, le transport, le
   squelette serveur et son dispatcher, le démarshalling : chaque étape d'un appel est
   tracée, chronométrée et affichée avec les octets réellement échangés.
2. **Deux façons de le construire.** Un RPC « fait maison » (sockets TCP + JSON-RPC 2.0)
   et son équivalent industriel, gRPC (contrat IDL Protobuf, binaire, HTTP/2) — avec
   REST/JSON comme point de repère.
3. **Ce que cela apporte, et ce que cela coûte.**
   - Avantages : transparence de localisation (le code appelant ne change pas),
     messages compacts, contrat typé, multiplexage.
   - Inconvénients : le « piège de la transparence » — latence, échéances, pannes
     partielles, doublons — et le couplage fort au contrat (*breaking changes*).

Rien n'est affirmé sans mesure : chaque conclusion affichée est calculée à partir des
appels que le laboratoire vient d'exécuter sur votre machine.

## Démarrage rapide

Prérequis : Python 3.11 ou plus récent. Les commandes ci-dessous sont celles de
PowerShell ; sous Linux ou macOS, seule l'activation de l'environnement change
(`source .venv/bin/activate`).

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m rpc_grpc.generate          # compile les contrats .proto (stubs et squelettes gRPC)
python main.py                       # menu interactif
```

Les trois commandes qui répondent directement au sujet :

```powershell
python main.py --benchmark           # tableaux comparatifs : octets et millisecondes
python main.py --simulate-failures   # un appel distant n'est pas un appel local
python main.py --dashboard           # le même laboratoire, dans le navigateur
```

`python main.py --benchmark --quick` donne les mêmes tableaux en quelques secondes
(la version complète dure une trentaine de secondes).

## Ligne de commande

Chaque mode démarre le laboratoire complet (cinq serveurs, trois proxys), s'exécute,
puis arrête tout.

| Commande | Rôle |
|---|---|
| `python main.py` | Menu interactif : appels synchrones, asynchrones et en flux, mode « sous le capot », visite guidée, banc d'essai, pannes, contrat, transparence, état des serveurs. |
| `python main.py --benchmark` | Banc d'essai complet, puis rapport JSON et Markdown dans `reports/`. |
| `python main.py --simulate-failures` | Les cinq scénarios de panne, chronologie en direct et verdict chiffré. |
| `python main.py --contract` | Diff des deux contrats `.proto`, puis un client v1 face aux serveurs v2. |
| `python main.py --demo` | Visite guidée : le même appel en JSON-RPC maison, gRPC puis REST, octet par octet. |
| `python main.py --dashboard` | Dashboard web (FastAPI + WebSocket). |
| `python main.py --serve` | Tous les serveurs au premier plan, jusqu'à Ctrl+C. |
| `python main.py --call METHOD [ARG ...]` | Un appel unitaire et son résultat. |

Options complémentaires :

| Option | Mode | Effet |
|---|---|---|
| `--iterations N` | `--benchmark` | Nombre d'appels chronométrés par protocole (1 000 par défaut). |
| `--quick` | `--benchmark` | Version courte : moins d'appels, balayage réseau réduit. |
| `--no-save` | `--benchmark` | N'enregistre pas le rapport. |
| `--scenario ID` | `--simulate-failures` | `latency_trap`, `timeout_spike`, `connection_cut`, `server_outage`, `duplicate_execution` ou `all` (par défaut). |
| `--protocol P` | `--simulate-failures`, `--call` | `custom` (par défaut), `grpc`, `rest` ; `local` pour `--call` seulement. |
| `--no-pause` | `--demo` | Déroule la visite sans attendre la touche Entrée. |
| `--port N` | `--dashboard` | Port HTTP du dashboard (8000 par défaut). |
| `--no-browser` | `--dashboard` | N'ouvre pas le navigateur. |
| `--inspect` | `--call` | Ajoute le pipeline de l'appel et les octets échangés. |
| `--via-proxy` | `--call` | Fait passer l'appel par le proxy de chaos. |
| `--timeout S` | `--call` | Échéance de l'appel, en secondes. |
| `--version`, `--help` | — | Version ; aide complète. |

Exemples d'appels unitaires :

```powershell
python main.py --call calculate_factorial 20 --protocol local
python main.py --call update_stock SKU-1001 -3 --protocol grpc --inspect
python main.py --call list_products limit=3 category=Audio --protocol rest
python main.py --call stream_analytics 5 100
python main.py --call check_stock --protocol grpc
```

Les arguments suivent l'ordre du catalogue (`common/catalog.py`) ; un argument omis
prend sa valeur par défaut, et la forme `nom=valeur` est acceptée.

Codes de sortie : `0` succès · `1` échec (port occupé, scénario interrompu, issue de
contrat inattendue) · `2` erreur d'utilisation ou erreur RPC renvoyée par `--call` ·
`130` interruption au clavier.

Chaque brique se lance aussi seule :

```powershell
python -m rpc_custom.custom_rpc_demo         # Phase 1 : le RPC maison, messages bruts à l'appui
python -m rpc_grpc.grpc_server               # Phase 2 : le serveur gRPC…
python -m rpc_grpc.grpc_client               # …et son client (dans un second terminal)
python -m benchmark_lab.benchmark_perf --quick
python -m benchmark_lab.failure_simulation --scenario duplicate_execution --protocol grpc
python -m benchmark_lab.contract_evolution --diff
python -m benchmark_lab.transparency_demo
```

## Architecture

```
                         ┌──────────────────────────── processus « laboratoire » ───────────────────────────┐
  CLI Rich (main.py) ───▶│                                                                                  │
  Dashboard web  ───────▶│  LabRuntime (lab/runtime.py)                                                     │
   (FastAPI + WS)        │   ├─ InventoryService (common/inventory.py) ← la « procédure distante »          │
                         │   ├─ Serveur JSON-RPC maison :9101 ◀── ChaosProxy :9201 ◀── stub maison          │
                         │   ├─ Serveur gRPC            :50051 ◀── ChaosProxy :50151 ◀── stub gRPC généré   │
                         │   ├─ Serveur REST            :8081  ◀── ChaosProxy :8181  ◀── client HTTP        │
                         │   ├─ Serveurs « contrat v2 » :9102 (JSON-RPC) et :50052 (gRPC)                   │
                         │   ├─ NetworkConditions (partagées par les 3 proxys)                              │
                         │   └─ EventBus + TraceCollector (common/telemetry.py) ← « sous le capot »         │
                         └──────────────────────────────────────────────────────────────────────────────────┘
```

Trois choix rendent la comparaison équitable :

- **un seul service métier** derrière les trois serveurs : seule la couche de
  communication change ;
- **une seule interface cliente** (`InventoryClient`) : que l'on parle à l'objet local,
  au stub maison, à gRPC ou à REST, le code appelant est identique et reçoit les mêmes
  dictionnaires et les mêmes erreurs ;
- **un seul réseau simulé** : chaque client atteint son serveur en direct (boucle
  locale idéale) ou à travers un proxy de chaos, vraie connexion TCP sur laquelle on
  injecte latence, pics, coupures, trou noir ou panne. Les trois protocoles subissent
  exactement les mêmes conditions.

Le pipeline d'un appel, tel que le tracent les trois middlewares :

```
client.call → client.marshal → client.send ─── réseau ───▶ server.receive → server.unmarshal
                                                            → server.dispatch → server.execute
client.return ← client.unmarshal ← client.receive ◀─ réseau ─── server.send ← server.marshal
```

Ports par défaut : dashboard 8000 · JSON-RPC 9101 (proxy 9201, v2 9102) · gRPC 50051
(proxy 50151, v2 50052) · REST 8081 (proxy 8181). Si l'un d'eux est occupé, le
programme le dit et s'arrête ; la variable d'environnement `RPCX_PORT_OFFSET` décale
alors tous les ports d'un bloc :

```powershell
$env:RPCX_PORT_OFFSET = "200"; python main.py --dashboard
```

Pour faire cohabiter deux laboratoires, décalez-les de 200 et non de 100 : chaque proxy
écoute 100 ports au-dessus de son serveur.

## Ce qu'il faut observer

Les valeurs citées ici ont été relevées sur la machine de développement (Windows 11,
Python 3.11, boucle locale). Les tailles sont déterministes ; les durées varient d'une
machine à l'autre, pas leur ordre de grandeur.

### Banc d'essai — `python main.py --benchmark`

- **Tableau « Taille du paquet (octets) : JSON vs Protobuf ».** La requête
  `update_stock` pèse 128 octets en JSON-RPC et 12 en Protobuf ; une fiche produit,
  535 contre 268 (−50 %). JSON répète le nom de chaque champ, Protobuf n'envoie qu'un
  numéro et un type de fil. L'écart se maintient sur 1 000 produits (−46 %).
- **Octets réels sur le fil.** Comptés par les proxys, trames HTTP/2 et en-têtes
  compris : environ 400 octets par appel pour gRPC, 650 pour JSON-RPC, 700 pour REST.
- **Tableau « Temps moyen par appel (ms) : Local vs RPC maison vs gRPC vs REST ».** Un
  appel local dure une dizaine de microsecondes ; un appel distant, même en boucle
  locale, dix à trente fois plus (0,15 à 0,45 ms). Sur une boucle locale et dans un
  seul interpréteur Python, gRPC n'est pas le plus rapide : son cœur HTTP/2 coûte plus
  que quelques octets économisés. Le rapport le dit tel quel — ses atouts mesurés sont
  la taille des messages et le contrat.
- **Local vs distant.** Dès que le réseau ajoute 50 ms, les trois protocoles se
  confondent (≈ 51 ms par appel) et l'appel local reste à 0,01 ms : la latence du
  réseau écrase le choix du middleware.

### Laboratoire de pannes — `python main.py --simulate-failures`

| Scénario | Ce que l'on voit |
|---|---|
| `latency_trap` | Une boucle de 12 lectures : 0,3 ms en local, 2,4 s à travers 200 ms de latence. Un seul appel groupé rapporte la même chose en 0,2 s. |
| `timeout_spike` | Sans échéance, l'appelant reste bloqué 1,5 s ; avec 300 ms d'échéance il est libéré — sans savoir que le serveur a tout de même exécuté l'appel. |
| `connection_cut` | Le client « naïf » reçoit une erreur réseau qu'aucun appel local ne peut produire ; le client résilient attend, réessaie et aboutit. |
| `server_outage` | Les nouvelles tentatives s'épuisent, le disjoncteur s'ouvre et refuse les appels suivants en quelques microsecondes, puis se referme au retour du serveur. |
| `duplicate_execution` | La réponse d'un `update_stock` se perd, le client réessaie : le stock est débité deux fois. Avec une clé d'idempotence, une seule. |

Chaque scénario se joue sur les trois middlewares (`--protocol custom|grpc|rest`) et
remet le réseau, les stocks et les proxys dans l'état où il les a trouvés.

### Laboratoire de contrat — `python main.py --contract`

Le serveur passe au contrat v2 (`rpc_grpc/protos/service_v2.proto`), les clients
restent en v1. Neuf scénarios, quatre issues possibles :

- **compatible** — un champ ajouté sous un nouveau numéro est ignoré par l'ancien client ;
- **rejet** — RPC renommée (`UNIMPLEMENTED`), paramètre devenu obligatoire (`-32602`) ;
- **plantage** — une clé de résultat renommée lève `KeyError` dans le code du client,
  après un appel réussi ;
- **corruption silencieuse** — un numéro de champ réutilisé avec un autre type : le
  serveur lit `delta = 0`, répond OK, et le stock n'a pas bougé. Aucune erreur, d'aucun
  côté : c'est le cas le plus dangereux, et celui qu'un IDL bien tenu évite.

## Le dashboard

`python main.py --dashboard` ouvre `http://127.0.0.1:8000`. L'interface fonctionne hors
ligne (aucune dépendance externe, polices embarquées), en thème sombre ou clair.

| Page | Contenu |
|---|---|
| Vue d'ensemble | État des serveurs, compteurs et débit en direct, architecture animée. |
| Console RPC | Choisir protocole, procédure et paramètres ; appels synchrones, asynchrones (N en parallèle) ou en flux ; durées, tailles, historique. |
| Sous le capot | Pipeline animé du stub au squelette, chronologie réelle des étapes, octets bruts colorés, décodage Protobuf champ par champ, comparaison des trois protocoles. |
| Benchmark | Configuration, progression en direct, graphiques (tailles, distribution des latences, percentiles, débit, local vs distant), export. |
| Chaos réseau | Conditions réseau (curseurs et préréglages), politique du client (échéance, retries, disjoncteur, idempotence), scénarios guidés. |
| Contrat & IDL | Diff `.proto` v1 ↔ v2, scénarios de rupture, verdict par scénario. |
| Transparence | Le même appel écrit de quatre façons, côte à côte, exécutable. |
| Bilan | Avantages et inconvénients, chiffrés par les mesures du laboratoire. |

L'interface ne contient aucune logique RPC : elle interroge l'API HTTP (`/api/...`) et
écoute le flux temps réel (`/ws`) servis par `dashboard/server.py`, qui délèguent au
même laboratoire que la ligne de commande. La liste des routes figure au §13 de
`implementation_plan.md`.

## Correspondance avec les phases du sujet

| Phase | Ce qui était demandé | Où le trouver |
|---|---|---|
| 1 — RPC « from scratch » | Stub, marshalling, transport par sockets, squelette et dispatcher | `rpc_custom/` : `protocol.py` (JSON-RPC 2.0 et tramage), `client_stub.py` (proxy dynamique, multiplexage, échéances), `server_skeleton.py` (dispatcher, liaison des arguments), `custom_rpc_demo.py` |
| 2 — gRPC et Protobuf | Contrat IDL, code généré, serveur et client | `rpc_grpc/` : `protos/service.proto`, `generate.py`, `grpc_server.py`, `grpc_client.py` — appels unaires et les trois formes de flux ; `wire_inspector.py` décode le format de fil |
| 3 — Avantages et inconvénients | Mesures comparatives, pannes, couplage au contrat | `benchmark_lab/` : `benchmark_perf.py`, `failure_simulation.py`, `contract_evolution.py`, `transparency_demo.py` ; le réseau simulé et la résilience sont dans `netsim/` |
| 4 — Interface | Point d'entrée, menu, options | `main.py`, `cli/` ; en complément, `dashboard/` |

## Carte du dépôt

```
RPC/
├── main.py                  point d'entrée : menu interactif et options
├── requirements.txt
├── implementation_plan.md   plan et contrat d'interfaces entre modules
├── common/                  socle partagé, sans réseau
│   ├── config.py              hôte, ports, constantes
│   ├── errors.py              erreurs métier (serveur) et erreurs d'appel (client), codes canoniques
│   ├── inventory.py           InventoryService : la logique métier, la « procédure distante »
│   ├── catalog.py             description des sept procédures
│   ├── client_api.py          InventoryClient : l'interface commune aux quatre « protocoles »
│   └── telemetry.py           évènements de trace, bus, regroupement par appel
├── rpc_custom/              Phase 1 — RPC fait maison (sockets + JSON-RPC 2.0)
├── rpc_grpc/                Phase 2 — gRPC et Protobuf (contrats v1 et v2, code généré)
├── rest_api/                API REST de référence (HTTP/1.1 + JSON)
├── netsim/                  réseau simulé (proxy de chaos) et résilience (retries, disjoncteur)
├── lab/runtime.py           LabRuntime : démarre tout, fabrique les clients
├── benchmark_lab/           Phase 3 — banc d'essai, pannes, contrat, transparence, rapports
├── cli/                     Phase 4 — interface console (Rich)
├── dashboard/               API HTTP + WebSocket (FastAPI) et interface web (static/)
├── tests/                   suite pytest
├── tools/shot.py            capture d'écran du dashboard (outil de développement)
└── reports/                 rapports générés par le banc d'essai
```

## Tests

```powershell
python -m pytest
```

Environ 900 tests en une minute. Ils n'utilisent que des ports éphémères et un bus de
traces privé : la suite peut tourner pendant qu'un laboratoire est ouvert. Seul
`tests/test_cli.py` lance le vrai programme, sur les ports décalés de 400.

| Fichier | Ce qu'il vérifie |
|---|---|
| `test_rpc_custom.py` | Conformité JSON-RPC 2.0, tramage, lots, flux, erreurs, multiplexage, échéances |
| `test_rpc_grpc.py`, `test_wire_inspector.py` | Appels unaires et trois formes de flux, statuts, traces ; décodeur du format de fil Protobuf |
| `test_rest_api.py` | Routes, statuts, flux NDJSON, connexion persistante |
| `test_netsim.py` | Latence, pics, coupures, trou noir, panne ; retries, disjoncteur, idempotence |
| `test_runtime.py` | Cycle de vie du laboratoire, ports, clients, remise à zéro |
| `test_integration.py` | Parité des quatre protocoles (mêmes résultats, mêmes erreurs), cohérence des traces |
| `test_benchmark.py`, `test_failures.py`, `test_contract.py` | Mesures, scénarios de panne, scénarios de contrat |
| `test_cli.py`, `test_dashboard_api.py` | Ligne de commande ; API HTTP et WebSocket du dashboard |

## En cas de difficulté

- **« Le laboratoire n'a pas pu démarrer »** : un port est occupé. Fermez l'autre
  instance, ou décalez les ports avec `RPCX_PORT_OFFSET` (voir plus haut).
- **Un contrat `.proto` a été modifié** : relancez `python -m rpc_grpc.generate`
  (`main.py` le fait de lui-même quand le code généré est plus ancien que le contrat).
- **Accents ou cadres illisibles dans une vieille console Windows** : le programme
  force l'UTF-8 ; utilisez Windows Terminal ou PowerShell 7 pour un rendu complet.
- **Durée du premier appel** : un appel unitaire (`--call`) ouvre sa connexion, ce que
  la sortie signale par « connexion comprise ». Les durées comparables sont celles du
  banc d'essai, mesurées sur des connexions déjà ouvertes.
