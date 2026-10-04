"""Intégration : les quatre « protocoles » face au même laboratoire, d'un bout à l'autre.

Chaque module a ses propres tests ; ce fichier vérifie ce qu'aucun d'eux ne peut
voir seul — que le même appel donne le même résultat et la même erreur quel que
soit le middleware, que les traces des trois middlewares obéissent aux mêmes
règles — et garde un test de non-régression par défaut d'intégration corrigé.

Le laboratoire écoute sur des ports éphémères et publie sur un bus privé.
"""
from __future__ import annotations

import logging
import math
import re
import socket
import threading
import time
from typing import Any, Callable, Iterator

import grpc
import pytest

from common.client_api import InventoryClient
from common.config import HOST, PROTOCOLS, REMOTE_PROTOCOLS, Ports
from common.errors import (
    CANCELLED,
    INVALID_ARGUMENT,
    NOT_FOUND,
    UNAVAILABLE,
    InvalidArgumentError,
    NotFoundError,
    RpcError,
    RpcTimeoutError,
    RpcTransportError,
)
from common.inventory import InventoryService
from common.telemetry import PIPELINE, EventBus, TraceEvent
from lab import PORT_OFFSET_STEP, LabRuntime
from rest_api.rest_client import RestInventoryClient
from rpc_custom import CustomInventoryClient
from rpc_grpc import grpc_client
from rpc_grpc.generated import service_pb2 as pb
from rpc_grpc.grpc_client import GrpcInventoryClient
from rpc_grpc.grpc_server import InventoryServicer
from rpc_grpc.interceptors import CALL_ID_KEY

ROUTES = [(protocol, via_proxy) for protocol in PROTOCOLS for via_proxy in (False, True)]
ROUTE_IDS = [f"{protocol}-{'proxy' if via_proxy else 'direct'}" for protocol, via_proxy in ROUTES]
# Champs qui dépendent de l'instant de l'appel, pas de ses arguments.
VOLATILE_KEYS = ("compute_us", "updated_at_ms", "timestamp_ms")
LARGEST_FACTORIAL = 5000            # MAX_FACTORIAL_N : la borne annoncée par le catalogue
LARGEST_FACTORIAL_DIGITS = 16_326
WIRE_STAGES = ("client.send", "server.receive", "server.send", "client.receive")


# --- Outils ------------------------------------------------------------------

def stable(value: Any) -> Any:
    """``value`` sans ses champs horodatés : ce qui doit être identique d'un protocole à l'autre."""
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items() if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def outcome(client: InventoryClient, method: str, params: dict[str, Any]) -> tuple[Any, ...]:
    """Issue d'un appel sous une forme comparable : le résultat, ou le code canonique de l'erreur."""
    try:
        result = client.invoke(method, params)
        if isinstance(result, Iterator):
            result = list(result)
    except RpcError as error:
        return ("error", error.code)
    return ("ok", stable(result))


def decimal_to_int(text: str) -> int:
    """Relit un grand entier par tranches : ``int()`` refuse les chaînes de plus de 4300 chiffres."""
    value = 0
    for start in range(0, len(text), 1000):
        chunk = text[start:start + 1000]
        value = value * 10 ** len(chunk) + int(chunk)
    return value


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def wait_until(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.perf_counter() + timeout
    while not condition():
        if time.perf_counter() > deadline:
            return False
        time.sleep(0.005)
    return True


# --- Laboratoire partagé par le fichier ---------------------------------------

@pytest.fixture(scope="module")
def lab() -> Iterator[LabRuntime]:
    with LabRuntime.ephemeral(bus=EventBus()) as runtime:
        yield runtime


@pytest.fixture(scope="module")
def clients(lab: LabRuntime) -> Iterator[dict[tuple[str, bool], InventoryClient]]:
    """Un client par route (protocole, direct ou via le proxy), gardé ouvert pour tout le fichier."""
    opened = {route: lab.client(route[0], via_proxy=route[1]) for route in ROUTES}
    yield opened
    for client in opened.values():
        client.close()


@pytest.fixture(autouse=True)
def pristine(lab: LabRuntime) -> None:
    lab.reset()


# --- Parité : même appel, même issue, quel que soit le middleware --------------

@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("calculate_factorial", {"n": 0}),
        ("calculate_factorial", {"n": 170}),
        ("get_product_details", {"product_id": "SKU-1003"}),
        ("list_products", {}),
        ("list_products", {"limit": 30}),
        ("list_products", {"limit": 4, "category": "Énergie"}),
        ("list_products", {"limit": 4, "category": "Inconnue"}),
        ("update_stock", {"product_id": "SKU-1002", "delta": -5}),
        ("update_stock", {"product_id": "SKU-1002", "delta": 7, "idempotency_key": "réassort-1"}),
        ("stream_analytics", {"samples": 3, "interval_ms": 0}),
    ],
    ids=lambda value: value if isinstance(value, str) else ",".join(f"{k}={v}" for k, v in value.items()) or "défauts",
)
def test_same_call_gives_the_same_result_on_every_route(
    lab: LabRuntime, clients: dict[tuple[str, bool], InventoryClient], method: str, params: dict[str, Any]
) -> None:
    results = {}
    for route in ROUTES:
        lab.service.reset()     # chaque route part du même inventaire : update_stock renvoie le même stock
        results[route] = outcome(clients[route], method, params)
    reference = results["local", False]
    assert reference[0] == "ok"
    for route, result in results.items():
        assert result == reference, f"{route} ne renvoie pas le même dictionnaire que l’appel local"


@pytest.mark.parametrize(
    ("method", "params", "code"),
    [
        ("calculate_factorial", {"n": -1}, INVALID_ARGUMENT),
        ("calculate_factorial", {"n": LARGEST_FACTORIAL + 1}, INVALID_ARGUMENT),
        ("get_product_details", {"product_id": "SKU-0000"}, NOT_FOUND),
        ("get_product_details", {"product_id": ""}, INVALID_ARGUMENT),
        ("get_product_details", {"product_id": "é à/ü?&=#%"}, NOT_FOUND),
        ("update_stock", {"product_id": "SKU-1002", "delta": -100_000}, "FAILED_PRECONDITION"),
        ("update_stock", {"product_id": "SKU-1002", "delta": 2_000_000}, INVALID_ARGUMENT),
        ("list_products", {"limit": -1}, INVALID_ARGUMENT),
        ("stream_analytics", {"samples": 0, "interval_ms": 10}, INVALID_ARGUMENT),
    ],
    ids=lambda value: value if isinstance(value, str) else ",".join(f"{k}={v!r}" for k, v in value.items()),
)
def test_same_mistake_gives_the_same_error_code_on_every_route(
    clients: dict[tuple[str, bool], InventoryClient], method: str, params: dict[str, Any], code: str
) -> None:
    for route in ROUTES:
        assert outcome(clients[route], method, params) == ("error", code), route


# --- Traces : les trois middlewares obéissent aux mêmes règles -------------------

@pytest.mark.parametrize("via_proxy", [False, True], ids=["direct", "proxy"])
@pytest.mark.parametrize("protocol", REMOTE_PROTOCOLS)
def test_trace_of_a_call_is_complete_ordered_and_byte_exact(
    lab: LabRuntime, clients: dict[tuple[str, bool], InventoryClient], protocol: str, via_proxy: bool
) -> None:
    client = clients[protocol, via_proxy]
    client.update_stock("SKU-1001", -3)
    trace = lab.collector.get(client.last_call_id)
    assert trace is not None
    assert wait_until(lambda: len(trace.events) >= len(PIPELINE))
    events = {event.stage: event for event in trace.events}
    assert [event.stage for event in trace.events if event.stage in PIPELINE] == list(PIPELINE)

    for stage in WIRE_STAGES:
        event = events[stage]
        assert event.payload is not None and event.size == len(event.payload), stage
        # Les segments découpent le payload sans trou ni recouvrement, du premier au dernier octet.
        position = 0
        for segment in event.detail["segments"]:
            assert segment["start"] == position and segment["end"] >= position, (stage, segment)
            position = segment["end"]
        assert position == event.size, stage
    # Ce que le client a écrit est ce que le serveur a lu, et réciproquement.
    assert events["client.send"].payload == events["server.receive"].payload
    assert events["server.send"].payload == events["client.receive"].payload

    summary = trace.summary()
    assert summary["status"] == "ok"
    assert summary["request_bytes"] == len(events["client.send"].payload)
    assert summary["response_bytes"] == len(events["client.receive"].payload)
    assert all(event.duration_us is None or event.duration_us >= 0 for event in trace.events)
    assert summary["duration_us"] >= summary["server_us"] > 0   # l'appel entier contient l'exécution distante


def test_no_trace_is_left_without_a_terminal_stage(
    lab: LabRuntime, clients: dict[tuple[str, bool], InventoryClient]
) -> None:
    for route in ROUTES:
        outcome(clients[route], "get_product_details", {"product_id": "SKU-1001"})
        outcome(clients[route], "get_product_details", {"product_id": "SKU-0000"})
        outcome(clients[route], "stream_analytics", {"samples": 2, "interval_ms": 0})
    assert [trace.call_id for trace in lab.collector.recent(400) if not trace.complete] == []


# --- Non-régression : défauts corrigés à l'intégration ---------------------------

def test_largest_advertised_factorial_exceeds_the_interpreter_digit_limit() -> None:
    """``str()`` d'un entier de plus de 4300 chiffres lève ValueError : 5000! en compte 16 326."""
    reply = InventoryService().calculate_factorial(LARGEST_FACTORIAL)
    assert reply["digits"] == len(reply["result"]) == LARGEST_FACTORIAL_DIGITS
    assert decimal_to_int(reply["result"]) == math.factorial(LARGEST_FACTORIAL)


@pytest.mark.parametrize("n", [1558, 1559, 3000])   # 1558! a exactement 4300 chiffres : la limite, puis au-delà
def test_factorial_is_exact_around_the_digit_limit(n: int) -> None:
    reply = InventoryService().calculate_factorial(n)
    assert decimal_to_int(reply["result"]) == math.factorial(n)
    assert reply["digits"] == len(reply["result"])


@pytest.mark.parametrize(("protocol", "via_proxy"), ROUTES, ids=ROUTE_IDS)
def test_largest_factorial_travels_through_every_middleware(
    clients: dict[tuple[str, bool], InventoryClient], protocol: str, via_proxy: bool
) -> None:
    reply = clients[protocol, via_proxy].calculate_factorial(LARGEST_FACTORIAL)
    assert reply["n"] == LARGEST_FACTORIAL and reply["digits"] == LARGEST_FACTORIAL_DIGITS
    assert decimal_to_int(reply["result"]) == math.factorial(LARGEST_FACTORIAL)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_error_echoing_an_oversized_argument_keeps_its_code(
    clients: dict[tuple[str, bool], InventoryClient], protocol: str
) -> None:
    """Un message d'erreur de 5 Kio débordait des trailers gRPC : NOT_FOUND devenait UNAVAILABLE (rejouable)."""
    with pytest.raises(NotFoundError) as caught:
        clients[protocol, False].get_product_details("X" * 5000)
    assert caught.value.code == NOT_FOUND and not caught.value.retryable
    assert caught.value.message.startswith("Produit inconnu : XXXX")


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("calculate_factorial", {"n": None}),
        ("update_stock", {"product_id": "SKU-1001", "delta": None}),
        ("list_products", {"limit": None}),
    ],
    ids=["factorial", "update_stock", "list_products"],
)
def test_missing_value_is_refused_by_every_protocol(
    lab: LabRuntime, clients: dict[tuple[str, bool], InventoryClient], method: str, params: dict[str, Any]
) -> None:
    """Protobuf prenait ``None`` pour « champ absent » : gRPC exécutait l'appel avec 0 là où les autres refusent."""
    for protocol in PROTOCOLS:
        with pytest.raises(InvalidArgumentError):
            clients[protocol, False].invoke(method, params)
    assert lab.service.stats()["operations"] == 0


def test_unreachable_server_is_unavailable_on_every_protocol() -> None:
    """Connexion impossible : rien n'a été envoyé, donc UNAVAILABLE partout — jamais TIMEOUT (issue inconnue)."""
    port, bus = free_port(), EventBus()
    custom = CustomInventoryClient(HOST, port, bus=bus)
    custom.stub.connect_timeout = 0.3
    remote_clients: list[InventoryClient] = [
        custom,
        GrpcInventoryClient(HOST, port, connect_timeout=0.3, bus=bus),
        RestInventoryClient(HOST, port, connect_timeout=0.3, bus=bus),
    ]
    try:
        for client in remote_clients:
            with pytest.raises(RpcTransportError) as caught:
                client.calculate_factorial(5)
            assert caught.value.code == UNAVAILABLE, client.protocol
            assert not isinstance(caught.value, RpcTimeoutError), client.protocol
    finally:
        for client in remote_clients:
            client.close()


class _ContextSpy:
    """Contexte gRPC réduit à ce que les gardes du serveur utilisent ; retient les statuts envoyés."""

    def __init__(self) -> None:
        self.aborts: list[tuple[grpc.StatusCode, str]] = []

    def is_active(self) -> bool:
        return False

    def set_trailing_metadata(self, metadata: Any) -> None:
        pass

    def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.aborts.append((code, details))
        raise RuntimeError("context.abort")


@pytest.mark.parametrize("rpc", ["CheckStock", "BulkUpdateStock"])
def test_interrupted_grpc_request_stream_is_not_a_server_fault(rpc: str, caplog: pytest.LogCaptureFixture) -> None:
    """gRPC lève ``RpcError`` dans le flux de requêtes quand le client annule : le serveur la laisse
    remonter telle quelle, au lieu de journaliser une « erreur inattendue » et de répondre INTERNAL."""
    interruption = grpc.RpcError()

    def interrupted_requests() -> Iterator[Any]:
        raise interruption
        yield   # jamais atteint : fait de la fonction un générateur

    context = _ContextSpy()
    servicer = InventoryServicer(InventoryService())
    with caplog.at_level(logging.WARNING), pytest.raises(grpc.RpcError) as caught:
        reply = getattr(servicer, rpc)(interrupted_requests(), context)
        if isinstance(reply, Iterator):
            list(reply)
    assert caught.value is interruption
    assert context.aborts == [] and caplog.records == []


def test_abandoned_grpc_stream_never_surfaces_as_an_internal_error(
    lab: LabRuntime, caplog: pytest.LogCaptureFixture
) -> None:
    """De bout en bout : le client abandonne un flux bidirectionnel.

    Selon l'instant où l'annulation atteint le serveur, celui-ci voit une interruption du flux de
    requêtes (``server.error`` CANCELLED), sa simple fin (``server.send``), ou délaisse la procédure
    suspendue entre deux réponses (aucun évènement). Dans aucun cas ce n'est une panne du serveur.
    """
    events: list[TraceEvent] = []
    unsubscribe = lab.bus.subscribe(events.append)
    release = threading.Event()
    call_id = "grpc-abandoned-stream"

    def references() -> Iterator[pb.ProductRequest]:
        yield pb.ProductRequest(product_id="SKU-1001")
        release.wait(5.0)   # le client garde son flux de requêtes ouvert

    def server_endings() -> list[TraceEvent]:
        return [e for e in events if e.call_id == call_id and e.stage in ("server.error", "server.send")]

    client = lab.client("grpc")
    assert isinstance(client, GrpcInventoryClient)
    try:
        with caplog.at_level(logging.WARNING):
            call = client.traced_stub.CheckStock(references(), timeout=5.0, metadata=((CALL_ID_KEY, call_id),))
            assert next(call).stock == 84
            call.cancel()
            wait_until(lambda: bool(server_endings()), timeout=1.0)   # sans garantie : voir le troisième cas
    finally:
        release.set()
        unsubscribe()
        client.close()
    assert [record.getMessage() for record in caplog.records if record.name.startswith(("rpc_grpc", "grpc"))] == []
    for error in (event for event in server_endings() if event.stage == "server.error"):
        assert error.detail["code"] == CANCELLED and error.detail["grpc_status"] == "CANCELLED"
        assert "http2_trailers" not in error.detail     # personne n'écoute plus : aucun trailer n'est envoyé


def test_advised_port_offset_never_lands_on_another_lab() -> None:
    """Le message « port occupé » conseillait +100 : les serveurs tombaient alors sur les proxys du voisin."""
    occupied = set(Ports().as_dict().values())
    for neighbours in range(1, 6):
        shifted = set(Ports().shifted(neighbours * PORT_OFFSET_STEP).as_dict().values())
        assert not occupied & shifted, f"{neighbours} × {PORT_OFFSET_STEP} réutilise {sorted(occupied & shifted)}"
    assert occupied & set(Ports().shifted(100).as_dict().values())   # ce que le pas conseillé évite


def test_grpc_client_demo_prints_numbers_the_french_way(lab: LabRuntime, capsys: pytest.CaptureFixture[str]) -> None:
    assert grpc_client.main(["--host", lab.host, "--port", str(lab.ports.grpc)]) == 0
    output = capsys.readouterr().out
    assert "129,90 €" in output and "commandes/min" in output
    assert re.search(r"\d\.\d+ (ms|€|commandes)", output) is None, "séparateur décimal anglais dans la démonstration"
    assert lab.service.check_stock("SKU-1001")["stock"] == 84   # la démonstration restaure ce qu'elle retire
