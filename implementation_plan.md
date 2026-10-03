# RPC Explorer & Benchmark Lab — Plan d'implémentation

> Démonstrateur interactif et pédagogique du **Remote Procedure Call** : un middleware
> JSON-RPC écrit à la main (sockets), son équivalent moderne gRPC/Protobuf, une API REST
> de référence, un banc d'essai, un laboratoire de pannes réseau et deux interfaces
> (CLI Rich + dashboard web).

Ce document est à la fois la feuille de route et le **contrat d'interfaces** entre modules.
Ce qui est décrit ici fait foi ; tout écart doit être signalé.

---

## 1. Objectifs (rappel du cahier des charges)

1. **Comprendre** les rouages internes du RPC : stub client, marshalling, transport,
   squelette/dispatcher, démarshalling.
2. **Comparer** deux approches : RPC « from scratch » (sockets + JSON-RPC 2.0) et gRPC
   (contrat IDL Protobuf, binaire) — avec REST/JSON comme point de repère.
3. **Démontrer expérimentalement** avantages et inconvénients : transparence de
   localisation, performance et typage, puis le « piège de la transparence » (latence,
   pannes, timeouts, retries) et le couplage fort au contrat (breaking changes).

Domaine simulé : un **service de calcul & gestion d'inventaire distribué**
(`calculate_factorial`, `get_product_details`, `update_stock`, `list_products`,
`stream_analytics`, + `bulk_update_stock` et `check_stock` pour les flux gRPC).

## 2. Architecture

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

Chaque client peut atteindre son serveur **en direct** (boucle locale idéale) ou **à travers
le proxy de chaos** (vraie connexion TCP sur laquelle on injecte latence, pics, coupures,
trou noir, panne). Les trois protocoles subissent donc exactement les mêmes conditions réseau.

Tous les ports sont décalés de `RPCX_PORT_OFFSET` (voir `common/config.py`), ce qui permet
de lancer plusieurs laboratoires en parallèle.

## 3. Arborescence

```
RPC/
├── implementation_plan.md        ← ce document
├── README.md
├── requirements.txt
├── main.py                       ← Phase 4 : point d'entrée (menu interactif + options)
├── common/                       ← socle partagé (aucune dépendance réseau)
│   ├── config.py                 hôtes, ports, constantes
│   ├── errors.py                 DomainError (serveur) / RpcError (client), codes canoniques
│   ├── telemetry.py              TraceEvent, EventBus (BUS), TraceCollector, hexdump
│   ├── inventory.py              InventoryService — logique métier
│   ├── catalog.py                METHODS — description des procédures
│   └── client_api.py             InventoryClient (interface commune) + LocalInventoryClient
├── rpc_custom/                   ← Phase 1 : RPC fait maison
│   ├── protocol.py               messages JSON-RPC 2.0 + tramage
│   ├── client_stub.py            stub / proxy dynamique
│   ├── server_skeleton.py        squelette / dispatcher
│   ├── inventory_binding.py      branchement du service métier + CustomInventoryClient
│   └── custom_rpc_demo.py        démo autonome
├── rpc_grpc/                     ← Phase 2 : gRPC & Protobuf
│   ├── protos/service.proto      contrat IDL v1
│   ├── protos/service_v2.proto   contrat v2 « mal évolué » (breaking changes)
│   ├── generate.py               invocation de protoc
│   ├── generated/                code généré
│   ├── converters.py             dict métier ⇄ messages Protobuf
│   ├── interceptors.py           intercepteurs de traçage
│   ├── wire_inspector.py         décodeur du format de fil Protobuf
│   ├── grpc_server.py            serveur (contrat v1)
│   ├── grpc_server_v2.py         serveur (contrat v2)
│   └── grpc_client.py            client
├── rest_api/                     ← point de comparaison REST
│   ├── rest_server.py
│   └── rest_client.py
├── netsim/                       ← réseau simulé & résilience
│   ├── conditions.py             NetworkConditions + préréglages
│   ├── chaos_proxy.py            proxy TCP injecteur de pannes
│   └── resilience.py             RetryPolicy, CircuitBreaker, ResilientClient
├── lab/
│   └── runtime.py                LabRuntime : démarre tout, fabrique les clients
├── benchmark_lab/                ← Phase 3 : avantages / inconvénients
│   ├── benchmark_perf.py         taille des payloads & temps d'exécution
│   ├── failure_simulation.py     latence, timeout, coupure, panne, doublons
│   ├── contract_evolution.py     breaking changes de contrat
│   ├── transparency_demo.py      transparence de localisation (comparaison de code)
│   └── report.py                 export JSON / Markdown / CSV
├── cli/
│   └── cli_runner.py             interface console (Rich)
├── dashboard/
│   ├── server.py                 API HTTP + WebSocket (FastAPI)
│   └── static/                   interface web (sans étape de build)
├── tests/
├── tools/shot.py                 capture d'écran headless (outil de dev)
└── reports/                      rapports générés
```

## 4. Socle `common/` (déjà écrit — lire le code, il fait foi)

- `config.py` — `HOST`, `Ports`, `default_ports()`, `PROTOCOLS`, `PROTOCOL_LABELS`,
  `DEFAULT_TIMEOUT_S`, `CONNECT_TIMEOUT_S`, `MAX_FRAME_BYTES`, `REPORTS_DIR`.
- `errors.py` — côté serveur `DomainError` (`InvalidArgument`, `ProductNotFound`,
  `InsufficientStock`) ; côté client `RpcError` → `RpcTransportError` (= `NetworkError`) →
  `RpcTimeoutError` ; `RpcRemoteError` → `MethodNotFoundError`, `InvalidArgumentError`,
  `NotFoundError`, `FailedPreconditionError`, `InternalRemoteError` ; `RpcProtocolError` ;
  `CircuitOpenError`. Codes canoniques (`TIMEOUT`, `UNAVAILABLE`, `NOT_FOUND`…) + tables de
  traduction JSON-RPC / gRPC / HTTP + `error_from_code()`.
- `inventory.py` — `InventoryService` : les procédures métier. Les dictionnaires renvoyés ont
  **exactement** les clés des messages Protobuf.
- `catalog.py` — `METHODS` (`MethodSpec`/`ParamSpec`), `CORE_METHODS`, `catalog_dict()`.
- `client_api.py` — `InventoryClient` (ABC) : `calculate_factorial`, `get_product_details`,
  `update_stock`, `list_products`, `stream_analytics`, `invoke()`, `submit()`, `close()`.
  **Toutes les implémentations renvoient des dictionnaires de forme identique** et lèvent des
  `RpcError`. Chaque appel affecte `self.last_call_id`.
- `telemetry.py` — voir §5.

Règles communes à tout le code :

- Identifiants en anglais, docstrings et commentaires en français, concis. Annotations de type.
- Python 3.11, Windows d'abord (pas de `signal.pause`, pas de `fork`, pas de sockets Unix).
- **Performance des sockets** : `TCP_NODELAY` sur toutes les sockets, et **un seul `sendall`
  par message** (en-tête + corps concaténés). Sans cela, l'algorithme de Nagle combiné aux
  ACK retardés ajoute ~40 ms par appel et fausse tous les benchmarks.
- Threads démons ; tout serveur expose `start()` (retour quand il écoute réellement),
  `stop()` (idempotent, libère le port) et `port` (port réel si `port=0`).
- Les tests utilisent `port=0` (port éphémère) et s'exécutent en quelques secondes.

## 5. Télémétrie « sous le capot »

Chaque étape d'un appel publie un `TraceEvent` sur `BUS` (`common/telemetry.py`) via
`BUS.emit(call_id=…, protocol=…, side=…, stage=…, method=…, duration_us=…, payload=…, detail=…)`.

**Les émetteurs testent `bus.enabled` avant de construire quoi que ce soit** : quand le bus
est coupé (`with BUS.muted():`, utilisé par les benchmarks), le surcoût doit être nul.

| # | `stage`            | Émetteur            | `payload` (octets bruts)                    | `duration_us`              |
|---|--------------------|---------------------|---------------------------------------------|----------------------------|
| 1 | `client.call`      | stub client         | —  (`detail.args`, `detail.kwargs`)         | —                          |
| 2 | `client.marshal`   | stub client         | message sérialisé (sans tramage)            | temps de sérialisation     |
| 3 | `client.send`      | stub client         | **octets émis** (tramage / en-têtes inclus) | temps d'écriture           |
| 4 | `server.receive`   | squelette serveur   | octets reçus (tramage inclus)               | —                          |
| 5 | `server.unmarshal` | squelette serveur   | —                                           | temps de désérialisation   |
| 6 | `server.dispatch`  | squelette serveur   | —  (`detail.target`, `detail.bound_args`)   | temps de résolution        |
| 7 | `server.execute`   | squelette serveur   | —                                           | **temps de la procédure**  |
| 8 | `server.marshal`   | squelette serveur   | message sérialisé                           | temps de sérialisation     |
| 9 | `server.send`      | squelette serveur   | octets émis                                 | temps d'écriture           |
|10 | `client.receive`   | stub client         | octets reçus                                | attente réseau + serveur   |
|11 | `client.unmarshal` | stub client         | —                                           | temps de désérialisation   |
|12 | `client.return`    | stub client         | —  (`detail.result_preview`)                | **durée totale de l'appel**|
| — | `client.error`     | stub client         | —  (`detail.code`, `detail.message`)        | **durée totale de l'appel**|

- `call_id` : généré par le client avec `new_call_id(protocol)` et **propagé au serveur** —
  JSON-RPC : c'est l'`id` de la requête ; gRPC : métadonnée `x-call-id` ; REST : en-tête
  `X-Call-Id`. Client et serveur publient donc sous le même identifiant.
- `size` des étapes `*.send` / `*.receive` = octets applicatifs réellement échangés.
- Flux : `server.stream_item` / `client.stream_item` par élément (payload = élément sérialisé).
- **Segments** : les évènements `client.send`, `client.receive`, `server.receive`,
  `server.send` portent `detail["segments"]`, une liste qui découpe `payload` pour la vue
  hexadécimale colorée :
  `{"label": str, "start": int, "end": int, "kind": "frame"|"header"|"tag"|"len"|"value"|"body", "field"?: str, "value"?: any, "depth"?: int}`
  - JSON-RPC maison : `[0:4] frame « Longueur (uint32 big-endian) »`, `[4:N] body « Message JSON-RPC 2.0 »`.
  - gRPC : `[0:1] frame « Drapeau de compression »`, `[1:5] frame « Longueur du message »`, puis
    un triplet `tag` / `len` / `value` par champ Protobuf (nom du champ, numéro, type de fil, valeur décodée).
  - REST : `header « Ligne de requête/statut »`, `header « En-têtes HTTP »`, `body « Corps JSON »`.
- `detail` peut aussi porter : `frame_header_hex`, `http2_headers` (gRPC : `:method`, `:path`,
  `content-type`, `te`, `grpc-timeout`), `http` (REST : méthode, chemin, statut), `text`.

## 6. Phase 1 — RPC maison (`rpc_custom/`)

**Format de fil** : `trame = longueur (4 octets, uint32 big-endian) + message JSON UTF-8 compact`
(`separators=(",", ":")`, `ensure_ascii=False`). Taille maximale `MAX_FRAME_BYTES`.

**JSON-RPC 2.0 complet** : requêtes (`params` positionnels *ou* nommés), réponses, erreurs,
notifications (sans `id`, sans réponse), lots (*batch* : tableau de requêtes → tableau de
réponses). Codes d'erreur : `-32700`, `-32600`, `-32601`, `-32602`, `-32603`, et dans la plage
serveur `-32001` (NOT_FOUND), `-32002` (FAILED_PRECONDITION), `-32000` (générique).
`error.data` porte `{"canonical": "<CODE>", …}`.

**Extensions** (préfixe réservé `rpc.`) :
- `rpc.discover` → `{"methods": [{"name", "params": [{"name","kind","default","annotation"}], "doc", "streaming": bool}]}`.
- Streaming serveur : quand la procédure renvoie un générateur, le squelette envoie pour chaque
  élément une notification `{"jsonrpc":"2.0","method":"rpc.stream.item","params":{"id":<id requête>,"seq":k,"item":…}}`
  puis la réponse finale `{"jsonrpc":"2.0","id":<id>,"result":{"stream":"end","count":N}}`.

`protocol.py` — `Request`, `Response`, `ErrorObject` (dataclasses), `encode_request`,
`encode_response`, `encode_batch`, `decode_message(bytes)`, `frame(bytes)`, `read_frame(sock)`,
`HEADER_SIZE`, constantes de codes, `error_to_exception()` / `exception_to_error()`.

`server_skeleton.py` — `RpcServerSkeleton(host=HOST, port=0, *, name="custom", bus=BUS)` :
`register(func, name=None)`, décorateur `expose`, `register_instance(obj, names=None)`,
`methods()`, `start()`, `stop()`, `port`. Un thread par connexion, connexions persistantes,
plusieurs requêtes par connexion. Liaison des arguments par `inspect.signature().bind()` →
`-32602` si la signature ne correspond pas. `DomainError` → code correspondant ; toute autre
exception → `-32603` (sans divulguer la pile). Message illisible → `-32700`.

`client_stub.py` — `RpcClientStub(host, port, *, timeout=DEFAULT_TIMEOUT_S, connect_timeout=CONNECT_TIMEOUT_S, bus=BUS, protocol="custom")` :
- **proxy dynamique** : `stub.update_stock("SKU-1001", -3)` via `__getattr__` ;
- `call(method, *args, timeout=None, **kwargs)`, `call_async(…) -> Future`, `notify(…)`,
  `batch([(method, params), …]) -> list`, `stream(method, *args, **kwargs) -> Iterator`,
  `discover()`, `connect()`, `close()`, gestionnaire de contexte ;
- **multiplexage** : un thread lecteur associe les réponses aux appels en attente par `id` ;
  plusieurs appels asynchrones partagent la même connexion (*pipelining*) ;
- connexion paresseuse et **reconnexion automatique** à l'appel suivant une coupure ;
- erreurs : pas de réponse à temps → `RpcTimeoutError` ; connexion refusée/coupée →
  `RpcTransportError` (tous les appels en attente échouent) ; trame invalide → `RpcProtocolError` ;
  erreur renvoyée par le serveur → sous-classe de `RpcRemoteError`.

`inventory_binding.py` — `build_inventory_skeleton(service, host=HOST, port=0, *, bus=BUS) -> RpcServerSkeleton`
(enregistre les 7 procédures) et `CustomInventoryClient(host, port, *, timeout=…, bus=BUS)`
(`InventoryClient` ; attribut `stub` ; paramètres **nommés** sur le fil ; `submit()` surchargé
pour multiplexer réellement sur la connexion).

`build_inventory_skeleton_v2(service, host=HOST, port=0, *, bus=BUS)` — le même service après
une évolution de contrat non coordonnée (utilisé par le laboratoire « Contrat ») :
`update_stock(product_id, delta, warehouse)` (paramètre obligatoire ajouté → `-32602` pour un
ancien client), `get_product` remplace `get_product_details` (→ `-32601`),
`calculate_factorial` inchangée mais renvoie une clé supplémentaire `algorithm` (compatible),
`list_products` renvoie `price_cents` à la place de `price` et `quantity` à la place de `stock`
(→ `KeyError` dans le code client qui lit `product["stock"]`).

`custom_rpc_demo.py` — `python -m rpc_custom.custom_rpc_demo` : démarre le serveur, déroule
appels synchrones / asynchrones / lot / flux / erreurs en affichant les messages bruts.

## 7. Phase 2 — gRPC (`rpc_grpc/`)

Contrats : `protos/service.proto` (v1) et `protos/service_v2.proto` (v2), déjà écrits et
compilés (`python -m rpc_grpc.generate`, `ensure_generated()`).

- `converters.py` — conversions **explicites** dict ⇄ message (ne pas utiliser `MessageToDict`,
  qui transforme les `int64` en chaînes) ; forme strictement identique à `InventoryService`.
- `grpc_server.py` — `InventoryServicer` (4 unaires + flux serveur `StreamAnalytics` + flux
  client `BulkUpdateStock` + flux bidirectionnel `CheckStock`) ; `DomainError` → statut gRPC
  (`context.abort`). `create_grpc_server(service, host=HOST, port=0, *, max_workers=16, bus=BUS) -> GrpcServerHandle`
  (`start()`, `stop(grace=0.2)`, `port`). Exécutable : `python -m rpc_grpc.grpc_server`.
- `grpc_client.py` — `GrpcInventoryClient(host, port, *, timeout=DEFAULT_TIMEOUT_S, bus=BUS)` :
  `InventoryClient` + `bulk_update_stock(updates)` + `check_stock(product_ids) -> Iterator` ;
  attributs `channel`, `grpc_stub`. Échéance (*deadline*) propagée par `timeout=`. Options de
  canal : `grpc.initial_reconnect_backoff_ms=100`, `grpc.min_reconnect_backoff_ms=100`,
  `grpc.max_reconnect_backoff_ms=1000`, `grpc.enable_retries=0` (les retries sont gérés et
  montrés explicitement par `netsim/resilience.py`). `grpc.RpcError` → `RpcError` canonique.
  Exécutable : `python -m rpc_grpc.grpc_client`.
- `interceptors.py` — traçage client et serveur publiant les évènements du §5 (octets
  Protobuf réels + préfixe gRPC de 5 octets + pseudo en-têtes HTTP/2). Surcoût nul bus coupé.
- `wire_inspector.py` — `decode_wire(data, descriptor=None) -> list[dict]` (numéro de champ,
  type de fil, octets du tag / de la longueur / de la valeur, valeur interprétée, sous-messages
  récursifs, offsets) et `segments_for(data, descriptor, base_offset=0)` pour le §5.
- `grpc_server_v2.py` — `create_grpc_server_v2(service, host=HOST, port=0, *, strict=False, bus=BUS)` :
  sert les messages v2 **sous le nom de service v1** (`rpcexplorer.v1.InventoryService`) via
  `grpc.method_handlers_generic_handler`. `GetProductDetails` n'existe plus (→ `UNIMPLEMENTED`),
  `UpdateStock` lit `delta` au n°4 (un client v1 l'envoie au n°2 → lu `0` : **corruption
  silencieuse**) ; si `strict=True`, `warehouse` vide → `INVALID_ARGUMENT` (**rejet**).
  L'attribut `strict` est modifiable à chaud.

## 8. Référence REST (`rest_api/`)

`rest_server.py` — `create_rest_server(service, host=HOST, port=0, *, bus=BUS) -> RestServerHandle`
(`ThreadingHTTPServer`, HTTP/1.1 keep-alive). Routes :

| Verbe | Route                                         | Procédure             |
|-------|-----------------------------------------------|-----------------------|
| GET   | `/api/factorial/{n}`                          | `calculate_factorial` |
| GET   | `/api/products/{product_id}`                  | `get_product_details` |
| GET   | `/api/products?limit=&category=`              | `list_products`       |
| POST  | `/api/stock/{product_id}` `{"delta","idempotency_key"}` | `update_stock` |
| GET   | `/api/analytics/stream?samples=&interval_ms=` | `stream_analytics` (NDJSON, *chunked*) |
| GET   | `/api/health`                                 | —                     |

Erreurs : statut HTTP (`HTTP_STATUS`) + corps `{"error": {"code": "<CANONIQUE>", "message": …, "detail": …}}`.

`rest_client.py` — `RestInventoryClient(host, port, *, timeout=…, bus=BUS)` (`InventoryClient`),
connexion persistante `http.client`, reconnexion automatique, octets de requête exacts.

## 9. Réseau simulé (`netsim/`)

`conditions.py` — `NetworkConditions` (dataclass, sûre vis-à-vis des threads) :

| Champ               | Sens                                                              |
|---------------------|-------------------------------------------------------------------|
| `latency_ms`        | latence **aller-retour** ajoutée (½ par sens)                     |
| `jitter_ms`         | variation aléatoire ± sur l'aller-retour                          |
| `spike_probability` | probabilité qu'une requête subisse un pic                         |
| `spike_ms`          | durée du pic                                                      |
| `reset_probability` | probabilité de couper la connexion à l'arrivée d'une requête      |
| `blackhole`         | les octets sont avalés, aucune réponse (→ timeout)                |
| `down`              | serveur injoignable : connexions fermées immédiatement            |
| `bandwidth_kbps`    | débit maximal (0 = illimité)                                      |
| `preset`            | nom du préréglage actif (`"custom"` après modification manuelle)  |

Méthodes : `update(**fields)`, `snapshot() -> dict`, `apply_preset(name)`, `reset()`.
`PRESETS` : `ideal`, `lan`, `wan`, `mobile_3g`, `satellite`, `flaky`, `blackhole`, `outage`
(chacun avec `label` et `description` en français via `PRESET_INFO`).

`chaos_proxy.py` — `ChaosProxy(name, listen_port, target_port, *, listen_host=HOST, target_host=HOST, conditions=None, bus=BUS)` :
`start()`, `stop()`, `port`, `conditions` (objet **partageable** entre proxys), `stats() -> dict`,
`reset_stats()`, `arm(kind, count=1)` avec `kind ∈ {"reset", "lost_reply"}` :
- `reset` : la prochaine requête coupe la connexion **sans atteindre le serveur** ;
- `lost_reply` : la prochaine requête atteint le serveur (qui l'exécute), mais la réponse est
  avalée et la connexion coupée après `hold_ms` — l'appelant ne sait pas si l'effet a eu lieu.
Latence = ligne à retard par sens (les échanges ne se sérialisent pas). Publie
`network.delay|reset|blackhole|refuse|lost_reply` (`side="network"`, `protocol=name`).
`stats()` : `connections_total`, `connections_active`, `bytes_up`, `bytes_down`, `chunks_up`,
`chunks_down`, `resets`, `refused`, `blackholed`, `lost_replies`.

`resilience.py` — `RetryPolicy(max_attempts=3, base_delay_ms=100, multiplier=2.0, max_delay_ms=2000, jitter=0.2, retry_on=("TIMEOUT","UNAVAILABLE"), retry_non_idempotent=True)`,
`CircuitBreaker(failure_threshold=3, reset_timeout_s=2.0, name="")` (`closed` / `open` /
`half_open`, `allow()`, `record_success()`, `record_failure()`, `snapshot()`),
`ResilientClient(inner, *, timeout=None, retry=None, breaker=None, auto_idempotency_key=False, bus=BUS)`
(`InventoryClient` enveloppant n'importe quel client ; `last_report = {"method","outcome","total_ms","breaker","attempts":[{"n","call_id","outcome","code","duration_ms","backoff_ms"}]}`).
Publie `resilience.attempt|backoff|breaker|dedup|give_up` (`side="resilience"`).

## 10. Orchestration (`lab/runtime.py`)

`LabRuntime(ports: Ports | None = None)` :
- `start()` / `stop()` / gestionnaire de contexte ; `start()` démarre les 3 serveurs, leurs
  3 proxys, les 2 serveurs « contrat v2 » et le `TraceCollector` ; si un port est occupé, erreur claire.
- attributs : `service`, `service_v2` (inventaire séparé), `ports`, `conditions`,
  `proxies: dict[str, ChaosProxy]`, `collector`, `grpc_v2` (poignée, pour `strict`).
- `client(protocol, *, via_proxy=False, timeout=None) -> InventoryClient` (nouveau client à
  chaque appel ; l'appelant le ferme) ; `client_v2(protocol)` pour `custom` et `grpc`.
- `status() -> dict`, `reset()` (inventaires, conditions, statistiques, traces).
- `LabRuntime.ephemeral()` : tous les ports à 0 (attribués par le système) ; après `start()`,
  `runtime.ports` contient les ports réels. C'est ce que les tests utilisent.

## 11. Phase 3 — Banc d'essai (`benchmark_lab/`)

`benchmark_perf.py` (latences mesurées **bus coupé**, après échauffement, `perf_counter_ns`) :
- `measure_payload_sizes(runtime) -> dict` : taille requête/réponse par procédure pour JSON-RPC
  maison, Protobuf (message seul et avec préfixe gRPC) et REST (HTTP complet) ; **octets réels
  sur le fil par appel** mesurés par les compteurs des proxys ; montée en charge de
  `list_products` (1, 10, 100, 1000 produits).
- `measure_serialization(iterations=2000) -> dict` : temps d'encodage / décodage JSON vs Protobuf.
- `run_latency_benchmark(runtime, *, method, params, protocols, iterations=1000, warmup=50, concurrency=1, via_proxy=False, progress=None) -> dict` :
  par protocole `count, errors, mean_ms, median_ms, p90_ms, p95_ms, p99_ms, min_ms, max_ms, stdev_ms, total_s, rps, histogram{edges_ms,counts}, samples_ms (≤ 400 points)`.
- `run_network_sweep(runtime, *, latencies_ms=(0, 10, 50, 100, 200), iterations=20, progress=None) -> dict` :
  « local vs distant » — la même boucle, de plus en plus loin.
- `run_full_benchmark(runtime, config: dict | None = None, progress=None) -> dict` : rapport complet
  `{id, created_at, config, environment, payload, serialization, latency, network, highlights}`.
- `progress(phase: str, fraction: float, message: str, partial: dict | None)`.

`failure_simulation.py` — `SCENARIOS` (métadonnées) et
`run_scenario(runtime, scenario_id, *, protocol="custom", on_step=None) -> dict`
renvoyant `{id, title, protocol, summary, verdict, lesson, metrics: {…}, steps: [{t_ms, kind, label, status, detail}]}`.
Scénarios : `latency_trap` (200 ms : la boucle « innocente » de N appels), `timeout_spike`
(pics de latence, avec/sans échéance), `connection_cut` (coupure en plein appel : client naïf
vs client résilient), `server_outage` (panne : retries épuisés → disjoncteur ouvert → reprise),
`duplicate_execution` (réponse perdue + retry sur `update_stock` → double débit ; corrigé par
la clé d'idempotence). Chaque scénario **restaure** les conditions réseau en sortie (`finally`).

`contract_evolution.py` — `contract_overview() -> dict` (sources des deux `.proto`, diff ligne à
ligne, liste des changements classés `breaking` / `compatible`) et
`run_contract_scenario(runtime, scenario_id) -> dict` renvoyant
`{id, title, protocol, change, outcome ∈ {"rejected","crash","silent_corruption","compatible"}, expected, observed, status, explanation, call_id}`.
Scénarios gRPC : méthode renommée (`UNIMPLEMENTED`), numéro réutilisé avec autre type
(corruption silencieuse), validation stricte (rejet `INVALID_ARGUMENT`), champ ajouté
(compatible), lecture d'un `Product` v2 par un client v1 (prix à 0, mauvais stock).
Scénarios JSON-RPC : paramètre obligatoire ajouté (`-32602`), méthode renommée (`-32601`),
clé de résultat renommée (`KeyError` côté client).

`transparency_demo.py` — les quatre façons d'écrire le même `update_stock` (local, stub maison,
gRPC, REST « à la main ») sous forme de vraies fonctions exécutables ; `code_comparison() -> dict`
(`inspect.getsource`, lignes de code, préoccupations gérées à la main) + l'exemple `fetch()` JS.

`report.py` — `save_report(report) -> Path` (JSON dans `reports/`), `to_markdown(report)`,
`to_csv(report)`, `list_reports()`, `load_report(name)`.

## 12. Phase 4 — CLI (`main.py`, `cli/cli_runner.py`)

```
python main.py                       menu interactif (Rich)
python main.py --benchmark           banc d'essai complet → tableaux + rapport   [--iterations N] [--quick]
python main.py --simulate-failures   démonstration des pannes                    [--scenario ID] [--protocol P]
python main.py --contract            démonstration des breaking changes
python main.py --demo                visite guidée « sous le capot » (JSON-RPC maison puis gRPC)
python main.py --dashboard           dashboard web                               [--port N] [--no-browser]
python main.py --serve               lance tous les serveurs au premier plan
python main.py --call METHOD [ARGS…] appel unitaire  [--protocol P] [--inspect] [--via-proxy]
```

Menu interactif : appels RPC (synchrone, asynchrone, flux), mode « sous le capot » (messages
bruts, hexdump, chronologie des étapes), banc d'essai, laboratoire de pannes (réglage des
conditions réseau), contrat, transparence, état des serveurs. Sortie UTF-8 forcée sous Windows.

## 13. Dashboard — API (`dashboard/server.py`)

`create_app(runtime) -> FastAPI`, `serve(runtime, host=HOST, port=…, open_browser=True)`.
Fichiers statiques servis à `/` avec `Cache-Control: no-store`. JSON partout.
Les appels bloquants s'exécutent hors de la boucle d'évènements.

| Méthode & route                 | Rôle |
|---------------------------------|------|
| `GET /api/status`               | `{app:{name,version,uptime_s}, servers:[{id,label,transport,host,port,proxy_port,up}], network:{…conditions}, proxies:{custom:{stats},…}, totals:{protocol:{calls,errors,bytes_out,bytes_in,avg_ms}}, inventory:{…stats}}` |
| `GET /api/catalog`              | `{methods:[MethodSpec], protocols:[{id,label,transport}], products:[{id,name,category,stock}], categories:[…], stages:{STAGE_INFO}, pipeline:[…]}` |
| `POST /api/call`                | exécute un appel — voir ci-dessous |
| `POST /api/inspect`             | « sous le capot » : `{method, params, protocols:[…], via_proxy}` → `{traces:[{protocol, ok, result, error, summary, events:[TraceEvent + offset_us]}]}` |
| `GET /api/traces?limit=`        | résumés des derniers appels |
| `GET /api/traces/{call_id}`     | trace complète (évènements + payloads hex) |
| `GET /api/network`              | `{conditions, presets:[{id,label,description,conditions}], proxies:{…stats}}` |
| `PUT /api/network`              | `{preset: "wan"}` ou champs de `NetworkConditions` → nouvelles conditions |
| `POST /api/network/arm`         | `{kind: "reset"|"lost_reply", protocol, count}` |
| `POST /api/benchmark/run`       | `{suites:["payload","serialization","latency","network"], iterations, warmup, method, protocols, concurrency, sweep_latencies_ms}` → `{job_id}` |
| `GET /api/benchmark/latest`     | dernier rapport (ou `null`) |
| `GET /api/failures/scenarios`   | métadonnées des scénarios |
| `POST /api/failures/run`        | `{scenario, protocol}` → `{job_id}` |
| `GET /api/jobs/{job_id}`        | `{id, kind, state: "running"|"done"|"error", progress, phase, message, result, error}` |
| `GET /api/contract`             | `contract_overview()` + scénarios |
| `POST /api/contract/run`        | `{scenario: id | "all", strict?: bool}` → `{results:[…]}` |
| `GET /api/code-compare`         | `code_comparison()` |
| `GET /api/summary`              | bilan avantages / inconvénients chiffré à partir des dernières mesures |
| `GET /api/reports` · `GET /api/reports/{name}?format=json|md|csv` | rapports enregistrés |
| `POST /api/reset`               | remet le laboratoire à zéro |
| `WS /ws`                        | flux temps réel |

`POST /api/call` — corps :
`{protocol, method, params, mode: "sync"|"async"|"stream", count, via_proxy, timeout_ms, policy: null | {max_attempts, base_delay_ms, breaker, auto_idempotency_key}}`
- `sync` → `{ok, result, error, call_id, duration_ms, request_bytes, response_bytes, attempts}`
- `async` (`count` appels lancés ensemble) → `{ok, mode, count, wall_ms, sum_ms, speedup, calls:[{call_id, ok, duration_ms, error}]}`
- `stream` → `{ok, mode, stream_id}` puis, sur le WebSocket, `stream` (un par élément) et `stream_end`.
- `error` = `RpcError.to_dict()` ; jamais de 500 pour une erreur RPC (c'est un résultat attendu).

Messages WebSocket (`{type, …}`) : `hello`, `stats` (chaque seconde : totaux, débit, état des
serveurs, conditions réseau), `call` (résumé de chaque appel terminé), `trace` (chaque
`TraceEvent`, payload inclus, si le client a envoyé `{"type":"subscribe","topics":[…"trace"]}`),
`network` (évènements du proxy), `resilience`, `job` (progression : `{job_id, kind, state, phase, progress, message, partial}`),
`stream` / `stream_end`.

## 14. Dashboard — interface (`dashboard/static/`)

Aucune étape de build : HTML + CSS + modules ES servis tels quels, polices et icônes embarquées
(fonctionne hors ligne). Langue : français.

```
static/
├── index.html
├── fonts/                      Inter (variable) + JetBrains Mono (variable), woff2
├── css/  tokens.css · base.css · layout.css · components.css · charts.css · pages/<page>.css
└── js/
    ├── app.js                  amorçage, coquille, routeur
    ├── core/                   dom.js · store.js · api.js · router.js · format.js · icons.js · theme.js
    ├── components/             ui.js · charts.js · hexview.js · codeblock.js · jsontree.js · toast.js · modal.js · palette.js · wiretap.js
    └── pages/                  overview.js · console.js · xray.js · benchmark.js · chaos.js · contract.js · compare.js · learn.js
```

Chaque page : `export default { id, title, icon, mount(container, ctx) }` où `mount` renvoie une
fonction de nettoyage. Routage par `location.hash` (`#/overview`, `#/console`, …).

### Pages

1. **Vue d'ensemble** — état des serveurs, compteurs en direct, débit, architecture animée.
2. **Console RPC** — choisir protocole / procédure / paramètres ; appels synchrones,
   asynchrones (N en parallèle), flux ; résultat, durée, tailles ; historique.
3. **Sous le capot** — pipeline animé stub → marshalling → transport → squelette → procédure
   et retour ; chronologie réelle des étapes ; octets bruts (JSON, hexadécimal coloré, décodage
   Protobuf champ par champ) ; comparaison côte à côte des trois protocoles pour le même appel.
4. **Benchmark** — configuration, exécution avec progression en direct, graphiques (tailles,
   distribution des latences, percentiles, débit, local vs distant), tableau, export.
5. **Chaos réseau** — réglage des conditions (curseurs + préréglages), politique du client
   (timeout, retries, disjoncteur, idempotence), scénarios guidés, chronologie des appels.
6. **Contrat & IDL** — diff `.proto` v1 ↔ v2, scénarios de rupture, verdict par scénario.
7. **Transparence** — le même appel écrit de quatre façons, côte à côte, exécutable.
8. **Bilan** — synthèse avantages / inconvénients chiffrée par les mesures du laboratoire.

### Système de design — « instrument de laboratoire »

Référence de niveau : Linear, Vercel, Raycast. Sobre, dense, précis ; aucun effet gratuit.

- **Thème sombre par défaut**, thème clair complet (bascule persistée dans `localStorage`
  sous la clé `rpcx.theme`, attribut `data-theme` sur `<html>`).
- **Surfaces (sombre)** : `--bg-0 #07090D` (application) · `--bg-1 #0C0F15` (barres) ·
  `--bg-2 #11151D` (cartes) · `--bg-3 #171C26` (survol / relief) · bordures
  `rgba(255,255,255,.07)` et `.12`. **Texte** : `--fg-0 #EDEFF3` · `--fg-1 #A9B1BF` ·
  `--fg-2 #6B7485` · `--fg-3 #4A5262`.
- **Accent de marque** : iris `#8B7CFF` (dégradé `#8B7CFF → #5CE1E6` réservé au logo et à de
  rares moments forts).
- **Couleurs de protocole — identiques partout** (graphiques, puces, pipelines) :
  local `#8A94A6` · JSON-RPC maison `#5AA2FF` · gRPC `#2FD9C4` · REST `#F2789F`.
- **États** : succès `#4ADE80` · avertissement `#FBBF24` · danger `#F87171` · info `#60A5FA`.
- **Typographie** : Inter pour l'interface, JetBrains Mono pour le code, les octets et tous les
  nombres mesurés (`font-variant-numeric: tabular-nums`). Échelle 11 / 12 / 13 / 14 / 16 / 20 /
  28 / 40. Titres resserrés (`letter-spacing: -0.02em`), libellés de section en petites
  capitales espacées.
- **Formes** : rayons 14 px (cartes), 10 px (contrôles), 6 px (puces) ; bordures d'un pixel ;
  léger reflet supérieur sur les cartes ; ombres douces ; flou d'arrière-plan sur la barre
  haute, les tiroirs et la palette de commandes.
- **Mouvement** : 120–200 ms `cubic-bezier(.2,.8,.2,1)` ; apparition échelonnée des cartes ;
  nombres qui s'incrémentent ; paquets qui circulent dans les pipelines ;
  `prefers-reduced-motion` respecté.
- **Graphiques** (SVG maison, sans dépendance) : traits fins, barres à sommet arrondi,
  quadrillage à peine visible, libellés directs, info-bulle + réticule au survol, légende en
  puces cliquables, animation à l'apparition, adaptation à la largeur.
- **Finitions attendues** : palette de commandes (Ctrl/⌘ K), raccourcis clavier, notifications,
  états vides et squelettes de chargement soignés, anneaux de focus visibles, contrastes AA,
  mise en page fluide de 1100 à 1920 px.

## 15. Vérification

1. `pytest` — protocole JSON-RPC (conformité, tramage, lots, flux, erreurs), stub/squelette,
   gRPC (unaire + 3 types de flux), REST, proxy de chaos, résilience, runtime, benchmarks,
   scénarios de pannes et de contrat, API du dashboard.
2. `python main.py --benchmark` — tableau comparatif : taille du paquet (octets) JSON vs
   Protobuf ; temps moyen par appel (ms) local vs RPC maison vs gRPC (vs REST).
3. `python main.py --simulate-failures` — différence observable entre un appel local et un
   appel RPC soumis aux règles du réseau.
4. `python main.py --dashboard` — parcours visuel des huit pages, thèmes sombre et clair.
