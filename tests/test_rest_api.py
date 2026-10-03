"""Tests de l'API REST de référence : routes, erreurs, client, pannes et télémétrie."""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterator

import pytest

import rest_api
from common.client_api import LocalInventoryClient
from common.config import HOST, ROOT_DIR, Ports
from common.errors import (
    FailedPreconditionError,
    InternalRemoteError,
    InvalidArgumentError,
    MethodNotFoundError,
    NotFoundError,
    RpcError,
    RpcProtocolError,
    RpcTimeoutError,
    RpcTransportError,
)
from common.inventory import InventoryService
from common.telemetry import BUS, PIPELINE, EventBus, TraceEvent
from rest_api.rest_client import RestInventoryClient
from rest_api.rest_server import MAX_BODY_BYTES, RestServerHandle, create_rest_server

WIRE_STAGES = ("client.send", "server.receive", "server.send", "client.receive")


# --- Montage -----------------------------------------------------------------

@pytest.fixture
def bus() -> EventBus:
    """Bus privé : les traces de ces tests ne se mêlent pas à celles des autres modules."""
    return EventBus()


@pytest.fixture
def service() -> InventoryService:
    return InventoryService()


@pytest.fixture
def server(service: InventoryService, bus: EventBus) -> Iterator[RestServerHandle]:
    handle = create_rest_server(service, port=0, bus=bus).start()
    yield handle
    handle.stop()


@pytest.fixture
def client(server: RestServerHandle, bus: EventBus) -> Iterator[RestInventoryClient]:
    rest = RestInventoryClient(HOST, server.port, timeout=3.0, connect_timeout=0.4, bus=bus)
    yield rest
    rest.close()


@pytest.fixture
def local() -> LocalInventoryClient:
    """Référence « appel local » sur un inventaire distinct mais identique au départ."""
    return LocalInventoryClient(InventoryService(), bus=EventBus())


class RawPeer:
    """Faux serveur d'un seul échange : garde les octets reçus, répond par des octets écrits à la main."""

    def __init__(self, reply: bytes) -> None:
        self._listener = socket.create_server((HOST, 0))
        self.port: int = self._listener.getsockname()[1]
        self._reply = reply
        self._request = b""
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        with self._listener:
            connection, _ = self._listener.accept()
        with connection:
            connection.settimeout(3.0)
            data = b""
            while b"\r\n\r\n" not in data:
                data += connection.recv(65536)
            head, _, body = data.partition(b"\r\n\r\n")
            announced = re.search(rb"content-length: (\d+)", head, re.IGNORECASE)
            missing = (int(announced.group(1)) if announced else 0) - len(body)
            while missing > 0:
                chunk = connection.recv(65536)
                data += chunk
                missing -= len(chunk)
            self._request = data
            connection.sendall(self._reply)

    def request(self) -> bytes:
        """Octets réellement arrivés sur la socket (attend la fin de l'échange)."""
        self._thread.join(3.0)
        return self._request


@pytest.fixture
def peer_client(bus: EventBus) -> Iterator[Callable[[bytes], tuple[RawPeer, RestInventoryClient]]]:
    """Fabrique un couple (faux serveur, client branché dessus)."""
    clients: list[RestInventoryClient] = []

    def build(reply: bytes) -> tuple[RawPeer, RestInventoryClient]:
        peer = RawPeer(reply)
        rest = RestInventoryClient(HOST, peer.port, timeout=2.0, connect_timeout=1.0, bus=bus)
        clients.append(rest)
        return peer, rest

    yield build
    for rest in clients:
        rest.close()


def http_reply(status_line: str, body: bytes, content_type: str = "text/plain") -> bytes:
    head = f"HTTP/1.1 {status_line}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n\r\n"
    return head.encode("ascii") + body


def fetch(
    server: RestServerHandle, verb: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    """Requête HTTP indépendante du client testé (connexion jetable)."""
    connection = http.client.HTTPConnection(HOST, server.port, timeout=3.0)
    try:
        connection.request(verb, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def exchange_raw(server: RestServerHandle, request: bytes) -> bytes:
    """Envoie des octets bruts et lit la réponse jusqu'à la fermeture par le serveur."""
    with socket.create_connection((HOST, server.port), timeout=3.0) as sock:
        sock.sendall(request)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def trace_of(bus: EventBus, call_id: str) -> list[TraceEvent]:
    return [event for event in bus.recent(limit=4000) if event.call_id == call_id]


def stage(events: list[TraceEvent], name: str) -> TraceEvent:
    return next(event for event in events if event.stage == name)


def raised(call: Callable[[], Any]) -> RpcError:
    with pytest.raises(RpcError) as caught:
        call()
    return caught.value


def free_port() -> int:
    with socket.create_server((HOST, 0)) as probe:
        return probe.getsockname()[1]


# --- Routes du serveur ---------------------------------------------------------

def test_health_route(server: RestServerHandle) -> None:
    status, headers, body = fetch(server, "GET", "/api/health")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert int(headers["Content-Length"]) == len(body)
    assert json.loads(body)["status"] == "ok"


def test_factorial_route(server: RestServerHandle) -> None:
    status, _, body = fetch(server, "GET", "/api/factorial/5")
    result = json.loads(body)
    assert status == 200
    assert set(result) == {"n", "result", "digits", "compute_us"}
    assert (result["n"], result["result"], result["digits"]) == (5, "120", 3)


def test_product_route(server: RestServerHandle, service: InventoryService) -> None:
    status, _, body = fetch(server, "GET", "/api/products/SKU-1003")
    assert status == 200
    assert json.loads(body) == service.get_product_details("SKU-1003")


def test_listing_route_reads_the_query_string(server: RestServerHandle) -> None:
    status, _, body = fetch(server, "GET", "/api/products?limit=3&category=%C3%89crans")
    listing = json.loads(body)
    assert status == 200
    assert listing["total"] == 2
    assert [product["category"] for product in listing["products"]] == ["Écrans"] * 3


def test_listing_route_falls_back_on_service_defaults(server: RestServerHandle) -> None:
    for path in ("/api/products", "/api/products?limit=&category="):
        listing = json.loads(fetch(server, "GET", path)[2])
        assert (len(listing["products"]), listing["total"]) == (20, 24)


def test_stock_route_applies_the_json_body(server: RestServerHandle, service: InventoryService) -> None:
    status, _, body = fetch(server, "POST", "/api/stock/SKU-1001", b'{"delta": -4}')
    result = json.loads(body)
    assert status == 200
    assert (result["previous_stock"], result["new_stock"], result["applied"]) == (84, 80, True)
    assert service.get_product_details("SKU-1001")["stock"] == 80


def test_analytics_route_streams_chunked_ndjson(server: RestServerHandle) -> None:
    status, headers, body = fetch(server, "GET", "/api/analytics/stream?samples=3&interval_ms=0")
    assert status == 200
    assert headers["Transfer-Encoding"] == "chunked"
    assert headers["Content-Type"] == "application/x-ndjson"
    assert "Content-Length" not in headers
    assert [json.loads(line)["seq"] for line in body.splitlines()] == [1, 2, 3]


# --- Erreurs du serveur ----------------------------------------------------------

@pytest.mark.parametrize(
    ("verb", "path", "body", "status", "code"),
    [
        ("GET", "/api/factorial/douze", None, 400, "INVALID_ARGUMENT"),
        ("GET", "/api/factorial/-1", None, 400, "INVALID_ARGUMENT"),
        ("GET", "/api/factorial/1.5", None, 400, "INVALID_ARGUMENT"),
        ("GET", "/api/products?limit=dix", None, 400, "INVALID_ARGUMENT"),
        ("GET", "/api/analytics/stream?samples=x", None, 400, "INVALID_ARGUMENT"),
        ("GET", "/api/analytics/stream?samples=0", None, 400, "INVALID_ARGUMENT"),
        ("GET", "/api/products/INCONNU", None, 404, "NOT_FOUND"),
        ("POST", "/api/stock/INCONNU", b'{"delta": 1}', 404, "NOT_FOUND"),
        ("GET", "/api/inconnu", None, 404, "METHOD_NOT_FOUND"),
        ("GET", "/", None, 404, "METHOD_NOT_FOUND"),
        ("POST", "/api/factorial/5/extra", b"{}", 404, "METHOD_NOT_FOUND"),
        ("DELETE", "/api/products/SKU-1001", None, 405, "METHOD_NOT_FOUND"),
        ("GET", "/api/stock/SKU-1001", None, 405, "METHOD_NOT_FOUND"),
        ("POST", "/api/stock/SKU-1001", b'{"delta": -100000}', 409, "FAILED_PRECONDITION"),
        ("POST", "/api/stock/SKU-1001", b'{"delta": ', 400, "INVALID_ARGUMENT"),
        ("POST", "/api/stock/SKU-1001", b"", 400, "INVALID_ARGUMENT"),
        ("POST", "/api/stock/SKU-1001", b"[1, 2]", 400, "INVALID_ARGUMENT"),
        ("POST", "/api/stock/SKU-1001", b"{}", 400, "INVALID_ARGUMENT"),
        ("POST", "/api/stock/SKU-1001", b'{"delta": "trois"}', 400, "INVALID_ARGUMENT"),
    ],
)
def test_error_statuses_and_bodies(
    server: RestServerHandle, verb: str, path: str, body: bytes | None, status: int, code: str
) -> None:
    got_status, headers, payload = fetch(server, verb, path, body)
    error = json.loads(payload)["error"]
    assert got_status == status
    assert headers["Content-Type"] == "application/json"
    assert set(error) == {"code", "message", "detail"}
    assert error["code"] == code
    assert error["message"]


def test_wrong_verb_lists_the_allowed_ones(server: RestServerHandle) -> None:
    status, headers, _ = fetch(server, "PUT", "/api/stock/SKU-1001", b"{}")
    assert (status, headers["Allow"]) == (405, "POST")


def test_domain_error_detail_travels_in_the_body(server: RestServerHandle) -> None:
    missing = json.loads(fetch(server, "GET", "/api/products/INCONNU")[2])["error"]
    shortage = json.loads(fetch(server, "POST", "/api/stock/SKU-1001", b'{"delta": -500}')[2])["error"]
    assert missing["detail"] == {"product_id": "INCONNU"}
    assert shortage["detail"] == {"product_id": "SKU-1001", "stock": 84, "requested": 500}


def test_unexpected_exception_becomes_a_generic_500(
    server: RestServerHandle, service: InventoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(product_id: str) -> dict[str, Any]:
        raise RuntimeError("mot de passe de la base : hunter2")

    def unserializable(product_id: str) -> dict[str, Any]:
        return {"id": product_id, "handle": object()}

    for faulty in (explode, unserializable):
        monkeypatch.setattr(service, "get_product_details", faulty)
        status, _, body = fetch(server, "GET", "/api/products/SKU-1001")
        assert status == 500
        assert json.loads(body)["error"] == {
            "code": "INTERNAL", "message": "Erreur interne du serveur", "detail": None,
        }
        assert b"hunter2" not in body
    assert fetch(server, "GET", "/api/health")[0] == 200


@pytest.mark.parametrize(
    ("request_head", "status", "code"),
    [
        (b"POST /api/stock/SKU-1001 HTTP/1.1\r\nContent-Length: beaucoup\r\n\r\n", 400, "INVALID_ARGUMENT"),
        (
            b"POST /api/stock/SKU-1001 HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % (MAX_BODY_BYTES + 1),
            413,
            "INVALID_ARGUMENT",
        ),
        (b"POST /api/stock/SKU-1001 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n", 411, "INVALID_ARGUMENT"),
        (b"N'IMPORTE QUOI\r\n", 400, "INVALID_ARGUMENT"),
        (b"BREW /api/health HTTP/1.1\r\n\r\n", 501, "METHOD_NOT_FOUND"),
    ],
)
def test_malformed_http_is_refused_in_json_then_closed(
    server: RestServerHandle, request_head: bytes, status: int, code: str
) -> None:
    head, _, body = exchange_raw(server, request_head).partition(b"\r\n\r\n")
    error = json.loads(body)["error"]
    assert head.startswith(b"HTTP/1.1 %d " % status)
    assert b"Connection: close" in head
    assert set(error) == {"code", "message", "detail"}
    assert error["code"] == code


# --- Client : mêmes résultats que l'appel local ---------------------------------

@pytest.mark.parametrize(
    ("method", "params", "clock_keys"),
    [
        ("calculate_factorial", {"n": 25}, {"compute_us"}),
        ("calculate_factorial", {"n": 0}, {"compute_us"}),
        ("get_product_details", {"product_id": "SKU-1003"}, set()),
        ("update_stock", {"product_id": "SKU-1005", "delta": -7}, {"updated_at_ms"}),
        ("update_stock", {"product_id": "SKU-1005", "delta": 3, "idempotency_key": "clé-42"}, {"updated_at_ms"}),
        ("list_products", {}, set()),
        ("list_products", {"limit": 30, "category": "Périphériques"}, set()),
        ("list_products", {"limit": 0}, set()),
    ],
)
def test_unary_results_match_the_local_client(
    client: RestInventoryClient, local: LocalInventoryClient, method: str, params: dict[str, Any], clock_keys: set[str]
) -> None:
    remote, reference = client.invoke(method, params), local.invoke(method, params)
    assert list(remote) == list(reference)
    for key in reference:
        if key in clock_keys:  # mesure d'horloge : seule la nature de la valeur est comparable
            assert type(remote[key]) is type(reference[key])
        else:
            assert remote[key] == reference[key]


def test_stream_results_match_the_local_client(client: RestInventoryClient, local: LocalInventoryClient) -> None:
    remote = list(client.stream_analytics(samples=4, interval_ms=0))
    reference = list(local.stream_analytics(samples=4, interval_ms=0))
    assert len(remote) == len(reference) == 4
    for item, expected in zip(remote, reference):
        assert list(item) == list(expected)
        assert {**item, "timestamp_ms": 0} == {**expected, "timestamp_ms": 0}


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("calculate_factorial", {"n": -1}),
        ("calculate_factorial", {"n": 5001}),
        ("get_product_details", {"product_id": "INCONNU"}),
        ("get_product_details", {"product_id": ""}),
        ("update_stock", {"product_id": "SKU-1001", "delta": -500}),
        ("update_stock", {"product_id": "INCONNU", "delta": 1}),
        ("update_stock", {"product_id": "SKU-1001", "delta": "trois"}),
        ("list_products", {"limit": -1}),
        ("stream_analytics", {"samples": 0}),
    ],
)
def test_errors_match_the_local_client(
    client: RestInventoryClient, local: LocalInventoryClient, method: str, params: dict[str, Any]
) -> None:
    remote = raised(lambda: client.invoke(method, params))
    reference = raised(lambda: local.invoke(method, params))
    assert type(remote) is type(reference)
    assert (remote.code, remote.message, remote.detail) == (reference.code, reference.message, reference.detail)
    assert (remote.protocol, remote.method) == ("rest", method)


def test_arguments_survive_url_encoding(client: RestInventoryClient) -> None:
    awkward = "a/b ?&=#é%"
    error = raised(lambda: client.get_product_details(awkward))
    assert isinstance(error, NotFoundError)
    assert error.detail == {"product_id": awkward}
    assert {product["category"] for product in client.list_products(4, "Énergie")["products"]} == {"Énergie"}


def test_invoke_and_submit(client: RestInventoryClient) -> None:
    assert client.invoke("calculate_factorial", {"n": 6})["result"] == "720"
    assert client.last_call_id.startswith("rest-")
    with pytest.raises(MethodNotFoundError):
        client.invoke("bulk_update_stock", {"updates": []})
    futures = [client.submit("calculate_factorial", {"n": n}) for n in range(12)]
    assert [future.result(timeout=5)["result"] for future in futures] == [str(math.factorial(n)) for n in range(12)]


def test_package_exposes_its_entry_points() -> None:
    assert rest_api.create_rest_server is create_rest_server
    assert rest_api.RestInventoryClient is RestInventoryClient
    with pytest.raises(AttributeError):
        getattr(rest_api, "inexistant")


# --- Connexions ------------------------------------------------------------------

def test_keep_alive_reuses_a_single_connection(client: RestInventoryClient, server: RestServerHandle) -> None:
    for n in range(20):
        assert client.calculate_factorial(n)["n"] == n
    assert server.connections_accepted == 1


def test_no_nagle_penalty(client: RestInventoryClient) -> None:
    """Avec Nagle + ACK retardés, chaque appel coûterait ~40 ms : 120 appels dépasseraient 4 s."""
    started = time.perf_counter()
    for _ in range(60):
        client.calculate_factorial(10)
        client.update_stock("SKU-1011", 1)
    assert time.perf_counter() - started < 2.0


def test_stream_items_arrive_incrementally(client: RestInventoryClient) -> None:
    started = time.perf_counter()
    arrivals = [time.perf_counter() - started for _ in client.stream_analytics(samples=4, interval_ms=120)]
    assert len(arrivals) == 4
    # Trois pauses de 120 ms séparent le premier élément du dernier ; un corps mis en tampon les livrerait d'un bloc.
    assert arrivals[-1] - arrivals[0] > 0.25


def test_stream_leaves_the_thread_connection_free(client: RestInventoryClient, server: RestServerHandle) -> None:
    stream = client.stream_analytics(samples=3, interval_ms=0)
    assert next(stream)["seq"] == 1
    assert client.calculate_factorial(4)["result"] == "24"
    assert [item["seq"] for item in stream] == [2, 3]
    assert server.connections_accepted == 2


def test_timeout_then_recovery(
    client: RestInventoryClient,
    server: RestServerHandle,
    service: InventoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    genuine = service.get_product_details

    def stalled(product_id: str) -> dict[str, Any]:
        release.wait(5.0)
        return genuine(product_id)

    monkeypatch.setattr(service, "get_product_details", stalled)
    try:
        error = raised(lambda: client.get_product_details("SKU-1001", timeout=0.15))
    finally:
        release.set()
    assert isinstance(error, RpcTimeoutError)
    assert (error.code, error.retryable, error.detail) == ("TIMEOUT", True, {"timeout_s": 0.15})
    # La connexion douteuse a été jetée : la réponse tardive ne peut pas être prise pour celle de l'appel suivant.
    assert client.calculate_factorial(5)["result"] == "120"
    assert server.connections_accepted == 2


def test_exhausted_deadline_fails_without_touching_the_network(
    client: RestInventoryClient, server: RestServerHandle
) -> None:
    for budget in (0, -0.5):
        error = raised(lambda: client.calculate_factorial(3, timeout=budget))
        assert isinstance(error, RpcTimeoutError)
    assert server.connections_accepted == 0
    assert client.calculate_factorial(3)["result"] == "6"


def test_stopped_server_then_restart_on_the_same_port(client: RestInventoryClient, server: RestServerHandle) -> None:
    port = server.port
    assert client.calculate_factorial(4)["result"] == "24"
    server.stop()
    error = raised(lambda: client.calculate_factorial(4))
    assert type(error) is RpcTransportError  # panne franche, pas un délai dépassé
    assert (error.code, error.retryable, error.detail["phase"]) == ("UNAVAILABLE", True, "connect")
    server.start()
    assert server.port == port
    assert client.calculate_factorial(4)["result"] == "24"


def test_idle_connection_closed_by_the_peer_is_replaced_silently(
    client: RestInventoryClient, server: RestServerHandle
) -> None:
    client.calculate_factorial(3)
    server.stop()  # coupe la connexion keep-alive pendant qu'elle est au repos
    server.start()
    assert client.calculate_factorial(3)["result"] == "6"
    assert server.connections_accepted == 1


def test_another_server_can_take_over_the_port(server: RestServerHandle, client: RestInventoryClient) -> None:
    client.calculate_factorial(3)
    server.stop()
    with create_rest_server(InventoryService(), port=server.port, bus=EventBus()) as successor:
        assert successor.port == server.port
        assert client.calculate_factorial(3)["result"] == "6"
        assert successor.connections_accepted == 1


def test_connection_cut_during_the_call_is_a_transport_error(
    peer_client: Callable[[bytes], tuple[RawPeer, RestInventoryClient]], bus: EventBus
) -> None:
    _, rest = peer_client(b"")  # le pair lit la requête puis raccroche sans répondre
    error = raised(lambda: rest.update_stock("SKU-1001", -1))
    assert type(error) is RpcTransportError
    assert (error.code, error.detail["phase"]) == ("UNAVAILABLE", "exchange")
    assert [event.stage for event in trace_of(bus, rest.last_call_id)] == [
        "client.call", "client.marshal", "client.send", "client.error",
    ]


def test_stop_is_idempotent_and_closes_live_connections(server: RestServerHandle) -> None:
    with socket.create_connection((HOST, server.port), timeout=3.0) as sock:
        sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: test\r\n\r\n")
        assert sock.recv(65536).startswith(b"HTTP/1.1 200 OK\r\n")
        assert server.connections_active == 1
        server.stop()
        server.stop()
        assert sock.recv(65536) == b""  # fin de flux : le serveur a raccroché
    assert not server.running
    with socket.create_server((HOST, server.port)):
        pass  # le port est de nouveau libre


def test_busy_port_is_reported_clearly(server: RestServerHandle) -> None:
    with pytest.raises(OSError, match=f"Serveur REST : impossible d'écouter sur {HOST}:{server.port}"):
        create_rest_server(InventoryService(), port=server.port).start()


def test_refused_connection_is_a_transport_error(bus: EventBus) -> None:
    with RestInventoryClient(HOST, free_port(), timeout=1.0, connect_timeout=0.3, bus=bus) as rest:
        error = raised(lambda: rest.calculate_factorial(3))
    assert type(error) is RpcTransportError
    assert error.code == "UNAVAILABLE"
    assert error.detail["phase"] == "connect"
    assert [event.stage for event in trace_of(bus, rest.last_call_id)] == [
        "client.call", "client.marshal", "client.error",
    ]


def test_client_reconnects_after_close(client: RestInventoryClient, server: RestServerHandle) -> None:
    client.calculate_factorial(3)
    client.close()
    assert client.calculate_factorial(3)["result"] == "6"
    assert server.connections_accepted == 2


def test_connections_of_finished_threads_are_reclaimed(client: RestInventoryClient, server: RestServerHandle) -> None:
    for _ in range(3):
        worker = threading.Thread(target=client.calculate_factorial, args=(3,))
        worker.start()
        worker.join()
    assert server.connections_accepted == 3
    client.calculate_factorial(3)  # une nouvelle connexion s'ouvre : celles des threads terminés sont fermées
    deadline = time.monotonic() + 3.0
    while server.connections_active > 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (server.connections_accepted, server.connections_active) == (4, 1)


def test_stream_can_be_consumed_by_another_thread(client: RestInventoryClient) -> None:
    stream = client.stream_analytics(samples=3, interval_ms=0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert [item["seq"] for item in pool.submit(list, stream).result(timeout=5)] == [1, 2, 3]


def test_thread_safety(client: RestInventoryClient, server: RestServerHandle, service: InventoryService) -> None:
    threads, calls = 8, 25
    initial = service.get_product_details("SKU-1011")["stock"]
    together = threading.Barrier(threads)

    def worker(index: int) -> list[tuple[int, str]]:
        together.wait(timeout=10)  # les huit threads travaillent en même temps, chacun sur sa connexion
        seen = []
        for step in range(calls):
            n = index * calls + step
            seen.append((n, client.calculate_factorial(n)["result"]))
            assert client.update_stock("SKU-1011", 1)["applied"]
        return seen

    with ThreadPoolExecutor(max_workers=threads) as pool:
        outcomes = list(pool.map(worker, range(threads)))
    for seen in outcomes:
        assert seen == [(n, str(math.factorial(n))) for n, _ in seen]
    assert service.get_product_details("SKU-1011")["stock"] == initial + threads * calls
    assert server.connections_accepted == threads  # une connexion persistante par thread


# --- Réponses hors contrat ---------------------------------------------------------

@pytest.mark.parametrize(
    ("status_line", "error_type", "code"),
    [
        ("503 Service Unavailable", RpcTransportError, "UNAVAILABLE"),
        ("504 Gateway Timeout", RpcTimeoutError, "TIMEOUT"),
        ("404 Not Found", MethodNotFoundError, "METHOD_NOT_FOUND"),
        ("500 Internal Server Error", InternalRemoteError, "INTERNAL"),
        ("418 I'm a teapot", InternalRemoteError, "INTERNAL"),
    ],
)
def test_error_without_json_body_falls_back_on_the_status(
    peer_client: Callable[[bytes], tuple[RawPeer, RestInventoryClient]],
    status_line: str,
    error_type: type[RpcError],
    code: str,
) -> None:
    _, rest = peer_client(http_reply(status_line, b"<html>panne</html>", "text/html"))
    error = raised(lambda: rest.calculate_factorial(3))
    assert type(error) is error_type
    assert (error.code, error.message) == (code, f"HTTP {status_line}")
    assert error.detail == {"http_status": int(status_line[:3])}


@pytest.mark.parametrize(
    ("status_line", "code", "error_type"),
    [
        ("400 Bad Request", "INVALID_ARGUMENT", InvalidArgumentError),
        ("404 Not Found", "NOT_FOUND", NotFoundError),  # ressource absente : le corps départage les deux 404
        ("404 Not Found", "METHOD_NOT_FOUND", MethodNotFoundError),
        ("405 Method Not Allowed", "METHOD_NOT_FOUND", MethodNotFoundError),
        ("409 Conflict", "FAILED_PRECONDITION", FailedPreconditionError),
        ("500 Internal Server Error", "INTERNAL", InternalRemoteError),
        ("503 Service Unavailable", "CODE_INCONNU", InternalRemoteError),
    ],
)
def test_error_body_code_wins_over_the_http_status(
    peer_client: Callable[[bytes], tuple[RawPeer, RestInventoryClient]],
    status_line: str,
    code: str,
    error_type: type[RpcError],
) -> None:
    body = json.dumps({"error": {"code": code, "message": "refusé", "detail": {"raison": 1}}}).encode()
    _, rest = peer_client(http_reply(status_line, body, "application/json"))
    error = raised(lambda: rest.calculate_factorial(3))
    assert type(error) is error_type
    assert (error.code, error.message, error.detail) == (code, "refusé", {"raison": 1})


def test_unreadable_success_body_is_a_protocol_error(
    peer_client: Callable[[bytes], tuple[RawPeer, RestInventoryClient]]
) -> None:
    _, rest = peer_client(http_reply("200 OK", b"ceci n'est pas du JSON"))
    assert isinstance(raised(lambda: rest.calculate_factorial(3)), RpcProtocolError)


def test_garbage_instead_of_http_is_a_protocol_error(
    peer_client: Callable[[bytes], tuple[RawPeer, RestInventoryClient]]
) -> None:
    _, rest = peer_client(b"BONJOUR\r\n\r\n")
    assert isinstance(raised(lambda: rest.calculate_factorial(3)), RpcProtocolError)


def test_truncated_stream_is_a_transport_error(
    peer_client: Callable[[bytes], tuple[RawPeer, RestInventoryClient]]
) -> None:
    line = b'{"seq":1}\n'
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\n\r\n"
    _, rest = peer_client(head + b"%x\r\n%b\r\n" % (len(line), line))  # pas de fragment final
    stream = rest.stream_analytics(samples=2, interval_ms=0)
    assert next(stream) == {"seq": 1}
    error = raised(lambda: next(stream))
    assert type(error) is RpcTransportError


# --- Télémétrie ------------------------------------------------------------------

def test_trace_follows_the_pipeline_under_one_call_id(client: RestInventoryClient, bus: EventBus) -> None:
    for n in range(30):  # répété : l'ordre ne doit rien au hasard de l'ordonnancement des threads
        client.calculate_factorial(n)
        events = trace_of(bus, client.last_call_id)
        assert [event.stage for event in events] == list(PIPELINE)
        assert [event.side for event in events] == ["client"] * 3 + ["server"] * 6 + ["client"] * 3
        assert {(event.protocol, event.method) for event in events} == {("rest", "calculate_factorial")}
    assert stage(events, "client.call").detail == {"args": [], "kwargs": {"n": 29}}
    assert stage(events, "server.dispatch").detail["bound_args"] == {"n": 29}
    assert stage(events, "server.dispatch").detail["target"] == "InventoryService.calculate_factorial"
    assert stage(events, "client.return").detail["result_preview"].startswith('{"n":29,')
    timed = set(PIPELINE) - {"client.call", "server.receive"}
    assert all(event.duration_us is not None and event.duration_us >= 0 for event in events if event.stage in timed)
    assert stage(events, "client.return").duration_us >= stage(events, "client.receive").duration_us


def test_trace_payloads_are_the_bytes_exchanged(client: RestInventoryClient, bus: EventBus) -> None:
    client.update_stock("SKU-1001", -2, idempotency_key="clé")
    posted = trace_of(bus, client.last_call_id)
    client.get_product_details("SKU-1001")
    fetched = trace_of(bus, client.last_call_id)

    body = '{"delta":-2,"idempotency_key":"clé"}'.encode()
    assert stage(posted, "client.marshal").payload == body
    assert stage(posted, "client.marshal").detail == {
        "http": {"method": "POST", "path": "/api/stock/SKU-1001", "status": None}, "text": body.decode(),
    }
    assert stage(posted, "client.send").payload.endswith(b"\r\n\r\n" + body)
    assert stage(fetched, "client.marshal").payload == b""
    assert stage(fetched, "client.marshal").size == 0
    assert stage(fetched, "client.marshal").detail == {
        "http": {"method": "GET", "path": "/api/products/SKU-1001", "status": None},
    }
    assert json.loads(stage(fetched, "server.marshal").detail["text"])["id"] == "SKU-1001"
    for events in (posted, fetched):
        # Chaque côté capture indépendamment : l'égalité prouve que la trace montre les octets du fil.
        assert stage(events, "client.send").payload == stage(events, "server.receive").payload
        assert stage(events, "server.send").payload == stage(events, "client.receive").payload
        assert stage(events, "server.send").payload.endswith(stage(events, "server.marshal").payload)
        assert f"X-Call-Id: {events[0].call_id}\r\n".encode() in stage(events, "client.send").payload


def test_wire_segments_cover_the_payload_without_gaps(client: RestInventoryClient, bus: EventBus) -> None:
    client.update_stock("SKU-1001", -2)
    posted = trace_of(bus, client.last_call_id)
    client.calculate_factorial(7)
    fetched = trace_of(bus, client.last_call_id)

    for events, verb in ((posted, "POST"), (fetched, "GET")):
        for name in WIRE_STAGES:
            event = stage(events, name)
            segments = event.detail["segments"]
            assert segments[0]["start"] == 0
            assert all(before["end"] == after["start"] for before, after in zip(segments, segments[1:]))
            assert segments[-1]["end"] == len(event.payload) == event.size
            first, headers, *rest = (event.payload[part["start"]:part["end"]] for part in segments)
            is_request = name in ("client.send", "server.receive")
            assert segments[0]["label"] == ("Ligne de requête" if is_request else "Ligne de statut")
            assert first.startswith(verb.encode() + b" /api/" if is_request else b"HTTP/1.1 200 OK")
            assert first.endswith(b"\r\n") and first.count(b"\r\n") == 1
            assert segments[1]["label"] == "En-têtes HTTP" and headers.endswith(b"\r\n\r\n")
            assert [part["kind"] for part in segments[:2]] == ["header", "header"]
            if is_request and verb == "GET":
                assert rest == []
            else:
                assert (segments[2]["label"], segments[2]["kind"]) == ("Corps JSON", "body")
                assert isinstance(json.loads(rest[0]), dict)
            assert event.detail["http"]["method"] == verb
            assert event.detail["http"]["status"] == (None if is_request else 200)
    assert stage(posted, "client.send").detail["http"]["path"] == "/api/stock/SKU-1001"


def test_client_send_size_is_the_real_byte_count(
    peer_client: Callable[[bytes], tuple[RawPeer, RestInventoryClient]], bus: EventBus
) -> None:
    reply = http_reply("200 OK", b'{"ok":true}', "application/json")
    peer, rest = peer_client(reply)
    assert rest.update_stock("SKU-1001", -1, idempotency_key="abc") == {"ok": True}
    on_the_wire = peer.request()
    events = trace_of(bus, rest.last_call_id)
    sent, received = stage(events, "client.send"), stage(events, "client.receive")
    assert on_the_wire.startswith(b"POST /api/stock/SKU-1001 HTTP/1.1\r\n")
    assert (sent.size, sent.payload) == (len(on_the_wire), on_the_wire)
    assert (received.size, received.payload) == (len(reply), reply)


def test_trace_of_a_remote_error(client: RestInventoryClient, bus: EventBus) -> None:
    raised(lambda: client.get_product_details("INCONNU"))
    events = trace_of(bus, client.last_call_id)
    assert [event.stage for event in events] == [
        "client.call", "client.marshal", "client.send",
        "server.receive", "server.unmarshal", "server.dispatch", "server.error", "server.marshal", "server.send",
        "client.receive", "client.unmarshal", "client.error",
    ]
    failure = stage(events, "client.error")
    assert (failure.detail["code"], failure.detail["message"]) == ("NOT_FOUND", "Produit inconnu : INCONNU")
    assert failure.duration_us > 0
    assert stage(events, "server.error").detail["http_status"] == 404
    assert stage(events, "client.receive").detail["http"]["status"] == 404


def test_trace_of_a_stream(client: RestInventoryClient, bus: EventBus) -> None:
    items = list(client.stream_analytics(samples=3, interval_ms=0))
    events = trace_of(bus, client.last_call_id)
    by_side = {side: [event for event in events if event.side == side] for side in ("client", "server")}
    assert [event.stage for event in by_side["client"]] == [
        "client.call", "client.marshal", "client.send", "client.receive",
        "client.stream_item", "client.stream_item", "client.stream_item", "client.return",
    ]
    assert [event.stage for event in by_side["server"]] == [
        "server.receive", "server.unmarshal", "server.dispatch", "server.execute", "server.send",
        "server.stream_item", "server.stream_item", "server.stream_item",
    ]
    for side in by_side.values():
        elements = [event for event in side if event.stage.endswith("stream_item")]
        assert [json.loads(event.payload) for event in elements] == items
        assert [event.detail["seq"] for event in elements] == [1, 2, 3]
    assert stage(events, "client.return").detail["count"] == 3
    assert stage(events, "client.receive").payload == stage(events, "server.send").payload


def test_abandoned_stream_ends_its_trace(client: RestInventoryClient, bus: EventBus) -> None:
    stream = client.stream_analytics(samples=5, interval_ms=0)
    next(stream)
    stream.close()
    # Le serveur, lui, peut encore publier ses derniers éléments : on ne regarde que le côté client.
    terminal = [event for event in trace_of(bus, client.last_call_id) if event.side == "client"][-1]
    assert (terminal.stage, terminal.detail["code"]) == ("client.error", "CANCELLED")
    assert client.calculate_factorial(3)["result"] == "6"


def test_server_traces_foreign_clients_but_not_health_probes(server: RestServerHandle, bus: EventBus) -> None:
    fetch(server, "GET", "/api/health")
    assert bus.recent() == []
    fetch(server, "GET", "/api/factorial/5", headers={"X-Call-Id": "curl-1"})
    fetch(server, "GET", "/api/nulle-part")
    named = trace_of(bus, "curl-1")
    assert [event.stage for event in named] == [name for name in PIPELINE if name.startswith("server.")]
    anonymous = [event for event in bus.recent() if event.call_id != "curl-1"]
    assert [event.stage for event in anonymous] == ["server.receive", "server.error", "server.marshal", "server.send"]
    assert len({event.call_id for event in anonymous}) == 1 and anonymous[0].call_id.startswith("rest-")


def test_muted_bus_costs_and_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[str] = []

    def spy(**event: Any) -> None:
        if event["protocol"] == "rest":
            emitted.append(event["stage"])

    monkeypatch.setattr(BUS, "emit", spy)
    with create_rest_server(InventoryService(), port=0) as server:
        with RestInventoryClient(HOST, server.port, timeout=3.0) as rest:
            with BUS.muted():
                assert rest.calculate_factorial(5)["result"] == "120"
                assert rest.update_stock("SKU-1001", -1)["new_stock"] == 83
                assert len(list(rest.stream_analytics(samples=2, interval_ms=0))) == 2
                raised(lambda: rest.get_product_details("INCONNU"))
                assert rest.last_call_id.startswith("rest-")
                assert emitted == []
            rest.calculate_factorial(5)
            assert emitted == list(PIPELINE)


# --- Lancement autonome -------------------------------------------------------------

def test_module_serves_on_the_default_port() -> None:
    port = free_port()
    environment = {**os.environ, "RPCX_PORT_OFFSET": str(port - Ports().rest)}
    process = subprocess.Popen(
        [sys.executable, "-m", "rest_api.rest_server"],
        cwd=ROOT_DIR, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    watchdog = threading.Timer(20.0, process.kill)  # un serveur muet ne doit pas bloquer la suite de tests
    watchdog.start()
    try:
        banner = process.stdout.readline().decode("utf-8")
        assert f"à l'écoute sur http://{HOST}:{port}" in banner
        with RestInventoryClient(HOST, port, timeout=3.0, bus=EventBus()) as rest:
            assert rest.calculate_factorial(5)["result"] == "120"
    finally:
        watchdog.cancel()
        process.kill()
        _, errors = process.communicate(timeout=5)
    assert errors == b""
