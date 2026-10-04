"""Appels RPC déclenchés depuis le dashboard : synchrones, groupés, en flux, ou inspectés.

Les durées affichées doivent être celles de l'appel, pas celles d'une ouverture de
connexion : chaque couple (protocole, direct ou via le proxy) dispose donc d'une « voie »,
un client longue durée servi par un unique thread. Ce thread est le seul à utiliser le
client, ce qui rend ``last_call_id`` fiable et réutilise la même connexion d'un appel à
l'autre. Après une panne de transport, la voie repart d'un client neuf.

Durée et tailles d'un appel sont lues dans sa trace (``TraceCollector``) : ce sont les
valeurs mesurées par le middleware lui-même. Quand le bus de traces est coupé (banc
d'essai en cours), la durée est celle observée autour de l'appel et les tailles manquent.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, Iterator, TypeVar

from common.catalog import METHODS, MethodSpec, method_spec
from common.client_api import InventoryClient, LocalInventoryClient
from common.config import PROTOCOLS, REMOTE_PROTOCOLS
from common.errors import OK, RpcError, RpcTransportError
from common.telemetry import CallTrace, TraceEvent
from netsim import CircuitBreaker, ResilientClient, RetryPolicy

from .hub import Message
from .payloads import BUSY, ApiError, Fields, malformed, not_found

if TYPE_CHECKING:
    from lab import LabRuntime

MODES: tuple[str, ...] = ("sync", "async", "stream")
MAX_ASYNC_CALLS = 200
ASYNC_WORKERS = 16          # appels simultanés d'un lot, pour les protocoles qui mobilisent un thread par appel
MAX_STREAMS = 16            # flux ouverts en même temps depuis le dashboard
MAX_TIMEOUT_MS = 60_000
MAX_ATTEMPTS = 10
MAX_BASE_DELAY_MS = 10_000

_RESULT_STREAMS: frozenset[str] = frozenset({"server_stream", "bidi_stream"})   # le résultat est un itérateur

# Appel d'échauffement d'une inspection : servi par tous les protocoles, sans effet sur l'inventaire.
_WARM_UP_METHOD = "calculate_factorial"
_WARM_UP_PARAMS: dict[str, Any] = {"n": 1}
_WARM_UP_TIMEOUT_S = 2.0    # sous un réseau dégradé, l'échauffement ne doit pas doubler l'attente
_STREAM_METHODS = ", ".join(spec.name for spec in METHODS if spec.kind != "unary")

# Comment chaque protocole mène plusieurs appels de front : la réponse n'est pas la même, et c'est instructif.
_ASYNC_STRATEGIES: dict[str, str] = {
    "local": f"Aucun réseau : jusqu'à {ASYNC_WORKERS} threads appellent directement la fonction, et le verrou "
             "global de l'interpréteur Python ne laisse progresser qu'un calcul à la fois.",
    "custom": "Multiplexage : toutes les requêtes partent d'un trait sur une seule connexion TCP, et chaque "
              "réponse retrouve son appel grâce à l'identifiant JSON-RPC.",
    "grpc": f"HTTP/2 : jusqu'à {ASYNC_WORKERS} appels simultanés, chacun sur son propre flux d'une même connexion.",
    "rest": f"HTTP/1.1 ne multiplexe pas : jusqu'à {ASYNC_WORKERS} connexions ouvertes, une par appel en cours.",
}
_ASYNC_RETRIES = (
    f"Avec une politique de retries, chaque appel logique occupe un thread (jusqu'à {ASYNC_WORKERS} de front) "
    "le temps de ses tentatives."
)

_log = logging.getLogger(__name__)

T = TypeVar("T")
Publish = Callable[[str, Message], None]


def _required_params(method: str) -> tuple[str, ...]:
    """Paramètres sans valeur par défaut dans la signature Python de la procédure."""
    signature = inspect.signature(getattr(LocalInventoryClient, method))
    return tuple(
        name for name, parameter in signature.parameters.items()
        if name != "self" and parameter.default is parameter.empty
    )


_REQUIRED: dict[str, tuple[str, ...]] = {spec.name: _required_params(spec.name) for spec in METHODS}


def required_params(method: str) -> tuple[str, ...]:
    """Paramètres obligatoires d'une procédure du catalogue (les autres ont une valeur par défaut)."""
    return _REQUIRED[method]


def checked_params(method: str, params: dict[str, Any]) -> MethodSpec:
    """Contrôle le NOM des arguments, pas leur valeur.

    Une valeur invalide doit atteindre le middleware : voir chacun la refuser à sa façon
    (avant l'envoi pour gRPC, par le serveur pour les autres) fait partie de la démonstration.
    """
    spec = method_spec(method)
    if spec is None:
        raise not_found(f"Procédure inconnue : {method!r} (connues : {', '.join(_REQUIRED)}).")
    known = [param.name for param in spec.params]
    unknown = sorted(set(params) - set(known))
    if unknown:
        raise malformed(
            f"Paramètre(s) inconnu(s) pour {method} : {', '.join(unknown)} (attendus : {', '.join(known)})."
        )
    missing = [name for name in _REQUIRED[method] if name not in params]
    if missing:
        raise malformed(f"Paramètre(s) manquant(s) pour {method} : {', '.join(missing)}.")
    # Seules les deux listes des flux gRPC sont contrôlées : un flux ne se construit pas sur autre chose.
    updates, product_ids = params.get("updates"), params.get("product_ids")
    if "updates" in params and not (isinstance(updates, list) and all(isinstance(item, dict) for item in updates)):
        raise malformed("« updates » doit être une liste d'objets {product_id, delta}.")
    if "product_ids" in params and not isinstance(product_ids, list):
        raise malformed("« product_ids » doit être une liste de références.")
    return spec


@dataclass(frozen=True)
class Policy:
    """Politique de résilience demandée pour un appel (``netsim.resilience``)."""

    max_attempts: int = 3
    base_delay_ms: float = 100.0
    breaker: bool = False
    auto_idempotency_key: bool = False
    retry_non_idempotent: bool = True

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Policy":
        fields = Fields(
            data, ("max_attempts", "base_delay_ms", "breaker", "auto_idempotency_key", "retry_non_idempotent")
        )
        return cls(
            max_attempts=fields.integer("max_attempts", cls.max_attempts, 1, MAX_ATTEMPTS),
            base_delay_ms=fields.number("base_delay_ms", cls.base_delay_ms, 0, MAX_BASE_DELAY_MS),
            breaker=fields.boolean("breaker", cls.breaker),
            auto_idempotency_key=fields.boolean("auto_idempotency_key", cls.auto_idempotency_key),
            retry_non_idempotent=fields.boolean("retry_non_idempotent", cls.retry_non_idempotent),
        )


@dataclass(frozen=True)
class CallRequest:
    """Corps validé de ``POST /api/call``."""

    protocol: str
    method: str
    params: dict[str, Any]
    mode: str = "sync"
    count: int = 1
    via_proxy: bool = False
    timeout: float | None = None        # secondes ; None : délai par défaut du client
    policy: Policy | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "CallRequest":
        fields = Fields(
            data, ("protocol", "method", "params", "mode", "count", "via_proxy", "timeout_ms", "policy")
        )
        protocol = fields.text("protocol", choices=PROTOCOLS)
        method = fields.text("method")
        mode = fields.text("mode", "sync", choices=MODES)
        params = fields.mapping("params", {})
        spec = checked_params(method, params)
        if mode == "stream" and spec.kind == "unary":
            raise malformed(
                f"« {method} » est un appel unaire : le mode « stream » est réservé aux procédures en flux "
                f"({_STREAM_METHODS})."
            )
        if mode != "stream" and spec.kind in _RESULT_STREAMS:
            raise malformed(f"« {method} » renvoie un flux d'éléments : utilisez le mode « stream ».")
        count = fields.integer("count", 1, 1, MAX_ASYNC_CALLS)
        timeout_ms = fields.number("timeout_ms", None, 1, MAX_TIMEOUT_MS)
        policy = fields.mapping("policy", None)
        return cls(
            protocol=protocol,
            method=method,
            params=params,
            mode=mode,
            count=count if mode == "async" else 1,
            # L'appel local n'emprunte aucun réseau : « via le proxy » n'a pas de sens pour lui.
            via_proxy=fields.boolean("via_proxy", False) and protocol != "local",
            timeout=None if timeout_ms is None else timeout_ms / 1000,
            policy=None if policy is None else Policy.parse(policy),
        )


@dataclass(frozen=True)
class InspectRequest:
    """Corps validé de ``POST /api/inspect``."""

    method: str
    params: dict[str, Any]
    protocols: tuple[str, ...]
    via_proxy: bool = False

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "InspectRequest":
        fields = Fields(data, ("method", "params", "protocols", "via_proxy"))
        method = fields.text("method")
        params = fields.mapping("params", {})
        spec = checked_params(method, params)
        if spec.kind in _RESULT_STREAMS:
            raise malformed(
                f"« {method} » renvoie un flux : l'inspection décompose un appel à réponse unique. Lancez le flux "
                "en mode « stream » puis relisez sa trace (GET /api/traces/{call_id})."
            )
        # Par défaut : les protocoles distants qui exposent la procédure — ce sont eux qui ont un fil à montrer.
        default = [name for name in REMOTE_PROTOCOLS if name in spec.protocols] or list(spec.protocols)
        protocols = fields.sequence("protocols", default)
        unknown = [name for name in protocols if name not in PROTOCOLS]
        if unknown or not protocols:
            raise malformed(f"« protocols » doit lister au moins un protocole parmi : {', '.join(PROTOCOLS)}.")
        return cls(method, params, tuple(dict.fromkeys(protocols)), fields.boolean("via_proxy", False))


def trace_payload(trace: CallTrace | None) -> dict[str, Any]:
    """Résumé et évènements d'un appel ; ``offset_us`` = microsecondes depuis son premier évènement."""
    if trace is None:
        return {"summary": None, "events": []}
    events = list(trace.events)     # copie : un évènement tardif du serveur peut encore s'ajouter
    origin = events[0].t_ns if events else 0
    return {
        "summary": trace.summary(),
        "events": [{**event.to_dict(), "offset_us": round((event.t_ns - origin) / 1000, 1)} for event in events],
    }


@dataclass(frozen=True)
class _Outcome:
    """Issue d'un appel vue du dashboard, avant lecture de sa trace."""

    result: Any
    error: RpcError | None
    call_id: str                    # identifiant du (dernier) appel émis ; "" si aucun n'a été tracé
    wall_ms: float                  # durée observée autour de l'appel
    attempts: int | None = None     # appel d'un lot avec politique : nombre de tentatives


class _Lane:
    """Un client longue durée et l'unique thread qui s'en sert."""

    def __init__(self, open_client: Callable[[], InventoryClient], name: str) -> None:
        self._open = open_client
        self._client: InventoryClient | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)

    @property
    def cold(self) -> bool:
        """Vrai tant que le client n'existe pas : le prochain appel ouvrira sa connexion, et la paiera."""
        return self._client is None

    @property
    def client(self) -> InventoryClient:
        """Le client de la voie, créé au premier besoin (à n'utiliser que depuis le thread de la voie)."""
        if self._client is None:
            self._client = self._open()
        return self._client

    def submit(self, work: Callable[["_Lane"], T]) -> "Future[T]":
        return self._executor.submit(work, self)

    def renew(self) -> None:
        """Abandonne le client courant : le prochain appel partira d'une connexion neuve.

        Après une coupure, un canal gRPC reste jusqu'à une seconde en attente de reconnexion
        et refuse tout appel : dans le dashboard, l'appel qui suit la fin d'une panne doit
        réussir aussitôt.
        """
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def close(self) -> None:
        self._executor.submit(self.renew)    # par le thread de la voie, une fois l'appel en cours terminé
        self._executor.shutdown(wait=False)


class CallService:
    """Exécute les appels demandés par ``/api/call`` et ``/api/inspect``."""

    def __init__(self, runtime: "LabRuntime", publish: Publish) -> None:
        self._runtime = runtime
        self._publish = publish
        self._lock = threading.Lock()
        self._lanes: dict[tuple[str, bool], _Lane] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._open_streams = 0
        self.latest_batches: dict[str, dict[str, Any]] = {}    # dernier lot asynchrone de chaque protocole

    # -- API (dans la boucle d'évènements) --------------------------------------------

    async def execute(self, request: CallRequest) -> dict[str, Any]:
        """Appel ``sync`` ou lot ``async`` : attend l'issue sans bloquer la boucle."""
        work = self._run_batch if request.mode == "async" else self._run_single
        return await self._on_lane(request.protocol, request.via_proxy, partial(work, request))

    async def inspect(self, request: InspectRequest) -> list[dict[str, Any]]:
        """Le même appel sur chaque protocole, l'un après l'autre pour que leurs mesures ne se gênent pas."""
        return [
            await self._on_lane(protocol, request.via_proxy, partial(self._run_inspection, request))
            for protocol in request.protocols
        ]

    def start_stream(self, request: CallRequest) -> str:
        """Ouvre un flux dans son propre thread ; renvoie aussitôt son identifiant."""
        with self._lock:
            if self._open_streams >= MAX_STREAMS:
                raise ApiError(
                    429, BUSY, f"Trop de flux en cours ({MAX_STREAMS}) : attendez la fin de l'un d'eux."
                )
            self._open_streams += 1
        stream_id = f"stream-{uuid.uuid4().hex[:8]}"
        threading.Thread(
            target=self._run_stream, args=(stream_id, request), name=f"dashboard-{stream_id}", daemon=True
        ).start()
        return stream_id

    def breakers(self) -> dict[str, dict[str, Any]]:
        """État des disjoncteurs partagés, par protocole (ceux qui ont déjà servi)."""
        with self._lock:
            return {protocol: breaker.snapshot() for protocol, breaker in self._breakers.items()}

    def reset(self) -> None:
        """Oublie l'état des disjoncteurs et les lots mesurés (remise à zéro du laboratoire)."""
        with self._lock:
            self._breakers.clear()
        self.latest_batches.clear()

    def close(self) -> None:
        with self._lock:
            lanes, self._lanes = list(self._lanes.values()), {}
            pool, self._pool = self._pool, None
        for lane in lanes:
            lane.close()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # -- voies ------------------------------------------------------------------------

    def _on_lane(self, protocol: str, via_proxy: bool, work: Callable[[_Lane], T]) -> "asyncio.Future[T]":
        via_proxy = via_proxy and protocol != "local"
        with self._lock:
            lane = self._lanes.get((protocol, via_proxy))
            if lane is None:
                name = f"dashboard-{protocol}{'-proxy' if via_proxy else ''}"
                open_client = partial(self._runtime.client, protocol, via_proxy=via_proxy)
                lane = self._lanes[protocol, via_proxy] = _Lane(open_client, name)
        return asyncio.wrap_future(lane.submit(work))

    def _fan_out_pool(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=ASYNC_WORKERS, thread_name_prefix="dashboard-async")
            return self._pool

    def _guarded(self, client: InventoryClient, policy: Policy | None) -> InventoryClient:
        """``client`` tel quel, ou enveloppé de la politique de résilience demandée."""
        if policy is None:
            return client
        breaker = None
        if policy.breaker:
            # Un disjoncteur par protocole, conservé d'un appel à l'autre : c'est sa mémoire des
            # échecs passés qui lui permet de refuser l'appel suivant sans toucher au réseau.
            with self._lock:
                breaker = self._breakers.setdefault(client.protocol, CircuitBreaker(name=client.protocol))
        return ResilientClient(
            client,
            retry=RetryPolicy(
                max_attempts=policy.max_attempts,
                base_delay_ms=policy.base_delay_ms,
                retry_non_idempotent=policy.retry_non_idempotent,
            ),
            breaker=breaker,
            auto_idempotency_key=policy.auto_idempotency_key,
            bus=self._runtime.bus,
        )

    def _trace(self, call_id: str) -> CallTrace | None:
        return self._runtime.collector.get(call_id) if call_id else None

    # -- appel synchrone (thread de la voie) ------------------------------------------

    def _run_single(self, request: CallRequest, lane: _Lane) -> dict[str, Any]:
        cold = lane.cold and request.protocol != "local"
        client = lane.client
        target = self._guarded(client, request.policy)
        outcome = _invoke(target, client, request.method, request.params, request.timeout)
        if isinstance(outcome.error, RpcTransportError):
            lane.renew()
        trace = self._trace(outcome.call_id)
        summary = trace.summary() if trace is not None else {}
        call_id = outcome.call_id if trace is not None else ""
        verdict = "ok" if outcome.error is None else "error"
        report: dict[str, Any] = {}
        if isinstance(target, ResilientClient):
            # Avec une politique, la durée est celle de l'appel LOGIQUE : tentatives et attentes comprises.
            report = target.last_report
            duration_ms = report.get("total_ms", outcome.wall_ms)
            attempts = report.get("attempts", [])
        else:
            # Sans politique : exactement une tentative, décrite comme le ferait le client résilient.
            duration_ms = _trace_ms(summary, outcome.wall_ms)
            attempts = [{
                "n": 1,
                "call_id": call_id,
                "outcome": verdict,
                "code": OK if outcome.error is None else outcome.error.code,
                "duration_ms": round(duration_ms, 3),
                "backoff_ms": 0.0,
            }]
        server_us = summary.get("server_us")
        return {
            "ok": outcome.error is None,
            "mode": "sync",
            "protocol": request.protocol,
            "method": request.method,
            "via_proxy": request.via_proxy,
            "result": outcome.result,
            "error": None if outcome.error is None else outcome.error.to_dict(),
            "call_id": call_id,
            "duration_ms": round(duration_ms, 3),
            "request_bytes": summary.get("request_bytes"),
            "response_bytes": summary.get("response_bytes"),
            "server_ms": None if server_us is None else round(server_us / 1000, 3),
            # Vrai si l'appel a ouvert la connexion (premier appel de la voie, ou suivant une panne) :
            # sa durée comprend alors cette ouverture.
            "cold": cold,
            "attempts": attempts,
            "outcome": report.get("outcome", verdict),
            "breaker": report.get("breaker"),
        }

    # -- lot asynchrone (thread de la voie) -------------------------------------------

    def _run_batch(self, request: CallRequest, lane: _Lane) -> dict[str, Any]:
        cold = lane.cold and request.protocol != "local"
        client = lane.client
        multiplexed = request.protocol == "custom" and request.policy is None
        started = time.perf_counter()
        outcomes = self._pipelined(client, request, started) if multiplexed else self._threaded(client, request)
        wall_ms = (time.perf_counter() - started) * 1000
        if any(isinstance(outcome.error, RpcTransportError) for outcome in outcomes):
            lane.renew()
        calls = [self._batch_entry(outcome) for outcome in outcomes]
        sum_ms = sum(call["duration_ms"] for call in calls)
        errors = sum(not call["ok"] for call in calls)
        batch = {
            "ok": errors == 0,
            "mode": "async",
            "protocol": request.protocol,
            "method": request.method,
            "via_proxy": request.via_proxy,
            "count": request.count,
            "errors": errors,
            "wall_ms": round(wall_ms, 3),
            "sum_ms": round(sum_ms, 3),
            # > 1 : les appels se sont chevauchés dans le temps ; ≈ 1 : ils se sont succédé.
            "speedup": round(sum_ms / wall_ms, 2) if wall_ms > 0 else None,
            "cold": cold,
            "strategy": _ASYNC_RETRIES if request.policy is not None else _ASYNC_STRATEGIES[request.protocol],
        }
        if request.policy is None:
            self.latest_batches[request.protocol] = batch   # preuve chiffrée du multiplexage, pour le bilan
        return {**batch, "calls": calls}

    def _pipelined(self, client: InventoryClient, request: CallRequest, started: float) -> list[_Outcome]:
        """Stub maison : ``submit`` émet chaque requête sans attendre la réponse de la précédente."""
        futures = [
            client.submit(request.method, dict(request.params), timeout=request.timeout)
            for _ in range(request.count)
        ]
        outcomes = []
        for future in futures:
            error: RpcError | None = None
            try:
                future.result()
            except RpcError as exc:
                error = exc
            outcomes.append(
                _Outcome(None, error, getattr(future, "call_id", ""), (time.perf_counter() - started) * 1000)
            )
        return outcomes

    def _threaded(self, client: InventoryClient, request: CallRequest) -> list[_Outcome]:
        """Autres clients : un appel bloquant par thread, tous sur le client (donc le canal) de la voie."""
        call_ids: dict[int, str] = {}

        def remember(event: TraceEvent) -> None:
            # ``last_call_id`` n'a pas de sens entre plusieurs threads. Mais le bus appelle ses abonnés
            # dans le thread émetteur : l'étape « client.call » vue ici est celle de l'appel de CE thread.
            if event.stage == "client.call" and event.protocol == client.protocol:
                call_ids[threading.get_ident()] = event.call_id

        def one_call() -> _Outcome:
            me = threading.get_ident()
            call_ids.pop(me, None)
            target = self._guarded(client, request.policy)   # un client résilient par appel logique
            started = time.perf_counter()
            error: RpcError | None = None
            try:
                target.invoke(request.method, dict(request.params), timeout=request.timeout)
            except RpcError as exc:
                error = exc
            wall_ms = (time.perf_counter() - started) * 1000
            attempts = len(target.last_report.get("attempts", ())) if isinstance(target, ResilientClient) else None
            return _Outcome(None, error, call_ids.pop(me, ""), wall_ms, attempts)

        pool = self._fan_out_pool()
        unsubscribe = self._runtime.bus.subscribe(remember)
        try:
            futures = [pool.submit(one_call) for _ in range(request.count)]
            return [future.result() for future in futures]
        finally:
            unsubscribe()

    def _batch_entry(self, outcome: _Outcome) -> dict[str, Any]:
        trace = self._trace(outcome.call_id)
        entry: dict[str, Any] = {
            "call_id": outcome.call_id if trace is not None else "",
            "ok": outcome.error is None,
            "error": None if outcome.error is None else outcome.error.to_dict(),
        }
        if outcome.attempts is None:
            entry["duration_ms"] = round(_trace_ms(trace.summary() if trace is not None else {}, outcome.wall_ms), 3)
        else:
            entry["duration_ms"] = round(outcome.wall_ms, 3)    # appel logique : tentatives et attentes comprises
            entry["attempts"] = outcome.attempts
        return entry

    # -- inspection (thread de la voie) -----------------------------------------------

    def _run_inspection(self, request: InspectRequest, lane: _Lane) -> dict[str, Any]:
        if lane.cold:
            self._warm_up(lane)
        cold = lane.cold            # encore vrai si l'échauffement a perdu sa connexion
        client = lane.client
        outcome = _invoke(client, client, request.method, request.params, None)
        if isinstance(outcome.error, RpcTransportError):
            lane.renew()
        trace = self._trace(outcome.call_id)
        return {
            "protocol": client.protocol,
            "ok": outcome.error is None,
            "result": outcome.result,
            "error": None if outcome.error is None else outcome.error.to_dict(),
            "call_id": outcome.call_id if trace is not None else "",
            "cold": cold and client.protocol != "local",
            **trace_payload(trace),
        }

    def _warm_up(self, lane: _Lane) -> None:
        """Ouvre la connexion de la voie par un appel non tracé, juste avant l'appel inspecté.

        Sans lui, l'inspection d'une voie neuve paierait l'ouverture de la connexion (pour gRPC,
        l'établissement du canal HTTP/2) : plusieurs millisecondes qu'un seul protocole subirait,
        et la comparaison des trois serait faussée. Bus coupé : ni trace, ni compteur, ni
        message sur le WebSocket.
        """
        client = lane.client
        if client.protocol == "local":
            return
        with self._runtime.bus.muted():
            outcome = _invoke(client, client, _WARM_UP_METHOD, _WARM_UP_PARAMS, _WARM_UP_TIMEOUT_S)
        if isinstance(outcome.error, RpcTransportError):
            lane.renew()

    # -- flux (thread dédié) ------------------------------------------------------------

    def _run_stream(self, stream_id: str, request: CallRequest) -> None:
        """Consomme un flux et pousse chaque élément sur le WebSocket, puis ``stream_end``.

        Le flux a son propre client : il peut durer longtemps, et les appels unaires lancés
        pendant ce temps (par exemple un ``update_stock`` dont on veut voir l'effet dans le
        flux d'indicateurs) ne doivent pas attendre derrière lui.
        """
        started = time.perf_counter()

        def elapsed_ms() -> float:
            return round((time.perf_counter() - started) * 1000, 3)

        count = 0
        result: Any = None
        error: dict[str, Any] | None = None
        call_id = ""
        client: InventoryClient | None = None
        report: dict[str, Any] = {}
        try:
            client = self._runtime.client(request.protocol, via_proxy=request.via_proxy)
            target = self._guarded(client, request.policy)
            before = client.last_call_id
            try:
                produced = target.invoke(request.method, dict(request.params), timeout=request.timeout)
                if isinstance(produced, Iterator):
                    for item in produced:
                        count += 1
                        self._publish("stream", {
                            "type": "stream", "stream_id": stream_id, "seq": count, "item": item,
                            "elapsed_ms": elapsed_ms(),
                        })
                else:
                    result = produced       # flux client : plusieurs requêtes, un bilan unique
            finally:
                if client.last_call_id != before and self._trace(client.last_call_id) is not None:
                    call_id = client.last_call_id
                if isinstance(target, ResilientClient):
                    report = target.last_report
        except RpcError as exc:
            error = exc.to_dict()
        except Exception as exc:    # quoi qu'il arrive, l'interface doit recevoir « stream_end »
            _log.exception("Flux %s interrompu par une erreur inattendue", stream_id)
            error = {"type": type(exc).__name__, "code": "INTERNAL", "message": str(exc)}
        finally:
            if client is not None:
                client.close()
            with self._lock:
                self._open_streams -= 1
        self._publish("stream", {
            "type": "stream_end",
            "stream_id": stream_id,
            "protocol": request.protocol,
            "method": request.method,
            "count": count,
            "duration_ms": elapsed_ms(),
            "result": result,
            "error": error,
            "call_id": call_id,
            "attempts": report.get("attempts"),
        })


def _invoke(
    target: InventoryClient, tracked: InventoryClient, method: str, params: dict[str, Any], timeout: float | None
) -> _Outcome:
    """Appelle ``method`` sur ``target`` ; l'identifiant de l'appel est relevé sur ``tracked``.

    ``tracked`` est le client qui parle réellement au réseau (``target`` peut l'envelopper).
    Bus coupé, certains clients ne renouvellent pas ``last_call_id`` : un identifiant
    inchangé signifie « appel non tracé », pas « même appel que le précédent ».
    """
    before = tracked.last_call_id
    started = time.perf_counter()
    result: Any = None
    error: RpcError | None = None
    try:
        result = target.invoke(method, dict(params), timeout=timeout)
    except RpcError as exc:
        error = exc
    wall_ms = (time.perf_counter() - started) * 1000
    call_id = tracked.last_call_id if tracked.last_call_id != before else ""
    return _Outcome(result, error, call_id, wall_ms)


def _trace_ms(summary: dict[str, Any], fallback_ms: float) -> float:
    """Durée mesurée par le stub (étape terminale de la trace) ; à défaut, celle observée autour de l'appel."""
    duration_us = summary.get("duration_us")
    return fallback_ms if duration_us is None else duration_us / 1000
