"""Banc d'essai de performance : ce qu'un appel distant coûte, en octets et en temps.

    python -m benchmark_lab.benchmark_perf [--quick] [--iterations N]

Quatre mesures, toutes prises sur le laboratoire en marche — rien n'est supposé :

* **tailles** — un appel tracé par protocole : les traces donnent la taille du
  message sérialisé et celle des octets confiés à la socket ; les compteurs des
  proxys donnent ensuite ce qui a VRAIMENT traversé TCP (HTTP/2 et en-têtes compris) ;
* **sérialisation** — encodage et décodage des mêmes données en JSON et en Protobuf ;
* **latence** — la même boucle d'appels sur chaque protocole : bus de traces coupé,
  connexion déjà ouverte, après échauffement, chaque appel chronométré séparément ;
* **réseau** — la même boucle encore, à travers les proxys, avec de plus en plus de latence.

Les « faits marquants » sont calculés à partir de ces mesures : aucun vainqueur
n'est écrit d'avance. Client et serveurs partagent ici un seul processus Python
(donc un seul GIL) : les valeurs absolues sont celles de cette machine, ce sont
les écarts entre protocoles et les ordres de grandeur qui se comparent.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import sys
import threading
import time
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from itertools import repeat
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping, Sequence, TextIO

import google.protobuf
import grpc

from common.catalog import method_spec
from common.client_api import InventoryClient
from common.config import APP_NAME, PROTOCOL_LABELS, PROTOCOLS, REMOTE_PROTOCOLS
from common.errors import DomainError, RpcError
from common.inventory import InventoryService
from common.telemetry import TraceEvent
from rpc_custom.protocol import HEADER_SIZE as JSONRPC_FRAME_HEADER
from rpc_grpc import converters as conv
from rpc_grpc.generated import service_pb2 as pb
from rpc_grpc.wire_inspector import FRAME_HEADER_SIZE as GRPC_FRAME_HEADER

from .report import fr_bytes, fr_compact, fr_duration, fr_number, fr_percent, fr_ratio, save_report, to_text

if TYPE_CHECKING:
    from lab import LabRuntime

ProgressCallback = Callable[[str, float, str, "dict[str, Any] | None"], None]

SUITES: tuple[str, ...] = ("payload", "serialization", "latency", "network")
SUITE_LABELS: dict[str, str] = {
    "payload": "Tailles des messages",
    "serialization": "Sérialisation JSON vs Protobuf",
    "latency": "Latence par protocole",
    "network": "Local vs distant selon la latence du réseau",
    "done": "Banc d'essai terminé",
}

REFERENCE_PRODUCT = "SKU-1001"

# Paramètres par défaut des procédures mesurables (les quatre appels unaires communs aux
# quatre protocoles). « delta » vaut +1 : avec −1, mille appels videraient le stock et la
# mesure tournerait à la série d'erreurs. Le stock est de toute façon rétabli en sortie.
DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "calculate_factorial": {"n": 20},
    "get_product_details": {"product_id": REFERENCE_PRODUCT},
    "update_stock": {"product_id": REFERENCE_PRODUCT, "delta": 1},
    "list_products": {"limit": 20},
}
PAYLOAD_CALLS: tuple[tuple[str, dict[str, Any]], ...] = tuple(DEFAULT_PARAMS.items())

WIRE_METHOD = "get_product_details"        # appel dont on compte les octets réels sur TCP
WIRE_CALLS = 20
SCALING_LIMITS: tuple[int, ...] = (1, 10, 100, 1000)
SERIALIZATION_LIST_SIZE = 100
HISTOGRAM_BUCKETS = 32
HISTOGRAM_CLIP_PERCENTILE = 99.5           # la queue au-delà écraserait toute la distribution sur un seul bac
MAX_SAMPLES = 400
PROGRESS_TICKS = 20                        # notifications de progression par protocole mesuré, au plus
MAX_CONSECUTIVE_ERRORS = 5
MAX_ITERATIONS = 1_000_000
MAX_CONCURRENCY = 64

DEFAULT_CONFIG: dict[str, Any] = {
    "suites": list(SUITES),
    "method": "get_product_details",
    "params": None,                         # None : DEFAULT_PARAMS de la procédure
    "protocols": list(PROTOCOLS),
    "iterations": 1000,
    "warmup": 50,
    "concurrency": 1,
    "via_proxy": False,
    "serialization_iterations": 2000,
    "sweep_latencies_ms": [0, 10, 50, 100, 200],
    "sweep_iterations": 20,
    "wire_calls": WIRE_CALLS,
    "scaling_limits": list(SCALING_LIMITS),
}
QUICK_CONFIG: dict[str, Any] = {
    **DEFAULT_CONFIG,
    "iterations": 200,
    "warmup": 20,
    "serialization_iterations": 300,
    "sweep_latencies_ms": [0, 20, 50],
    "sweep_iterations": 8,
}

_MS_DECIMALS = 6                 # 1 ns : la résolution de perf_counter_ns
_LOOP_TICKS = PROGRESS_TICKS - 2   # deux notifications sont réservées à l'ouverture et à la clôture d'une mesure
_TIMING_PASSES = 3               # passes de chronométrage par opération de sérialisation (on garde la meilleure)
_LIST_ITERATION_DIVISOR = 20     # la liste de 100 produits coûte ~100 fois la fiche : on la mesure moins souvent
_WIRE_WARMUP = 3
_SWEEP_WARMUP = 2
_CPU_WAKE_S = 0.03               # échauffement minimal : le temps que le processeur quitte son régime d'attente
_SWEEP_BASE_COST_MS = 5.0        # poids d'un point de balayage dans la progression, hors latence ajoutée
_SETTLE_STEP_S = 0.01
_SETTLE_TIMEOUT_S = 0.3
_POLL_S = 0.05
_MAX_STOCK_DELTA = 1_000_000     # borne de « delta » acceptée par InventoryService.update_stock
_MAX_LIST_LIMIT = 5000           # borne de « limit » acceptée par InventoryService.list_products
_SUITE_WEIGHTS = {"payload": 1.0, "serialization": 1.0, "latency": 3.0, "network": 3.0}
_FRAME_HEADERS = {"custom": JSONRPC_FRAME_HEADER, "grpc": GRPC_FRAME_HEADER}
_STAT_KEYS = ("mean_ms", "median_ms", "p90_ms", "p95_ms", "p99_ms", "min_ms", "max_ms", "stdev_ms")
_JSON = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))   # le réglage des deux middlewares JSON


# --- Validation et progression -----------------------------------------------

def resolve_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Configuration complète et validée : ``config`` complète ``DEFAULT_CONFIG``.

    Une valeur ``None`` laisse la valeur par défaut. Clé inconnue, protocole ou
    procédure inconnus, nombre hors bornes : ``ValueError``.
    """
    supplied = dict(config or {})
    unknown = sorted(set(supplied) - set(DEFAULT_CONFIG))
    if unknown:
        raise ValueError(
            f"Option(s) de banc d'essai inconnue(s) : {', '.join(unknown)} (attendues : {', '.join(DEFAULT_CONFIG)})"
        )
    settings = {**DEFAULT_CONFIG, **{key: value for key, value in supplied.items() if value is not None}}
    suites = _as_tuple("suites", settings["suites"])
    if not suites or any(suite not in SUITES for suite in suites):
        raise ValueError(f"Suite inconnue dans {list(suites)!r} (attendu : {', '.join(SUITES)})")
    if not isinstance(settings["via_proxy"], bool):
        raise ValueError(f"« via_proxy » doit être un booléen (reçu : {settings['via_proxy']!r})")
    settings.update(
        suites=[suite for suite in SUITES if suite in suites],
        params=_resolve_params(settings["method"], settings["params"]),
        protocols=list(_resolve_protocols(settings["protocols"])),
        iterations=_require_int("iterations", settings["iterations"], 1, MAX_ITERATIONS),
        warmup=_require_int("warmup", settings["warmup"], 0, MAX_ITERATIONS),
        concurrency=_require_int("concurrency", settings["concurrency"], 1, MAX_CONCURRENCY),
        serialization_iterations=_require_int(
            "serialization_iterations", settings["serialization_iterations"], 1, MAX_ITERATIONS
        ),
        sweep_latencies_ms=_resolve_latencies(settings["sweep_latencies_ms"]),
        sweep_iterations=_require_int("sweep_iterations", settings["sweep_iterations"], 1, MAX_ITERATIONS),
        wire_calls=_require_int("wire_calls", settings["wire_calls"], 1, MAX_ITERATIONS),
        scaling_limits=_resolve_limits(settings["scaling_limits"]),
    )
    return settings


def _require_started(runtime: "LabRuntime") -> None:
    if not runtime.started:
        raise RuntimeError("Le laboratoire doit être démarré (LabRuntime.start()) avant toute mesure.")


def _require_int(name: str, value: Any, minimum: int, maximum: int) -> int:
    # bool est un sous-type d'int : « iterations=True » passerait sans ce garde-fou.
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"« {name} » doit être un entier compris entre {minimum} et {maximum} (reçu : {value!r})")
    return value


def _as_tuple(name: str, values: Any) -> tuple[Any, ...]:
    if isinstance(values, str):
        return (values,)
    try:
        return tuple(values)
    except TypeError:
        raise ValueError(f"« {name} » doit être une liste (reçu : {values!r})") from None


def _resolve_protocols(protocols: Iterable[str] | str) -> tuple[str, ...]:
    names = _as_tuple("protocols", protocols)
    if not names:
        raise ValueError(f"Aucun protocole à mesurer (attendu : {', '.join(PROTOCOLS)})")
    for name in names:
        if not isinstance(name, str) or name not in PROTOCOLS:
            raise ValueError(f"Protocole inconnu : {name!r} (attendu : {', '.join(PROTOCOLS)})")
    return tuple(dict.fromkeys(names))


def _resolve_params(method: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Arguments de l'appel mesuré : ceux de l'appelant, complétés par ``DEFAULT_PARAMS``."""
    if not isinstance(method, str) or method not in DEFAULT_PARAMS:
        raise ValueError(
            f"Procédure inconnue ou non mesurable : {method!r} (attendu, appels unaires : {', '.join(DEFAULT_PARAMS)})"
        )
    if params is not None and not isinstance(params, Mapping):
        raise ValueError(f"« params » doit être un dictionnaire d'arguments (reçu : {params!r})")
    resolved = {**DEFAULT_PARAMS[method], **(params or {})}
    unknown = sorted(set(resolved) - {spec.name for spec in method_spec(method).params})
    if unknown:
        raise ValueError(f"Paramètre(s) inconnu(s) pour {method} : {', '.join(map(str, unknown))}")
    return resolved


def _resolve_latencies(latencies_ms: Iterable[float]) -> list[float]:
    values = _as_tuple("latencies_ms", latencies_ms)
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Une latence est un nombre de millisecondes ≥ 0 (reçu : {value!r})")
    if not values:
        raise ValueError("Le balayage réseau demande au moins une latence")
    return list(values)


def _resolve_limits(limits: Iterable[int]) -> list[int]:
    values = _as_tuple("scaling_limits", limits)
    return [_require_int("scaling_limits", value, 1, _MAX_LIST_LIMIT) for value in values]


def _emitter(progress: ProgressCallback | None, phase: str) -> Callable[..., None]:
    """Rappel de progression d'une phase ; sans effet si personne n'écoute."""

    def emit(fraction: float, message: str, partial_result: dict[str, Any] | None = None) -> None:
        if progress is not None:
            progress(phase, round(min(max(fraction, 0.0), 1.0), 4), message, partial_result)

    return emit


def _scaled(progress: ProgressCallback | None, start: float, span: float) -> ProgressCallback | None:
    """Replace la progression d'une suite (0..1) dans sa tranche de la progression globale."""
    if progress is None:
        return None

    def forward(phase: str, fraction: float, message: str, partial_result: dict[str, Any] | None = None) -> None:
        progress(phase, round(start + span * fraction, 4), message, partial_result)

    return forward


# --- Tailles des messages ----------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Exchange:
    """Tailles d'un appel relevées dans sa trace (octets)."""

    request_message: int     # message sérialisé seul (corps JSON-RPC, message Protobuf, corps JSON de REST)
    request_wire: int        # ce que le client confie à la socket : trame, ou requête HTTP entière
    response_message: int
    response_wire: int


def measure_payload_sizes(
    runtime: "LabRuntime",
    *,
    scaling_limits: Iterable[int] = SCALING_LIMITS,
    wire_calls: int = WIRE_CALLS,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Taille des requêtes et des réponses, par procédure et par protocole.

    * ``rows`` — un appel tracé par protocole : ``json_bytes`` / ``protobuf_bytes``
      sont les messages sérialisés seuls, ``json_rpc_bytes`` / ``grpc_bytes`` /
      ``rest_bytes`` ce que le client écrit sur la socket (préfixe de 4 ou de
      5 octets, ou message HTTP complet) — les tailles affichées par « Sous le capot » ;
    * ``wire`` — octets réellement relayés par les proxys, par appel, sur une
      connexion déjà ouverte : seule mesure qui voit HTTP/2 (trames, HPACK) ;
    * ``scaling`` — réponse de ``list_products`` pour un nombre croissant de produits.

    ``protobuf_vs_json_pct`` compare les deux messages seuls : négatif quand
    Protobuf est plus petit. Un protocole dont l'appel échoue laisse ``None``.
    """
    _require_started(runtime)
    if not runtime.bus.enabled:
        raise RuntimeError("Les tailles se lisent dans les traces : le bus de télémétrie doit être actif.")
    limits = _resolve_limits(scaling_limits)
    _require_int("wire_calls", wire_calls, 1, MAX_ITERATIONS)
    emit = _emitter(progress, "payload")
    section: dict[str, Any] = {"rows": [], "wire": [], "scaling": []}
    steps = len(PAYLOAD_CALLS) + len(REMOTE_PROTOCOLS) + len(limits)
    clients = {protocol: runtime.client(protocol) for protocol in REMOTE_PROTOCOLS}
    try:
        with _stock_preserved(runtime.service, PAYLOAD_CALLS):
            for step, (method, params) in enumerate(PAYLOAD_CALLS):
                emit(step / steps, f"Tailles des messages : {method}")
                exchanges = {
                    protocol: _traced_exchange(runtime, client, method, params)
                    for protocol, client in clients.items()
                }
                title = method_spec(method).title
                section["rows"] = [*section["rows"], *(
                    {"method": method, "title": title, "direction": direction, **_sizes(exchanges, direction)}
                    for direction in ("request", "response")
                )]
            for step, protocol in enumerate(REMOTE_PROTOCOLS, len(PAYLOAD_CALLS)):
                label = PROTOCOL_LABELS[protocol]
                emit(step / steps, f"Octets réels sur TCP : {label}", {"payload": dict(section)})
                section["wire"] = [*section["wire"], _wire_row(runtime, protocol, wire_calls)]
            for step, limit in enumerate(limits, len(PAYLOAD_CALLS) + len(REMOTE_PROTOCOLS)):
                emit(step / steps, f"Montée en charge : {fr_number(limit)} produits", {"payload": dict(section)})
                exchanges = {
                    protocol: _traced_exchange(runtime, client, "list_products", {"limit": limit})
                    for protocol, client in clients.items()
                }
                section["scaling"] = [*section["scaling"], {"items": limit, **_sizes(exchanges, "response")}]
    finally:
        for client in clients.values():
            client.close()
    emit(1.0, "Tailles des messages mesurées", {"payload": dict(section)})
    return section


def _traced_exchange(
    runtime: "LabRuntime", client: InventoryClient, method: str, params: Mapping[str, Any]
) -> _Exchange | None:
    """Exécute un appel tracé et relit, dans le collecteur, les tailles de ses étapes client."""
    protocol = client.protocol
    try:
        client.invoke(method, dict(params))
    except RpcError:
        return None
    trace = runtime.collector.get(client.last_call_id)
    stages: dict[str, TraceEvent] = {event.stage: event for event in trace.events} if trace else {}
    try:
        marshal, send, receive = (stages[stage] for stage in ("client.marshal", "client.send", "client.receive"))
    except KeyError as missing:
        raise RuntimeError(
            f"Trace incomplète pour {method} via {protocol} (étape {missing.args[0]} absente) : "
            "le bus de télémétrie a-t-il été coupé pendant la mesure ?"
        ) from None
    if protocol in _FRAME_HEADERS:
        response_message = receive.size - _FRAME_HEADERS[protocol]
    else:
        # REST : le message utile est le corps JSON, repéré par les segments de la trace.
        body = next((segment for segment in receive.detail.get("segments", ()) if segment["kind"] == "body"), None)
        response_message = body["end"] - body["start"] if body else 0
    return _Exchange(marshal.size, send.size, response_message, receive.size)


def _sizes(exchanges: Mapping[str, _Exchange | None], direction: str) -> dict[str, int | float | None]:
    """Colonnes de taille d'une ligne, pour un sens de l'échange."""

    def size(protocol: str, part: str) -> int | None:
        exchange = exchanges.get(protocol)
        return None if exchange is None else getattr(exchange, f"{direction}_{part}")

    json_bytes, protobuf_bytes = size("custom", "message"), size("grpc", "message")
    return {
        "json_bytes": json_bytes,
        "json_rpc_bytes": size("custom", "wire"),
        "protobuf_bytes": protobuf_bytes,
        "grpc_bytes": size("grpc", "wire"),
        "rest_bytes": size("rest", "wire"),
        "rest_body_bytes": size("rest", "message"),
        "protobuf_vs_json_pct": _relative_pct(protobuf_bytes, json_bytes),
    }


def _relative_pct(value: float | None, reference: float | None) -> float | None:
    """Écart relatif en % : négatif si ``value`` est plus petit que ``reference``."""
    if value is None or not reference:
        return None
    return round((value - reference) / reference * 100, 1)


def _wire_row(runtime: "LabRuntime", protocol: str, calls: int) -> dict[str, Any]:
    """Octets relayés par le proxy de ``protocol`` pour ``calls`` appels identiques, ramenés à un appel.

    Mesuré comme la latence — bus coupé, connexion ouverte par quelques appels
    préalables — et sur un réseau idéal : on compte des octets, pas des pannes.
    """
    proxy = runtime.proxies[protocol]
    saved = runtime.conditions.snapshot()
    runtime.conditions.reset()
    try:
        with runtime.bus.muted(), runtime.client(protocol, via_proxy=True) as client:
            call = _bound_call(client, WIRE_METHOD, DEFAULT_PARAMS[WIRE_METHOD])
            _warm_up(call, _WIRE_WARMUP)
            before = _settled_stats(proxy)
            batch = _timed_calls(call, calls, threading.Event(), _ignore_tick)
            after = _settled_stats(proxy)
    finally:
        runtime.conditions.update(**saved)
    succeeded = len(batch.durations_ns)
    row: dict[str, Any] = {
        "protocol": protocol,
        "label": PROTOCOL_LABELS[protocol],
        "method": WIRE_METHOD,
        "calls": succeeded,
        "bytes_up_per_call": None,
        "bytes_down_per_call": None,
        "total_per_call": None,
    }
    if succeeded == calls:   # un seul appel en échec fausserait la moyenne : mieux vaut ne rien publier
        up = round((after["bytes_up"] - before["bytes_up"]) / calls, 1)
        down = round((after["bytes_down"] - before["bytes_down"]) / calls, 1)
        # Somme des valeurs arrondies : le total affiché est celui des deux colonnes affichées.
        row.update(bytes_up_per_call=up, bytes_down_per_call=down, total_per_call=round(up + down, 1))
    return row


def _settled_stats(proxy: Any) -> dict[str, int]:
    """Compteurs du proxy une fois le trafic retombé.

    Après le retour d'un appel, gRPC échange encore des trames de service
    (WINDOW_UPDATE, PING) : on attend que les compteurs d'octets ne bougent plus.
    """
    deadline = time.perf_counter() + _SETTLE_TIMEOUT_S
    stats = proxy.stats()
    while time.perf_counter() < deadline:
        time.sleep(_SETTLE_STEP_S)
        latest = proxy.stats()
        if (latest["bytes_up"], latest["bytes_down"]) == (stats["bytes_up"], stats["bytes_down"]):
            return latest
        stats = latest
    return stats


@contextmanager
def _stock_preserved(service: InventoryService, calls: Iterable[tuple[str, Mapping[str, Any]]]) -> Iterator[None]:
    """Rétablit en sortie le stock des produits touchés par les ``update_stock`` mesurés.

    Mesurer une écriture ne doit pas dérégler l'inventaire que le reste du
    laboratoire (dashboard, scénarios de pannes) est en train d'observer.
    """
    before = [
        (params.get("product_id"), _stock(service, params.get("product_id")))
        for method, params in calls if method == "update_stock"
    ]
    try:
        yield
    finally:
        for product_id, initial in before:
            current = _stock(service, product_id)
            remaining = 0 if initial is None or current is None else initial - current
            while remaining:
                step = max(-_MAX_STOCK_DELTA, min(_MAX_STOCK_DELTA, remaining))
                service.update_stock(product_id, step)
                remaining -= step


def _stock(service: InventoryService, product_id: Any) -> int | None:
    try:
        return service.check_stock(product_id)["stock"]
    except DomainError:   # produit inconnu : les appels mesurés échoueront, il n'y aura rien à rétablir
        return None


# --- Sérialisation -----------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Subject:
    """Une donnée métier à sérialiser, et le message du contrat qui lui correspond."""

    label: str
    items: int
    value: dict[str, Any]
    to_proto: Callable[[dict[str, Any]], Any]
    from_proto: Callable[[Any], dict[str, Any]]
    message_class: Any
    loops: int


def measure_serialization(iterations: int = 2000, *, progress: ProgressCallback | None = None) -> dict[str, Any]:
    """Temps d'encodage et de décodage d'une fiche produit et d'une liste de 100 produits.

    ``encode_ns`` / ``decode_ns`` mesurent le codec seul : dictionnaire ⇄ octets
    pour JSON, message ⇄ octets pour Protobuf. ``encode_from_dict_ns`` /
    ``decode_to_dict_ns`` partent du dictionnaire métier et y reviennent : pour
    Protobuf, ils ajoutent la conversion champ par champ (``rpc_grpc.converters``)
    que ce laboratoire fait en Python ; pour JSON, ce sont les mêmes valeurs.
    La liste est mesurée sur ``iterations // 20`` tours (``iterations`` dans chaque ligne).
    """
    _require_int("iterations", iterations, 1, MAX_ITERATIONS)
    emit = _emitter(progress, "serialization")
    service = InventoryService()   # catalogue initial : les mêmes données à chaque exécution
    subjects = (
        _Subject(
            "Fiche produit", 1, service.get_product_details(REFERENCE_PRODUCT),
            conv.product_to_proto, conv.product_from_proto, pb.Product, iterations,
        ),
        _Subject(
            f"Liste de {SERIALIZATION_LIST_SIZE} produits", SERIALIZATION_LIST_SIZE,
            service.list_products(SERIALIZATION_LIST_SIZE),
            conv.product_list_to_proto, conv.product_list_from_proto, pb.ProductList,
            max(1, iterations // _LIST_ITERATION_DIVISOR),
        ),
    )
    rows: list[dict[str, Any]] = []
    for index, subject in enumerate(subjects):
        emit(index / len(subjects), f"Sérialisation : {subject.label.lower()}")
        rows += _serialization_rows(subject)
    section = {"iterations": iterations, "rows": rows}
    emit(1.0, "Sérialisation mesurée", {"serialization": section})
    return section


def _serialization_rows(subject: _Subject) -> list[dict[str, Any]]:
    value, loops = subject.value, subject.loops
    json_data = _json_encode(value)
    message = subject.to_proto(value)
    proto_data = message.SerializeToString()
    parse = subject.message_class.FromString
    json_encode = _time_per_call(partial(_json_encode, value), loops)
    json_decode = _time_per_call(partial(json.loads, json_data), loops)
    common = {
        "message": subject.label,
        "message_type": subject.message_class.DESCRIPTOR.name,
        "items": subject.items,
        "iterations": loops,
    }
    return [
        {
            **common,
            "format": "json",
            "size_bytes": len(json_data),
            "encode_ns": json_encode,
            "decode_ns": json_decode,
            "encode_from_dict_ns": json_encode,
            "decode_to_dict_ns": json_decode,
        },
        {
            **common,
            "format": "protobuf",
            "size_bytes": len(proto_data),
            "encode_ns": _time_per_call(message.SerializeToString, loops),
            "decode_ns": _time_per_call(partial(parse, proto_data), loops),
            "encode_from_dict_ns": _time_per_call(lambda: subject.to_proto(value).SerializeToString(), loops),
            "decode_to_dict_ns": _time_per_call(lambda: subject.from_proto(parse(proto_data)), loops),
        },
    ]


def _json_encode(value: Any) -> bytes:
    return _JSON.encode(value).encode("utf-8")


def _time_per_call(func: Callable[[], Any], loops: int) -> float:
    """Durée d'un appel à ``func``, en nanosecondes : la meilleure de plusieurs passes.

    Chaque passe est chronométrée d'un bloc — un encodage Protobuf isolé ne dure
    que quelques pas de l'horloge (100 ns sous Windows) — et le minimum écarte
    les passes perturbées par un autre thread ou par le ramasse-miettes.
    """
    per_pass = max(1, loops // _TIMING_PASSES)
    best = math.inf
    for _ in range(_TIMING_PASSES):
        started = time.perf_counter_ns()
        for _ in repeat(None, per_pass):
            func()
        best = min(best, (time.perf_counter_ns() - started) / per_pass)
    return round(best, 1)


# --- Latence -----------------------------------------------------------------

@dataclass(slots=True)
class _Batch:
    """Résultat brut d'une boucle chronométrée (un client, un thread)."""

    starts_ns: list[int] = field(default_factory=list)       # instant de départ de chaque appel réussi
    durations_ns: list[int] = field(default_factory=list)    # durée du même appel
    errors: int = 0
    last_error: RpcError | None = None
    aborted: bool = False
    started_ns: int = 0
    ended_ns: int = 0
    paused_ns: int = 0                                       # temps passé à notifier la progression


@dataclass(frozen=True, slots=True)
class _Run:
    """Mesure d'un protocole, tous clients confondus."""

    protocol: str
    durations_ns: list[int]      # appels réussis, dans l'ordre chronologique
    errors: int
    last_error: RpcError | None
    aborted: bool
    wall_ns: int                 # durée de la boucle, du premier départ à la dernière arrivée

    @classmethod
    def merge(cls, protocol: str, batches: Sequence[_Batch]) -> "_Run":
        if len(batches) == 1:
            durations = batches[0].durations_ns
        else:
            timeline = sorted(
                pair for batch in batches for pair in zip(batch.starts_ns, batch.durations_ns)
            )
            durations = [duration for _, duration in timeline]
        failed = [batch.last_error for batch in batches if batch.last_error is not None]
        wall_ns = max(batch.ended_ns for batch in batches) - min(batch.started_ns for batch in batches)
        return cls(
            protocol=protocol,
            durations_ns=durations,
            errors=sum(batch.errors for batch in batches),
            last_error=failed[-1] if failed else None,
            aborted=any(batch.aborted for batch in batches),
            wall_ns=max(wall_ns - max(batch.paused_ns for batch in batches), 0),
        )


def run_latency_benchmark(
    runtime: "LabRuntime",
    *,
    method: str = "get_product_details",
    params: Mapping[str, Any] | None = None,
    protocols: Iterable[str] = PROTOCOLS,
    iterations: int = 1000,
    warmup: int = 50,
    concurrency: int = 1,
    via_proxy: bool = False,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """La même boucle de ``iterations`` appels, protocole après protocole.

    Conditions de mesure : bus de traces coupé (on ne mesure pas l'instrumentation),
    connexion persistante ouverte par ``warmup`` appels non comptés (prolongés
    jusqu'à 30 ms, le temps que le processeur sorte de son régime d'attente), un
    client par thread quand ``concurrency`` > 1 (départ groupé). Les statistiques portent sur
    les appels réussis ; un appel en échec est compté dans ``errors`` sans arrêter
    la mesure, sauf après ``MAX_CONSECUTIVE_ERRORS`` échecs d'affilée
    (``aborted``). Sans aucun appel réussi, les statistiques valent ``None``.

    L'histogramme de chaque protocole utilise les MÊMES bornes — du plus petit
    échantillon au plus grand 99,5ᵉ centile — pour que les distributions se
    superposent ; ``clipped`` compte les échantillons au-delà de la dernière borne.
    """
    _require_started(runtime)
    params = _resolve_params(method, params)
    protocols = _resolve_protocols(protocols)
    _require_int("iterations", iterations, 1, MAX_ITERATIONS)
    _require_int("warmup", warmup, 0, MAX_ITERATIONS)
    _require_int("concurrency", concurrency, 1, MAX_CONCURRENCY)
    emit = _emitter(progress, "latency")
    section: dict[str, Any] = {
        "method": method,
        "params": params,
        "iterations": iterations,
        "warmup": warmup,
        "concurrency": concurrency,
        "via_proxy": bool(via_proxy),
        "results": [],
    }
    runs: list[_Run] = []
    with runtime.bus.muted(), _stock_preserved(runtime.service, [(method, params)]):
        for index, protocol in enumerate(protocols):
            label = PROTOCOL_LABELS[protocol]

            def tick(done: int, index: int = index, label: str = label) -> None:
                emit(
                    (index + done / iterations) / len(protocols),
                    f"Latence — {label} : {fr_number(done)}/{fr_number(iterations)} appels",
                )

            tick(0)
            runs.append(_measure_protocol(
                runtime, protocol, method, params, iterations, warmup, concurrency, bool(via_proxy), tick
            ))
            section["results"] = _summarise(runs)
            emit((index + 1) / len(protocols), f"Latence — {label} : mesure terminée", {"latency": dict(section)})
    return section


def _measure_protocol(
    runtime: "LabRuntime",
    protocol: str,
    method: str,
    params: Mapping[str, Any],
    iterations: int,
    warmup: int,
    concurrency: int,
    via_proxy: bool,
    tick: Callable[[int], None],
) -> _Run:
    if concurrency == 1:
        with runtime.client(protocol, via_proxy=via_proxy) as client:
            call = _bound_call(client, method, params)
            _warm_up(call, warmup, _CPU_WAKE_S)
            return _Run.merge(protocol, [_timed_calls(call, iterations, threading.Event(), tick)])

    # Un client (donc une connexion) par thread ; les itérations sont réparties entre eux.
    shares = [iterations // concurrency + (worker < iterations % concurrency) for worker in range(concurrency)]
    done_by_worker = [0] * concurrency
    stop, ready, go = threading.Event(), threading.Semaphore(0), threading.Event()
    clients = [runtime.client(protocol, via_proxy=via_proxy) for _ in range(concurrency)]

    def work(worker: int) -> _Batch:
        try:
            call = _bound_call(clients[worker], method, params)
            _warm_up(call, warmup, _CPU_WAKE_S)
        finally:
            ready.release()   # même en cas d'échec : le départ groupé ne doit pas rester bloqué
        go.wait()
        return _timed_calls(call, shares[worker], stop, partial(done_by_worker.__setitem__, worker))

    try:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix=f"bench-{protocol}") as pool:
            futures = [pool.submit(work, worker) for worker in range(concurrency)]
            try:
                for _ in futures:
                    ready.acquire()
                go.set()   # tous les clients sont connectés et échauffés : départ groupé
                reported = 0
                while wait(futures, timeout=_POLL_S).not_done:
                    done = sum(done_by_worker)
                    if done < iterations and done * _LOOP_TICKS // iterations > reported:
                        reported = done * _LOOP_TICKS // iterations
                        tick(done)
            finally:
                # Sortie anticipée (Ctrl+C, rappel de progression en échec) : aucun thread ne doit
                # rester à attendre le départ ni dérouler sa boucle jusqu'au bout.
                stop.set()
                go.set()
        return _Run.merge(protocol, [future.result() for future in futures])
    finally:
        for client in clients:
            client.close()


def _bound_call(client: InventoryClient, method: str, params: Mapping[str, Any]) -> Callable[[], Any]:
    """L'appel à mesurer, résolu une fois pour toutes : la boucle ne paie ni recherche ni validation."""
    return partial(getattr(client, method), **params)


def _warm_up(call: Callable[[], Any], count: int, wake_s: float = 0.0) -> None:
    """Appels non mesurés : ils ouvrent la connexion et amorcent les caches (HPACK, routes, allocations).

    ``wake_s`` prolonge l'échauffement jusqu'à cette durée. Après une attente, le
    processeur tourne à fréquence réduite pendant une vingtaine de millisecondes :
    sans cela, les premiers appels mesurés seraient deux à quatre fois plus lents
    que les suivants, et l'appel local — le plus court — serait le plus pénalisé.
    """
    if not count:
        return
    deadline = time.perf_counter() + wake_s
    done = 0
    while done < count or time.perf_counter() < deadline:
        try:
            call()
        except RpcError:
            return   # inutile d'insister : la boucle mesurée comptera les erreurs
        done += 1


def _ignore_tick(done: int) -> None:
    """Progression non suivie (boucles courtes : octets sur le fil, balayage réseau)."""


def _timed_calls(call: Callable[[], Any], count: int, stop: threading.Event, tick: Callable[[int], None]) -> _Batch:
    """La boucle mesurée : ``count`` appels, chacun chronométré par ``perf_counter_ns``.

    Un appel en échec est compté, pas chronométré : sa durée serait celle d'une
    panne (une échéance de 5 s…), pas celle d'un appel. Après
    ``MAX_CONSECUTIVE_ERRORS`` échecs d'affilée, ``stop`` est levé et la boucle
    s'arrête : on n'attend pas mille échéances d'un serveur injoignable.
    """
    batch = _Batch()
    clock = time.perf_counter_ns
    chunk = max(1, math.ceil(count / _LOOP_TICKS))
    streak = 0
    batch.started_ns = clock()
    for done in range(1, count + 1):
        if stop.is_set():
            batch.aborted = True
            break
        started = clock()
        try:
            call()
        except RpcError as error:
            batch.errors += 1
            batch.last_error = error
            streak += 1
            if streak >= MAX_CONSECUTIVE_ERRORS:
                stop.set()
        else:
            batch.durations_ns.append(clock() - started)
            batch.starts_ns.append(started)
            streak = 0
        if done % chunk == 0 and done < count:
            paused = clock()
            tick(done)
            batch.paused_ns += clock() - paused
    batch.ended_ns = clock()
    return batch


def _summarise(runs: Sequence[_Run]) -> list[dict[str, Any]]:
    """Statistiques par protocole, avec un histogramme aux bornes communes."""
    series = [[round(duration / 1e6, _MS_DECIMALS) for duration in run.durations_ns] for run in runs]
    ordered = [sorted(samples) for samples in series]
    edges = _shared_edges([samples for samples in ordered if samples])
    local_mean = next(
        (math.fsum(samples) / len(samples) for run, samples in zip(runs, ordered)
         if run.protocol == "local" and samples),
        None,
    )
    return [
        _result(run, chronological, samples, edges, local_mean)
        for run, chronological, samples in zip(runs, series, ordered)
    ]


def _result(
    run: _Run,
    chronological: Sequence[float],
    ordered: Sequence[float],
    edges: Sequence[float],
    local_mean: float | None,
) -> dict[str, Any]:
    count = len(ordered)
    total_s = run.wall_ns / 1e9
    result: dict[str, Any] = {
        "protocol": run.protocol,
        "label": PROTOCOL_LABELS[run.protocol],
        "count": count,
        "errors": run.errors,
        **dict.fromkeys(_STAT_KEYS),
    }
    mean = math.fsum(ordered) / count if count else None
    if mean is not None:
        variance = math.fsum((sample - mean) ** 2 for sample in ordered) / (count - 1) if count > 1 else 0.0
        result.update(
            mean_ms=round(mean, _MS_DECIMALS),
            median_ms=_percentile(ordered, 50),
            p90_ms=_percentile(ordered, 90),
            p95_ms=_percentile(ordered, 95),
            p99_ms=_percentile(ordered, 99),
            min_ms=ordered[0],
            max_ms=ordered[-1],
            stdev_ms=round(math.sqrt(variance), _MS_DECIMALS),
        )
    result.update(
        total_s=round(total_s, 6),
        rps=round(count / total_s, 1) if total_s > 0 else None,
        overhead_vs_local_x=round(mean / local_mean, 2) if mean is not None and local_mean else None,
        histogram=_histogram(ordered, edges),
        samples_ms=_downsample(chronological),
        aborted=run.aborted,
        error=None if run.last_error is None else {"code": run.last_error.code, "message": run.last_error.message},
    )
    return result


def _percentile(ordered: Sequence[float], percent: float) -> float:
    """Centile par interpolation linéaire entre les deux rangs voisins (``ordered`` est trié)."""
    rank = (len(ordered) - 1) * percent / 100
    lower = math.floor(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower), _MS_DECIMALS)


def _shared_edges(populated: Sequence[Sequence[float]]) -> list[float]:
    """Bornes d'histogramme communes à toutes les séries (triées, non vides) ; ``[]`` s'il n'y en a aucune."""
    if not populated:
        return []
    low = min(samples[0] for samples in populated)
    high = max(_percentile(samples, HISTOGRAM_CLIP_PERCENTILE) for samples in populated)
    if high <= low:   # échantillons tous identiques : il faut tout de même des bacs de largeur non nulle
        high = round(low + max(low * 0.01, 10 ** -_MS_DECIMALS), _MS_DECIMALS)
    width = (high - low) / HISTOGRAM_BUCKETS
    return [round(low + bucket * width, _MS_DECIMALS) for bucket in range(HISTOGRAM_BUCKETS)] + [high]


def _histogram(ordered: Sequence[float], edges: Sequence[float]) -> dict[str, Any]:
    """Effectifs par bac ``[borne, borne suivante[`` — le dernier bac inclut sa borne haute."""
    if not edges:
        return {"edges_ms": [], "counts": [], "clipped": 0}
    bounds = [bisect_left(ordered, edge) for edge in edges[:-1]] + [bisect_right(ordered, edges[-1])]
    return {
        "edges_ms": list(edges),
        "counts": [upper - lower for lower, upper in zip(bounds, bounds[1:])],
        "clipped": len(ordered) - bounds[-1],
    }


def _downsample(values: Sequence[float], limit: int = MAX_SAMPLES) -> list[float]:
    """Au plus ``limit`` points régulièrement espacés, dans l'ordre : de quoi tracer la série."""
    if len(values) <= limit:
        return list(values)
    step = len(values) / limit
    return [values[int(index * step)] for index in range(limit)]


# --- Local vs distant --------------------------------------------------------

def run_network_sweep(
    runtime: "LabRuntime",
    *,
    latencies_ms: Iterable[float] = (0, 10, 50, 100, 200),
    iterations: int = 20,
    method: str = "get_product_details",
    params: Mapping[str, Any] | None = None,
    protocols: Iterable[str] = PROTOCOLS,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """« Local vs distant » : la même boucle, sur un réseau de plus en plus lent.

    Les protocoles distants passent par leur proxy de chaos, réglé sur un réseau
    idéal auquel on ajoute ``latency_ms`` d'aller-retour ; l'appel local, lui,
    n'emprunte aucun réseau. Chaque point donne le temps moyen d'un appel
    (``None`` si tous ont échoué, voir ``errors``). Les conditions réseau
    trouvées à l'entrée sont rétablies en sortie, quoi qu'il arrive.
    """
    _require_started(runtime)
    params = _resolve_params(method, params)
    protocols = _resolve_protocols(protocols)
    latencies = _resolve_latencies(latencies_ms)
    _require_int("iterations", iterations, 1, MAX_ITERATIONS)
    emit = _emitter(progress, "network")
    section: dict[str, Any] = {
        "method": method,
        "params": params,
        "iterations": iterations,
        "via_proxy": True,
        "protocols": list(protocols),
        "points": [],
    }
    budget = sum(latency + _SWEEP_BASE_COST_MS for latency in latencies)
    spent = 0.0
    saved = runtime.conditions.snapshot()
    clients = {protocol: runtime.client(protocol, via_proxy=True) for protocol in protocols}
    try:
        with runtime.bus.muted(), _stock_preserved(runtime.service, [(method, params)]):
            calls = {protocol: _bound_call(client, method, params) for protocol, client in clients.items()}
            for latency in latencies:
                runtime.conditions.update(preset="ideal", latency_ms=latency)
                cost = latency + _SWEEP_BASE_COST_MS
                point: dict[str, Any] = {"latency_ms": latency, "results": {}, "errors": {}}
                for index, protocol in enumerate(protocols):
                    emit(
                        (spent + cost * index / len(protocols)) / budget,
                        f"Réseau simulé : {fr_compact(latency)} ms de latence — {PROTOCOL_LABELS[protocol]}",
                    )
                    # Chaque appel d'échauffement coûte la latence ajoutée : deux suffisent à (r)ouvrir la
                    # connexion. Quand les appels sont courts, on les prolonge jusqu'au réveil du processeur.
                    _warm_up(calls[protocol], _SWEEP_WARMUP, _CPU_WAKE_S)
                    batch = _timed_calls(calls[protocol], iterations, threading.Event(), _ignore_tick)
                    durations = batch.durations_ns
                    point["results"][protocol] = (
                        round(math.fsum(durations) / len(durations) / 1e6, _MS_DECIMALS) if durations else None
                    )
                    point["errors"][protocol] = batch.errors
                spent += cost
                section["points"] = [*section["points"], point]
                emit(
                    spent / budget,
                    f"Réseau simulé : point à {fr_compact(latency)} ms mesuré",
                    {"network": dict(section)},
                )
    finally:
        runtime.conditions.update(**saved)
        for client in clients.values():
            client.close()
    return section


# --- Rapport complet ---------------------------------------------------------

def environment_info() -> dict[str, Any]:
    """La machine et les bibliothèques : sans elles, une mesure ne se compare à rien."""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "grpcio": grpc.__version__,
        "protobuf": google.protobuf.__version__,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def run_full_benchmark(
    runtime: "LabRuntime",
    config: Mapping[str, Any] | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Enchaîne les suites demandées et renvoie le rapport complet.

    ``{id, created_at, config, environment, payload, serialization, latency,
    network, highlights}`` — une suite non demandée vaut ``None``. ``progress``
    reçoit ``(phase, fraction globale 0..1, message, partial)`` ; ``partial``
    porte chaque section (``{"latency": …}``) dès qu'elle est disponible, et
    même en cours de route, pour qu'une interface dessine au fur et à mesure.
    """
    settings = resolve_config(config)
    _require_started(runtime)
    environment = environment_info()
    created = datetime.fromisoformat(environment["timestamp"])
    report: dict[str, Any] = {
        "id": f"benchmark-{created:%Y%m%d-%H%M%S}",
        "created_at": environment["timestamp"],
        "config": settings,
        "environment": environment,
        **dict.fromkeys(SUITES),
        "highlights": [],
    }
    runners: dict[str, Callable[[ProgressCallback | None], dict[str, Any]]] = {
        "payload": lambda listener: measure_payload_sizes(
            runtime, scaling_limits=settings["scaling_limits"], wire_calls=settings["wire_calls"], progress=listener
        ),
        "serialization": lambda listener: measure_serialization(
            settings["serialization_iterations"], progress=listener
        ),
        "latency": lambda listener: run_latency_benchmark(
            runtime,
            method=settings["method"],
            params=settings["params"],
            protocols=settings["protocols"],
            iterations=settings["iterations"],
            warmup=settings["warmup"],
            concurrency=settings["concurrency"],
            via_proxy=settings["via_proxy"],
            progress=listener,
        ),
        "network": lambda listener: run_network_sweep(
            runtime,
            latencies_ms=settings["sweep_latencies_ms"],
            iterations=settings["sweep_iterations"],
            method=settings["method"],
            params=settings["params"],
            protocols=settings["protocols"],
            progress=listener,
        ),
    }
    total_weight = sum(_SUITE_WEIGHTS[suite] for suite in settings["suites"])
    start = 0.0
    for suite in settings["suites"]:
        span = _SUITE_WEIGHTS[suite] / total_weight
        report[suite] = runners[suite](_scaled(progress, start, span))
        start += span
    report["highlights"] = build_highlights(report)
    if progress is not None:
        progress("done", 1.0, SUITE_LABELS["done"], {"highlights": report["highlights"]})
    return report


# --- Faits marquants ---------------------------------------------------------

def build_highlights(report: Mapping[str, Any]) -> list[dict[str, str]]:
    """Faits marquants ``{id, title, value, detail, tone}``, calculés à partir des mesures du rapport.

    Chaque phrase est construite sur les nombres du rapport : le protocole « le
    plus rapide » ou « le plus léger » est celui que cette exécution a mesuré
    comme tel. ``tone`` vaut ``success`` (avantage), ``warning`` (coût) ou
    ``info`` (constat). Une section absente ne produit aucun fait.
    """
    builders = (
        _highlight_payload, _highlight_scaling, _highlight_wire, _highlight_http, _highlight_serialization,
        _highlight_remote_cost, _highlight_ranking, _highlight_tail, _highlight_throughput, _highlight_network,
    )
    return [highlight for build in builders if (highlight := build(report)) is not None]


def _highlight(identifier: str, title: str, value: str, detail: str, tone: str) -> dict[str, str]:
    return {"id": identifier, "title": title, "value": value, "detail": detail, "tone": tone}


def _payload_rows(report: Mapping[str, Any], method: str) -> list[Mapping[str, Any]]:
    return [row for row in (report.get("payload") or {}).get("rows") or () if row["method"] == method]


def _measured(report: Mapping[str, Any], *, remote_only: bool = False) -> list[Mapping[str, Any]]:
    """Résultats de latence exploitables (au moins un appel réussi), du plus rapide au plus lent."""
    results = [
        result for result in (report.get("latency") or {}).get("results") or ()
        if result.get("mean_ms") and not (remote_only and result["protocol"] == "local")
    ]
    return sorted(results, key=lambda result: result["mean_ms"])


def _times(factor: float) -> str:
    return f"{fr_number(factor, 0 if factor >= 100 else 1)} fois"


def _highlight_payload(report: Mapping[str, Any]) -> dict[str, str] | None:
    row = next((row for row in _payload_rows(report, "get_product_details") if row["direction"] == "response"), None)
    if row is None or not row.get("json_bytes") or row.get("protobuf_bytes") is None:
        return None
    json_bytes, protobuf_bytes = row["json_bytes"], row["protobuf_bytes"]
    smaller = protobuf_bytes < json_bytes
    reason = (
        "Protobuf ne transmet que des numéros de champ là où JSON répète chaque nom en toutes lettres."
        if smaller else
        "Sur ce message, le format binaire n'est pas plus compact que le texte."
    )
    return _highlight(
        "payload_size",
        "Fiche produit : Protobuf vs JSON",
        fr_percent(_relative_pct(protobuf_bytes, json_bytes), signed=True),
        f"Réponse de get_product_details : {fr_bytes(protobuf_bytes)} en Protobuf contre "
        f"{fr_bytes(json_bytes)} en JSON-RPC. {reason}",
        "success" if smaller else "warning",
    )


def _highlight_scaling(report: Mapping[str, Any]) -> dict[str, str] | None:
    rows = [
        row for row in (report.get("payload") or {}).get("scaling") or ()
        if row.get("json_bytes") and row.get("protobuf_bytes")
    ]
    if not rows:
        return None
    row = max(rows, key=lambda row: row["items"])
    items, json_bytes, protobuf_bytes = row["items"], row["json_bytes"], row["protobuf_bytes"]
    gap = json_bytes - protobuf_bytes
    outcome = (
        f"soit {fr_bytes(gap)} de moins à chaque réponse" if gap > 0
        else f"soit {fr_bytes(-gap)} de plus à chaque réponse"
    )
    return _highlight(
        "payload_scaling",
        f"Liste de {fr_number(items)} produits : Protobuf vs JSON",
        fr_percent(_relative_pct(protobuf_bytes, json_bytes), signed=True),
        f"{fr_bytes(protobuf_bytes)} en Protobuf contre {fr_bytes(json_bytes)} en JSON, {outcome} "
        f"({fr_number(protobuf_bytes / items)} octets par produit contre {fr_number(json_bytes / items)}).",
        "success" if gap > 0 else "warning",
    )


def _highlight_wire(report: Mapping[str, Any]) -> dict[str, str] | None:
    rows = sorted(
        (row for row in (report.get("payload") or {}).get("wire") or () if row.get("total_per_call")),
        key=lambda row: row["total_per_call"],
    )
    if len(rows) < 2:
        return None
    lightest, heaviest = rows[0], rows[-1]
    method = lightest.get("method", WIRE_METHOD)
    listing = ", ".join(f"{row['label']} {fr_number(row['total_per_call'])}" for row in rows)
    detail = (
        f"Octets relayés par appel {method}, aller et retour, tramage et en-têtes compris : {listing}. "
        f"{lightest['label']} en échange {_times(heaviest['total_per_call'] / lightest['total_per_call'])} "
        f"moins que {heaviest['label']}."
    )
    # Ce que le transport ajoute aux trames gRPC, invisible depuis l'application : la différence
    # entre les octets comptés par le proxy et ceux que le client a confiés à la bibliothèque.
    grpc_row = next((row for row in rows if row["protocol"] == "grpc"), None)
    framed = [row.get("grpc_bytes") for row in _payload_rows(report, method)]
    if grpc_row is not None and len(framed) == 2 and all(framed):
        transport = grpc_row["total_per_call"] - sum(framed)
        if transport > 0:
            detail += (
                f" HTTP/2 ajoute {fr_number(transport)} octets (trames, en-têtes compressés) aux "
                f"{fr_number(sum(framed))} octets des messages gRPC."
            )
    return _highlight(
        "wire_bytes",
        "Octets réellement échangés sur TCP",
        f"{fr_bytes(lightest['total_per_call'])} — {lightest['label']}",
        detail,
        "info",
    )


def _highlight_http(report: Mapping[str, Any]) -> dict[str, str] | None:
    totals: dict[str, list[int]] = {}
    for row in (report.get("payload") or {}).get("rows") or ():
        if row.get("rest_bytes") and row.get("rest_body_bytes") is not None:
            sums = totals.setdefault(row["method"], [0, 0])
            sums[0] += row["rest_bytes"]
            sums[1] += row["rest_body_bytes"]
    if not totals:
        return None
    method, (exchanged, useful) = max(totals.items(), key=lambda item: 1 - item[1][1] / item[1][0])
    share = (exchanged - useful) / exchanged * 100
    return _highlight(
        "http_overhead",
        "Part de l'enveloppe HTTP dans un appel REST",
        fr_percent(share, 0),
        f"{method} : {fr_bytes(exchanged)} échangés pour {fr_bytes(useful)} de JSON utile. Ligne de requête, "
        f"statut et en-têtes pèsent {fr_bytes(exchanged - useful)}, répétés à chaque appel.",
        "warning" if share >= 50 else "info",
    )


def _highlight_serialization(report: Mapping[str, Any]) -> dict[str, str] | None:
    rows = {
        row["format"]: row for row in (report.get("serialization") or {}).get("rows") or ()
        if row.get("items") == 1
    }
    if "json" not in rows or "protobuf" not in rows:
        return None
    json_ns = rows["json"]["encode_ns"] + rows["json"]["decode_ns"]
    codec_ns = rows["protobuf"]["encode_ns"] + rows["protobuf"]["decode_ns"]
    full_ns = rows["protobuf"]["encode_from_dict_ns"] + rows["protobuf"]["decode_to_dict_ns"]
    if not json_ns or not codec_ns:
        return None
    faster = codec_ns < json_ns
    verdict = f"{_times(json_ns / codec_ns)} plus rapide" if faster else f"{_times(codec_ns / json_ns)} plus lent"
    conversion = (
        "soit plus que JSON : en Python, le typage du contrat se paie à la conversion"
        if full_ns > json_ns else "et reste plus rapide que JSON"
    )
    return _highlight(
        "serialization_speed",
        "Codec Protobuf vs JSON (fiche produit)",
        verdict,
        f"Encoder puis décoder une fiche produit prend {fr_duration(codec_ns / 1e6)} en Protobuf contre "
        f"{fr_duration(json_ns / 1e6)} en JSON. Avec la conversion dictionnaire ⇄ message que fait ce "
        f"laboratoire, Protobuf passe à {fr_duration(full_ns / 1e6)}, {conversion}.",
        "success" if faster else "warning",
    )


def _highlight_remote_cost(report: Mapping[str, Any]) -> dict[str, str] | None:
    latency = report.get("latency") or {}
    local = next((result for result in _measured(report) if result["protocol"] == "local"), None)
    remote = _measured(report, remote_only=True)
    if local is None or not remote:
        return None
    fastest = remote[0]
    factor = fastest["mean_ms"] / local["mean_ms"]
    return _highlight(
        "remote_cost",
        "Coût d'un appel distant",
        fr_ratio(factor),
        f"{latency.get('method', '')} : {fr_duration(local['mean_ms'])} en appel local, "
        f"{fr_duration(fastest['mean_ms'])} avec {fastest['label']}, le plus rapide des protocoles distants "
        f"de cette mesure. Le code appelant est le même ; l'appel, lui, dure {_times(factor)} plus longtemps.",
        "warning",
    )


def _highlight_ranking(report: Mapping[str, Any]) -> dict[str, str] | None:
    latency = report.get("latency") or {}
    remote = _measured(report, remote_only=True)
    if len(remote) < 2:
        return None
    fastest = remote[0]
    listing = ", ".join(f"{result['label']} {fr_duration(result['mean_ms'])}" for result in remote)
    clients = latency.get("concurrency", 1)
    route = "à travers le proxy de chaos" if latency.get("via_proxy") else "en boucle locale"
    detail = (
        f"Temps moyen par appel {latency.get('method', '')} ({route}, {fr_number(clients)} "
        f"client{'s' if clients > 1 else ''}) : {listing}."
    )
    grpc_result = next((result for result in remote if result["protocol"] == "grpc"), None)
    if grpc_result is not None and grpc_result is not fastest:
        detail += (
            f" gRPC est {_times(grpc_result['mean_ms'] / fastest['mean_ms'])} plus lent que "
            f"{fastest['label']} ici{_protobuf_edge(report)}."
        )
    elif grpc_result is not None:
        lead = (1 - fastest["mean_ms"] / remote[1]["mean_ms"]) * 100
        detail += f" gRPC devance {remote[1]['label']} de {fr_percent(lead, 0)}."
    return _highlight("fastest_remote", "Protocole distant le plus rapide", fastest["label"], detail, "info")


def _protobuf_edge(report: Mapping[str, Any]) -> str:
    """Là où les mesures de taille donnent l'avantage à Protobuf (chaîne vide si elles ne le montrent pas)."""
    def saving(row: Mapping[str, Any]) -> float:
        """Part des octets économisée par Protobuf sur ce message, en % (0 s'il n'est pas plus petit)."""
        return max(-(_relative_pct(row.get("protobuf_bytes"), row.get("json_bytes")) or 0.0), 0.0)

    product = next(
        (saving(row) for row in _payload_rows(report, "get_product_details") if row["direction"] == "response"), 0.0
    )
    if not product:
        return ""
    edge = f" ; son avantage mesuré est la taille : des messages {fr_percent(product, 0)} plus petits"
    scaling = [row for row in (report.get("payload") or {}).get("scaling") or () if saving(row)]
    if scaling:
        largest = max(scaling, key=lambda row: row["items"])
        edge += f" ({fr_percent(saving(largest), 0)} sur {fr_number(largest['items'])} produits)"
    return edge + ", garantis par un contrat typé"


def _highlight_tail(report: Mapping[str, Any]) -> dict[str, str] | None:
    remote = [result for result in _measured(report, remote_only=True) if result.get("median_ms")]
    if not remote:
        return None
    worst = max(remote, key=lambda result: result["p99_ms"] / result["median_ms"])
    factor = worst["p99_ms"] / worst["median_ms"]
    return _highlight(
        "tail_latency",
        "Queue de distribution (p99 / médiane)",
        fr_ratio(factor),
        f"{worst['label']} : médiane {fr_duration(worst['median_ms'])}, mais un appel sur cent dépasse "
        f"{fr_duration(worst['p99_ms'])} et le plus lent a duré {fr_duration(worst['max_ms'])}.",
        "warning" if factor >= 2 else "info",
    )


def _highlight_throughput(report: Mapping[str, Any]) -> dict[str, str] | None:
    latency = report.get("latency") or {}
    remote = [result for result in _measured(report, remote_only=True) if result.get("rps")]
    if not remote:
        return None
    best = max(remote, key=lambda result: result["rps"])
    clients = latency.get("concurrency", 1)
    detail = f"{best['label']}, avec {fr_number(clients)} client{'s en parallèle' if clients > 1 else ''}"
    local = next((result for result in _measured(report) if result["protocol"] == "local" and result.get("rps")), None)
    if local is not None:
        detail += f" ; l'appel local atteint {fr_number(local['rps'])} appels/s"
    return _highlight(
        "throughput",
        "Débit le plus élevé à distance",
        f"{fr_number(best['rps'])} appels/s",
        detail + ".",
        "info",
    )


def _highlight_network(report: Mapping[str, Any]) -> dict[str, str] | None:
    network = report.get("network") or {}
    points = [point for point in network.get("points") or () if point["latency_ms"] > 0]
    if not points:
        return None
    point = max(points, key=lambda point: point["latency_ms"])
    remote = [mean for protocol, mean in point["results"].items() if protocol != "local" and mean]
    if not remote:
        return None
    latency_ms, iterations = point["latency_ms"], network.get("iterations", 1)
    mean = math.fsum(remote) / len(remote)
    local = point["results"].get("local")
    title = f"Distant vs local avec {fr_compact(latency_ms)} ms de latence"
    detail = (
        f"Une boucle de {fr_number(iterations)} appels {network.get('method', '')} dure "
        f"{fr_duration(mean * iterations)} à distance"
    )
    detail += f" contre {fr_duration(local * iterations)} en local. " if local else ". "
    detail += f"Le réseau représente {fr_percent(min(latency_ms / mean, 1) * 100, 1)} du temps d'un appel"
    if len(remote) > 1:
        spread = max(remote) - min(remote)
        detail += (
            f" ; l'écart entre le plus rapide et le plus lent des protocoles est de {fr_duration(spread)}, "
            f"soit {fr_percent(spread / mean * 100, 1)} de ce temps"
        )
    return _highlight(
        "network_latency",
        title,
        fr_ratio(mean / local) if local else fr_duration(mean),
        detail + ".",
        "warning",
    )


# --- Ligne de commande -------------------------------------------------------

class _ConsoleProgress:
    """Progression sur la console : une ligne réécrite sur place, ou une ligne par phase si la sortie est redirigée."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._interactive = stream.isatty()
        self._phase = ""

    def __call__(self, phase: str, fraction: float, message: str, partial_result: dict[str, Any] | None = None) -> None:
        if self._interactive:
            width = shutil.get_terminal_size().columns - 1
            line = f"  [{round(fraction * 100):3d} %] {message}"[:width]
            self._stream.write("\r" + line.ljust(width) + ("\n" if phase == "done" else ""))
            self._stream.flush()
        elif phase != self._phase:
            self._phase = phase
            print(f"  • {SUITE_LABELS.get(phase, message)}", file=self._stream, flush=True)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_ITERATIONS:
        raise argparse.ArgumentTypeError(f"attendu : un entier compris entre 1 et {MAX_ITERATIONS}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """Lance le banc d'essai complet sur un laboratoire à ports éphémères, l'affiche et l'enregistre."""
    parser = argparse.ArgumentParser(
        prog="python -m benchmark_lab.benchmark_perf",
        description="Banc d'essai de performance : tailles des messages, sérialisation, latence, réseau simulé.",
    )
    parser.add_argument("--quick", action="store_true", help="version courte : moins d'appels, balayage réduit")
    parser.add_argument("--iterations", type=_positive_int, metavar="N",
                        help="nombre d'appels chronométrés par protocole")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):   # console Windows : accents et cadres en UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    config = dict(QUICK_CONFIG if args.quick else DEFAULT_CONFIG)
    if args.iterations is not None:
        config["iterations"] = args.iterations

    from lab import LabRuntime   # import tardif : il charge les trois middlewares

    print(f"{APP_NAME} — banc d'essai de performance{' (version courte)' if args.quick else ''}")
    try:
        # Ports éphémères : le banc tourne même si un laboratoire occupe déjà les ports par défaut.
        with LabRuntime.ephemeral() as runtime:
            report = run_full_benchmark(runtime, config, _ConsoleProgress(sys.stdout))
    except (RuntimeError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nBanc d'essai interrompu.", file=sys.stderr)
        return 130
    print()
    print(to_text(report))
    print(f"\nRapport enregistré : {save_report(report)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
