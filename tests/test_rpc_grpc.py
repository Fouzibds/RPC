"""gRPC de bout en bout : serveur réel sur port éphémère, quatre formes d'appel, erreurs, traces, contrat v2."""
from __future__ import annotations

import json
import socket
import time
from typing import Any, Iterator, NamedTuple

import grpc
import pytest

from common.catalog import CORE_METHODS
from common.client_api import LocalInventoryClient
from common.config import HOST, MAX_FACTORIAL_N
from common.errors import (
    FailedPreconditionError,
    InsufficientStock,
    InternalRemoteError,
    InvalidArgumentError,
    MethodNotFoundError,
    NotFoundError,
    RpcTimeoutError,
    RpcTransportError,
)
from common.inventory import InventoryService
from common.telemetry import BUS, PIPELINE, EventBus, TraceCollector, TraceEvent
from rpc_grpc import converters as conv
from rpc_grpc.generated import service_pb2 as pb
from rpc_grpc.generated import service_v2_pb2 as pb_v2
from rpc_grpc.grpc_client import GrpcInventoryClient
from rpc_grpc.grpc_server import INTERNAL_MESSAGE, GrpcServerHandle, create_grpc_server
from rpc_grpc.grpc_server_v2 import V1_SERVICE_NAME, create_grpc_server_v2
from rpc_grpc.interceptors import CALL_ID_KEY, TracingServerInterceptor

VOLATILE_KEYS = {"compute_us", "updated_at_ms", "timestamp_ms"}   # horodatages et durées mesurées
SECRET = "détail interne à ne jamais divulguer"
SLOW_CALL_S = 0.4
SLOW_ITEM_S = 0.1
BROKEN_STREAM, SLOW_STREAM = 6, 9   # valeurs de « samples » qui déclenchent les pièges de FaultyService


class _HandlerCallDetails(NamedTuple):
    """Ce que gRPC remet à un intercepteur serveur : chemin de la méthode et métadonnées reçues."""

    method: str
    invocation_metadata: tuple


class FaultyService(InventoryService):
    """Service piégé : lenteur, exception inattendue et flux qui casse en cours de route."""

    def calculate_factorial(self, n: int) -> dict[str, Any]:
        if n == 13:
            raise RuntimeError(SECRET)
        if n == 7:
            time.sleep(SLOW_CALL_S)
        return super().calculate_factorial(n)

    def stream_analytics(self, samples: int = 10, interval_ms: int = 200) -> Iterator[dict[str, Any]]:
        snapshots = super().stream_analytics(samples, interval_ms)

        def trapped() -> Iterator[dict[str, Any]]:
            for snapshot in snapshots:
                if samples == BROKEN_STREAM and snapshot["seq"] == 3:
                    raise InsufficientStock("Flux interrompu côté serveur", after=2)
                if samples == SLOW_STREAM:
                    time.sleep(SLOW_ITEM_S)   # plus lent que l'intervalle annoncé
                yield snapshot

        return trapped()


# --- Fixtures ----------------------------------------------------------------

@pytest.fixture(scope="module")
def service() -> InventoryService:
    return InventoryService()


@pytest.fixture(scope="module")
def server(service):
    handle = create_grpc_server(service).start()
    yield handle
    handle.stop()


@pytest.fixture()
def client(server, service):
    service.reset()
    with GrpcInventoryClient(HOST, server.port) as grpc_client:
        yield grpc_client


@pytest.fixture(scope="module")
def faulty_server():
    handle = create_grpc_server(FaultyService()).start()
    yield handle
    handle.stop()


@pytest.fixture()
def faulty_client(faulty_server):
    with GrpcInventoryClient(HOST, faulty_server.port) as grpc_client:
        yield grpc_client


@pytest.fixture()
def v2():
    """Serveur « contrat v2 » sur son propre inventaire, et un client resté en v1."""
    service = InventoryService()
    handle = create_grpc_server_v2(service).start()
    with GrpcInventoryClient(HOST, handle.port) as grpc_client:
        yield handle, service, grpc_client
    handle.stop()


@pytest.fixture()
def events():
    captured: list[TraceEvent] = []
    unsubscribe = BUS.subscribe(captured.append)
    yield captured
    unsubscribe()


@pytest.fixture()
def isolated():
    """Serveur et client sur un bus privé : aucun évènement d'un autre appel ne peut s'y glisser."""
    bus = EventBus()
    captured: list[TraceEvent] = []
    bus.subscribe(captured.append)
    handle = create_grpc_server(InventoryService(), bus=bus).start()
    with GrpcInventoryClient(HOST, handle.port, bus=bus) as grpc_client:
        yield bus, grpc_client, captured
    handle.stop()


def _of_call(events: list[TraceEvent], call_id: str) -> list[TraceEvent]:
    return [event for event in events if event.call_id == call_id]


def _stage(events: list[TraceEvent], stage: str) -> TraceEvent:
    return next(event for event in events if event.stage == stage)


def _wait_for_stage(events: list[TraceEvent], call_id: str, stage: str, timeout: float = 2.0) -> None:
    """Attend une étape publiée par un autre fil (le serveur, après que le client a renoncé)."""
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if any(event.stage == stage for event in _of_call(events, call_id)):
            return
        time.sleep(0.005)
    raise AssertionError(f"{stage} jamais publié pour {call_id}")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def _comparable(value: Any) -> Any:
    """Résultat débarrassé de ses valeurs volatiles, pour comparer deux exécutions."""
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in value.items() if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    return value


def _shape(value: Any) -> Any:
    """Squelette clés + types d'un résultat : c'est lui qui doit être identique d'un protocole à l'autre."""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    return type(value).__name__


# --- Cycle de vie du serveur -------------------------------------------------

def test_server_handle_lifecycle():
    handle = create_grpc_server(InventoryService())
    assert handle.port == 0 and not handle.running

    assert handle.start() is handle
    port = handle.port
    assert port > 0 and handle.running and handle.address == f"{HOST}:{port}"
    assert handle.start().port == port   # déjà démarré : sans effet
    with GrpcInventoryClient(HOST, port) as grpc_client:
        assert grpc_client.calculate_factorial(5)["result"] == "120"

    handle.stop()
    handle.stop()   # idempotent
    assert not handle.running
    assert handle.start().port == port   # relançable sur le même port
    handle.stop()


def test_port_already_in_use_is_a_clear_error(server):
    with pytest.raises(OSError, match="déjà utilisé"):
        create_grpc_server(InventoryService(), port=server.port).start()


# --- Appels unaires ----------------------------------------------------------

def test_calculate_factorial(client):
    reply = client.calculate_factorial(25)

    assert reply["n"] == 25 and reply["result"] == "15511210043330985984000000"
    assert reply["digits"] == 26 and isinstance(reply["compute_us"], float)


def test_get_product_details_returns_the_business_dictionary(client, service):
    assert client.get_product_details("SKU-1004") == service.get_product_details("SKU-1004")


def test_update_stock_applies_a_negative_delta_and_honours_idempotency(client, service):
    first = client.update_stock("SKU-1001", -3, idempotency_key="commande-42")
    replay = client.update_stock("SKU-1001", -3, idempotency_key="commande-42")

    assert (first["previous_stock"], first["new_stock"], first["delta"]) == (84, 81, -3)
    assert first["applied"] is True and replay["applied"] is False
    assert service.get_product_details("SKU-1001")["stock"] == 81


def test_list_products_filters_and_carries_large_payloads(client):
    screens = client.list_products(limit=5, category="Écrans")
    large = client.list_products(limit=400)

    assert screens["total"] == 2 and len(screens["products"]) == 5
    assert {product["category"] for product in screens["products"]} == {"Écrans"}
    assert len(large["products"]) == 400 and large["total"] == 24


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("calculate_factorial", {"n": 30}),
        ("get_product_details", {"product_id": "SKU-1008"}),
        ("update_stock", {"product_id": "SKU-1005", "delta": -7, "idempotency_key": "k-1"}),
        ("list_products", {"limit": 30, "category": ""}),
        ("stream_analytics", {"samples": 4, "interval_ms": 0}),
    ],
)
def test_results_are_identical_to_a_local_call(client, method, params):
    assert method in CORE_METHODS
    local = LocalInventoryClient(InventoryService())

    remote_result = client.invoke(method, params)
    local_result = local.invoke(method, params)
    if method == "stream_analytics":
        remote_result, local_result = list(remote_result), list(local_result)

    assert _comparable(remote_result) == _comparable(local_result)
    assert _shape(remote_result) == _shape(local_result)


# --- Traduction des erreurs --------------------------------------------------

def test_not_found_maps_to_not_found_error(client):
    with pytest.raises(NotFoundError) as caught:
        client.get_product_details("SKU-9999")

    error = caught.value
    assert error.code == "NOT_FOUND" and error.message == "Produit inconnu : SKU-9999"
    assert error.protocol == "grpc" and error.method == "get_product_details"
    assert error.detail == {"grpc_status": "NOT_FOUND", "product_id": "SKU-9999"}


def test_failed_precondition_carries_structured_detail(client):
    with pytest.raises(FailedPreconditionError) as caught:
        client.update_stock("SKU-1001", -10_000)

    assert caught.value.code == "FAILED_PRECONDITION"
    assert caught.value.detail == {
        "grpc_status": "FAILED_PRECONDITION", "product_id": "SKU-1001", "stock": 84, "requested": 10_000,
    }


def test_invalid_argument_rejected_by_the_server(client):
    with pytest.raises(InvalidArgumentError) as caught:
        client.calculate_factorial(MAX_FACTORIAL_N + 1)

    assert caught.value.detail["grpc_status"] == "INVALID_ARGUMENT"
    assert str(MAX_FACTORIAL_N) in caught.value.message


def test_invalid_argument_rejected_by_the_contract_before_sending(client, service):
    with pytest.raises(InvalidArgumentError) as negative:
        client.calculate_factorial(-1)   # uint32 : le code généré refuse la valeur
    with pytest.raises(InvalidArgumentError):
        client.update_stock("SKU-1001", "trois")

    assert negative.value.code == "INVALID_ARGUMENT" and negative.value.detail == {"rejected_by": "client"}
    assert service.stats()["calls"] == {}   # rien n'a atteint le serveur


def test_unexpected_exception_becomes_internal_without_leaking(faulty_client):
    with pytest.raises(InternalRemoteError) as caught:
        faulty_client.calculate_factorial(13)

    assert caught.value.code == "INTERNAL" and caught.value.message == INTERNAL_MESSAGE
    assert SECRET not in str(caught.value.to_dict())


def test_deadline_exceeded_maps_to_timeout(faulty_client, events):
    started = time.perf_counter()
    with pytest.raises(RpcTimeoutError) as caught:
        faulty_client.calculate_factorial(7, timeout=0.1)
    elapsed = time.perf_counter() - started

    assert caught.value.code == "TIMEOUT" and caught.value.retryable
    assert caught.value.detail["grpc_status"] == "DEADLINE_EXCEEDED"
    assert 0.09 < elapsed < SLOW_CALL_S   # l'échéance interrompt l'attente avant la fin du traitement
    trace = _of_call(events, faulty_client.last_call_id)
    assert trace[-1].stage == "client.error" and trace[-1].detail["code"] == "TIMEOUT"
    # Le serveur, lui, va au bout de la procédure : son résultat part vers un client qui n'écoute plus.
    _wait_for_stage(events, faulty_client.last_call_id, "server.send")


def test_unreachable_server_fails_fast_with_transport_error():
    with GrpcInventoryClient(HOST, _free_port()) as grpc_client:
        started = time.perf_counter()
        with pytest.raises(RpcTransportError) as caught:
            grpc_client.calculate_factorial(5)
        first_failure = time.perf_counter() - started

        started = time.perf_counter()
        with pytest.raises(RpcTransportError):
            grpc_client.get_product_details("SKU-1001")
        second_failure = time.perf_counter() - started

    assert caught.value.code == "UNAVAILABLE" and caught.value.retryable
    assert first_failure < 3.0
    assert second_failure < 0.5   # canal déjà en échec : refus immédiat, sans nouvelle attente


def test_short_connect_timeout_bounds_the_first_failure():
    with GrpcInventoryClient(HOST, _free_port(), connect_timeout=0.2) as grpc_client:
        started = time.perf_counter()
        with pytest.raises(RpcTransportError):
            grpc_client.calculate_factorial(5)

    assert time.perf_counter() - started < 1.0


# --- Flux --------------------------------------------------------------------

def test_server_streaming_yields_ordered_snapshots(client):
    snapshots = list(client.stream_analytics(samples=5, interval_ms=5))

    assert [snapshot["seq"] for snapshot in snapshots] == [1, 2, 3, 4, 5]
    assert all(snapshot["total_units"] == 1707 for snapshot in snapshots)
    assert all(isinstance(snapshot["orders_per_min"], float) for snapshot in snapshots)


def test_server_streaming_error_before_first_item(client):
    stream = client.stream_analytics(samples=0)

    with pytest.raises(InvalidArgumentError, match="samples"):
        next(stream)


def test_server_streaming_error_in_the_middle_of_the_stream(faulty_client):
    received = []

    with pytest.raises(FailedPreconditionError, match="Flux interrompu") as caught:
        for snapshot in faulty_client.stream_analytics(samples=BROKEN_STREAM, interval_ms=0):
            received.append(snapshot["seq"])

    assert received == [1, 2]
    assert caught.value.detail == {"grpc_status": "FAILED_PRECONDITION", "after": 2}


def test_stream_on_schedule_outlives_its_timeout(client, events):
    snapshots = list(client.stream_analytics(samples=4, interval_ms=60, timeout=0.15))

    assert len(snapshots) == 4   # 180 ms de flux pour 150 ms de délai : l'échéance inclut la durée annoncée
    sent = _stage(_of_call(events, client.last_call_id), "client.send")
    assert sent.detail["http2_headers"]["grpc-timeout"] == "390m"


def test_stream_slower_than_announced_hits_the_deadline(faulty_client):
    received = []

    started = time.perf_counter()
    with pytest.raises(RpcTimeoutError) as caught:
        for snapshot in faulty_client.stream_analytics(samples=SLOW_STREAM, interval_ms=0, timeout=0.25):
            received.append(snapshot["seq"])

    assert caught.value.code == "TIMEOUT" and len(received) < SLOW_STREAM
    assert time.perf_counter() - started < SLOW_STREAM * SLOW_ITEM_S   # interrompu en plein flux


def test_client_streaming_returns_a_single_summary(client, service):
    updates = [
        {"product_id": "SKU-1001", "delta": -2},
        {"product_id": "SKU-1005", "delta": 10, "idempotency_key": "lot-1"},
        {"product_id": "SKU-9999", "delta": 1},
        {"product_id": "SKU-1017", "delta": -500},
    ]

    summary = client.bulk_update_stock(updates)

    assert summary == LocalInventoryClient(InventoryService()).bulk_update_stock(updates)
    assert summary["applied"] == 2 and summary["rejected"] == 2 and summary["total_delta"] == 8
    assert service.get_product_details("SKU-1005")["stock"] == 220


def test_bidirectional_streaming_keeps_going_after_an_unknown_product(client):
    levels = list(client.check_stock(["SKU-1001", "SKU-9999", "SKU-1022"]))

    assert [level["product_id"] for level in levels] == ["SKU-1001", "SKU-9999", "SKU-1022"]
    assert levels[0] == {"product_id": "SKU-1001", "stock": 84, "available": True, "warehouse": "PAR-01"}
    assert levels[1] == {"product_id": "SKU-9999", "stock": 0, "available": False, "warehouse": ""}
    assert levels[2]["stock"] == 3 and levels[2]["available"] is True


def test_bidirectional_streaming_aborts_on_a_malformed_request(client):
    received = []

    with pytest.raises(InvalidArgumentError):
        for level in client.invoke("check_stock", {"product_ids": ["SKU-1001", ""]}):
            received.append(level["product_id"])

    assert received == ["SKU-1001"]


def test_async_submissions_share_the_channel(client):
    futures = [client.submit("calculate_factorial", {"n": n}) for n in range(1, 13)]

    assert [future.result(timeout=5)["n"] for future in futures] == list(range(1, 13))


# --- Télémétrie --------------------------------------------------------------

def test_unary_trace_follows_the_pipeline_with_a_shared_call_id(client, events):
    client.update_stock("SKU-1001", -3)

    trace = _of_call(events, client.last_call_id)
    assert client.last_call_id.startswith("grpc-")
    assert [event.stage for event in trace] == list(PIPELINE)
    assert [event.side for event in trace] == ["client"] * 3 + ["server"] * 6 + ["client"] * 3
    assert {event.protocol for event in trace} == {"grpc"}
    assert {event.method for event in trace} == {"update_stock"}

    by_stage = {event.stage: event for event in trace}
    for stage in ("client.marshal", "client.send", "server.unmarshal", "server.dispatch", "server.execute",
                  "server.marshal", "server.send", "client.receive", "client.unmarshal", "client.return"):
        assert by_stage[stage].duration_us is not None and by_stage[stage].duration_us >= 0
    assert by_stage["client.return"].duration_us >= by_stage["client.receive"].duration_us
    assert by_stage["client.receive"].duration_us >= by_stage["server.execute"].duration_us
    assert by_stage["client.call"].detail["kwargs"] == {"product_id": "SKU-1001", "delta": -3, "idempotency_key": ""}
    assert by_stage["client.call"].detail["kind"] == "unary"
    assert by_stage["server.dispatch"].detail["target"] == "InventoryServicer.UpdateStock"
    assert by_stage["server.dispatch"].detail["bound_args"]["request"]["delta"] == -3
    assert by_stage["client.return"].detail["result_preview"]["new_stock"] == 81


def test_trace_payloads_are_the_real_wire_bytes(client, events):
    client.update_stock("SKU-1001", -3)

    by_stage = {event.stage: event for event in _of_call(events, client.last_call_id)}
    request = pb.UpdateStockRequest(product_id="SKU-1001", delta=-3).SerializeToString()
    assert by_stage["client.marshal"].payload == request
    assert by_stage["client.send"].payload == b"\x00\x00\x00\x00\x0c" + request   # préfixe gRPC de 5 octets
    assert by_stage["server.receive"].payload == by_stage["client.send"].payload

    serialized_reply = by_stage["server.marshal"].payload
    reply = pb.UpdateStockReply.FromString(serialized_reply)
    assert (reply.previous_stock, reply.new_stock, reply.delta) == (84, 81, -3)
    assert by_stage["server.send"].payload == b"\x00" + len(serialized_reply).to_bytes(4, "big") + serialized_reply
    assert by_stage["client.receive"].payload == by_stage["server.send"].payload

    assert by_stage["client.marshal"].size == 12 and by_stage["client.send"].size == 17
    assert by_stage["client.receive"].size == len(serialized_reply) + 5
    assert by_stage["client.marshal"].detail["message_type"] == "rpcexplorer.v1.UpdateStockRequest"
    assert 'product_id: "SKU-1001"' in by_stage["client.marshal"].detail["text"]


def test_trace_exposes_http2_pseudo_headers(client, events):
    client.update_stock("SKU-1001", -3, timeout=1.5)

    by_stage = {event.stage: event for event in _of_call(events, client.last_call_id)}
    headers = by_stage["client.send"].detail["http2_headers"]
    assert headers[":method"] == "POST" and headers[":scheme"] == "http"
    assert headers[":path"] == f"/{V1_SERVICE_NAME}/UpdateStock"
    assert headers[":authority"] == client.target
    assert headers["content-type"] == "application/grpc" and headers["te"] == "trailers"
    assert headers["grpc-timeout"] == "1500m"   # l'échéance voyage avec la requête
    assert headers[CALL_ID_KEY] == client.last_call_id
    received = by_stage["server.receive"].detail["http2_headers"]
    assert received[":path"] == headers[":path"] and received[CALL_ID_KEY] == client.last_call_id
    assert by_stage["client.receive"].detail["http2_trailers"] == {"grpc-status": "0"}


def test_segments_cover_each_wire_payload_without_gaps(client, events):
    client.get_product_details("SKU-1003")

    trace = _of_call(events, client.last_call_id)
    for stage in ("client.send", "server.receive", "server.send", "client.receive"):
        event = _stage(trace, stage)
        segments = event.detail["segments"]
        assert [segment["kind"] for segment in segments[:2]] == ["frame", "frame"]
        assert segments[0]["start"] == 0 and segments[-1]["end"] == len(event.payload)
        for previous, current in zip(segments, segments[1:]):
            assert current["start"] == previous["end"]
        assert event.detail["frame_header_hex"] == event.payload[:5].hex()
        assert {segment["kind"] for segment in segments[2:]} == {"tag", "len", "value"}
    received = _stage(trace, "client.receive").detail["segments"]
    name = next(segment for segment in received if segment.get("field") == "name" and segment["kind"] == "value")
    assert name["value"] == 'Écran 27" QHD Lumen 27Q' and name["depth"] == 0
    assert any(segment.get("field") == "weight_kg" and segment["depth"] == 1 for segment in received)


def test_large_payload_trace_stays_bounded(client, events):
    client.list_products(limit=600)

    received = _stage(_of_call(events, client.last_call_id), "client.receive")
    segments = received.detail["segments"]
    assert received.size > 100_000 and received.detail["payload_truncated"] is True
    assert len(segments) < 500 and segments[-1]["kind"] == "body" and segments[-1]["end"] == received.size


def test_error_trace_ends_with_client_error(client, events):
    collector = TraceCollector().start()
    try:
        with pytest.raises(NotFoundError):
            client.get_product_details("SKU-9999")
    finally:
        collector.stop()

    trace = _of_call(events, client.last_call_id)
    assert [event.stage for event in trace] == [
        "client.call", "client.marshal", "client.send",
        "server.receive", "server.unmarshal", "server.dispatch", "server.execute", "server.error",
        "client.error",
    ]
    assert _stage(trace, "server.error").detail["grpc_status"] == "NOT_FOUND"
    assert _stage(trace, "client.error").detail == {
        "code": "NOT_FOUND", "message": "Produit inconnu : SKU-9999", "grpc_status": "NOT_FOUND",
    }
    summary = collector.get(client.last_call_id).summary()
    assert summary["status"] == "error" and summary["error"]["code"] == "NOT_FOUND"
    assert summary["request_bytes"] == 15 and summary["server_us"] is not None


def test_contract_rejection_is_traced_without_any_network_stage(client, events):
    with pytest.raises(InvalidArgumentError):
        client.calculate_factorial(-1)

    trace = _of_call(events, client.last_call_id)
    assert [event.stage for event in trace] == ["client.call", "client.error"]
    assert trace[0].detail["kwargs"] == {"n": -1}
    assert trace[1].detail["code"] == "INVALID_ARGUMENT"


def test_server_streaming_trace_has_one_item_event_per_message(client, events):
    snapshots = list(client.stream_analytics(samples=3, interval_ms=0))

    trace = _of_call(events, client.last_call_id)
    stages = [event.stage for event in trace]
    assert stages.count("server.stream_item") == stages.count("client.stream_item") == 3
    assert [stage for stage in stages if not stage.endswith("stream_item")] == list(PIPELINE)
    items = [event for event in trace if event.stage == "client.stream_item"]
    assert [pb.AnalyticsSnapshot.FromString(event.payload).seq for event in items] == [1, 2, 3]
    assert [event.detail["seq"] for event in items] == [1, 2, 3]
    assert {event.detail["direction"] for event in items} == {"response"}
    assert items[0].detail["segments"][0]["start"] == 0 and items[0].detail["segments"][-1]["end"] == items[0].size
    server_items = [event for event in trace if event.stage == "server.stream_item"]
    assert [event.payload for event in server_items] == [event.payload for event in items]
    assert _stage(trace, "client.receive").size == sum(event.size + 5 for event in items)
    assert trace[-1].detail["result_preview"] == {"stream": "end", "count": len(snapshots)}


def test_request_streams_are_traced_in_both_directions(client, events):
    client.bulk_update_stock([{"product_id": "SKU-1001", "delta": 1}, {"product_id": "SKU-1002", "delta": 2}])
    bulk = _of_call(events, client.last_call_id)
    list(client.check_stock(["SKU-1001", "SKU-1002"]))
    bidi = _of_call(events, client.last_call_id)

    sent = [event for event in bulk if event.stage == "client.stream_item"]
    assert [event.detail["direction"] for event in sent] == ["request", "request"]
    assert [event.payload for event in bulk if event.stage == "server.stream_item"] == [event.payload for event in sent]
    assert _stage(bulk, "client.send").size == sum(event.size + 5 for event in sent)
    assert _stage(bulk, "client.call").detail["kind"] == "client_stream"
    assert bulk[-1].stage == "client.return" and bulk[-1].detail["result_preview"]["applied"] == 2

    directions = [(event.side, event.detail["direction"]) for event in bidi if event.stage.endswith("stream_item")]
    assert sorted(directions) == sorted(
        [("client", "request"), ("server", "request"), ("server", "response"), ("client", "response")] * 2
    )
    assert _stage(bidi, "client.call").detail["kind"] == "bidi_stream"
    assert bidi[-1].stage == "client.return" and bidi[-1].detail["result_preview"]["count"] == 2


def test_nothing_is_emitted_while_the_bus_is_muted(isolated):
    bus, client, events = isolated
    client.calculate_factorial(3)
    traced_call_id, traced_events = client.last_call_id, len(events)
    assert traced_events == len(PIPELINE)

    with bus.muted():
        client.calculate_factorial(10)
        client.update_stock("SKU-1001", 1)
        list(client.stream_analytics(samples=2, interval_ms=0))
        client.bulk_update_stock([{"product_id": "SKU-1001", "delta": 1}])
        list(client.check_stock(["SKU-1001"]))
        with pytest.raises(NotFoundError):
            client.get_product_details("SKU-9999")
        with pytest.raises(InvalidArgumentError):
            client.calculate_factorial(-1)

    assert len(events) == traced_events          # ni le client ni le serveur n'ont publié quoi que ce soit
    assert client.last_call_id == traced_call_id   # aucun identifiant de corrélation n'a même été créé


def test_plain_generated_stub_is_not_traced_but_traced_stub_is(isolated):
    _, client, events = isolated

    plain = client.grpc_stub.CalculateFactorial(pb.FactorialRequest(n=6))
    assert plain.result == "720" and events == []   # pas de x-call-id : le serveur ne publie rien non plus

    traced, call = client.traced_stub.CalculateFactorial.with_call(pb.FactorialRequest(n=6))

    assert traced.result == "720" and call.code() is grpc.StatusCode.OK
    assert [event.stage for event in events] == list(PIPELINE)
    assert len({event.call_id for event in events}) == 1 and events[0].call_id.startswith("grpc-")
    assert "grpc-timeout" not in _stage(events, "client.send").detail["http2_headers"]


def test_undecodable_reply_fails_the_same_way_with_and_without_tracing(events):
    garbage = b"\x0a\x05ab"   # champ de 5 octets annoncé, 2 présents : pas un FactorialReply

    def register(grpc_server: grpc.Server) -> None:
        handler = grpc.unary_unary_rpc_method_handler(
            lambda request, context: garbage, request_deserializer=pb.FactorialRequest.FromString
        )
        grpc_server.add_generic_rpc_handlers(
            (grpc.method_handlers_generic_handler(V1_SERVICE_NAME, {"CalculateFactorial": handler}),)
        )

    with GrpcServerHandle(register, HOST, 0, max_workers=2, bus=BUS) as handle:
        with GrpcInventoryClient(HOST, handle.port) as grpc_client:
            with pytest.raises(InternalRemoteError) as traced:
                grpc_client.calculate_factorial(4)
            call_id = grpc_client.last_call_id
            with BUS.muted(), pytest.raises(InternalRemoteError) as plain:
                grpc_client.calculate_factorial(4)

    assert traced.value.message == plain.value.message == "Exception deserializing response!"
    trace = _of_call(events, call_id)
    assert trace[-1].stage == "client.error" and trace[-1].detail["grpc_status"] == "INTERNAL"
    received = _stage(trace, "client.receive")
    assert received.payload == b"\x00\x00\x00\x00\x04" + garbage
    assert [segment["kind"] for segment in received.detail["segments"]] == ["frame", "frame", "body"]


def test_abandoned_stream_is_cancelled_and_traced(client, events):
    stream = client.stream_analytics(samples=50, interval_ms=20)

    assert next(stream)["seq"] == 1
    stream.close()   # l'appelant s'arrête avant la fin : le flux HTTP/2 doit être libéré

    client_side = [event for event in _of_call(events, client.last_call_id) if event.side == "client"]
    assert client_side[-1].stage == "client.error" and client_side[-1].detail["code"] == "CANCELLED"
    assert [event.stage for event in client_side].count("client.stream_item") == 1


def test_trace_events_are_json_serialisable(client, events):
    client.get_product_details("SKU-1001")
    list(client.stream_analytics(samples=2, interval_ms=0))
    client.bulk_update_stock([{"product_id": "SKU-1001", "delta": 1}])
    with pytest.raises(NotFoundError):
        client.get_product_details("SKU-9999")

    assert len(events) > 40
    for event in events:
        json.dumps(event.to_dict(), allow_nan=False)


def test_server_interceptor_leaves_untraced_calls_untouched():
    handler = grpc.unary_unary_rpc_method_handler(lambda request, context: request)
    interceptor = TracingServerInterceptor(BUS)
    details = _HandlerCallDetails("/rpcexplorer.v1.InventoryService/CalculateFactorial", (("user-agent", "test"),))
    traced_details = details._replace(invocation_metadata=((CALL_ID_KEY, "grpc-test"),))

    assert interceptor.intercept_service(lambda _: handler, details) is handler   # pas de x-call-id
    with BUS.muted():
        assert interceptor.intercept_service(lambda _: handler, traced_details) is handler
    wrapped = interceptor.intercept_service(lambda _: handler, traced_details)
    assert wrapped is not handler and wrapped.unary_unary is not handler.unary_unary


# --- Contrat v2 --------------------------------------------------------------

def test_v2_renamed_method_is_unimplemented(v2, events):
    _, _, v1_client = v2

    with pytest.raises(MethodNotFoundError) as caught:
        v1_client.get_product_details("SKU-1001")

    assert caught.value.code == "METHOD_NOT_FOUND" and caught.value.detail["grpc_status"] == "UNIMPLEMENTED"
    trace = _of_call(events, v1_client.last_call_id)
    assert [event.stage for event in trace] == [
        "client.call", "client.marshal", "client.send", "server.error", "client.error",
    ]
    assert _stage(trace, "server.error").detail["code"] == "METHOD_NOT_FOUND"


def test_v2_reused_field_number_corrupts_silently(v2, events):
    _, service, v1_client = v2

    reply = v1_client.update_stock("SKU-1001", -3)   # aucune exception : c'est tout le problème

    assert reply["applied"] is True and reply["delta"] == 0
    assert reply["previous_stock"] == reply["new_stock"] == 84
    assert service.get_product_details("SKU-1001")["stock"] == 84
    trace = _of_call(events, v1_client.last_call_id)
    assert trace[-1].stage == "client.return"
    flagged = [segment for segment in _stage(trace, "server.receive").detail["segments"] if segment.get("mismatch")]
    assert {segment["field"] for segment in flagged} == {"warehouse"}
    assert _stage(trace, "server.dispatch").detail["bound_args"]["request"] == {
        "product_id": "SKU-1001", "warehouse": "", "idempotency_key": "", "delta": 0,
    }


def test_v2_strict_mode_turns_corruption_into_a_rejection(v2):
    handle, service, v1_client = v2
    assert handle.strict is False

    handle.strict = True
    with pytest.raises(InvalidArgumentError, match="warehouse") as caught:
        v1_client.update_stock("SKU-1001", -3)
    handle.strict = False
    relaxed = v1_client.update_stock("SKU-1001", -3)

    assert caught.value.detail == {"grpc_status": "INVALID_ARGUMENT", "param": "warehouse"}
    assert relaxed["delta"] == 0 and service.get_product_details("SKU-1001")["stock"] == 84


def test_v2_added_field_stays_compatible(v2):
    _, _, v1_client = v2

    reply = v1_client.calculate_factorial(10)

    assert reply["result"] == "3628800" and set(reply) == {"n", "result", "digits", "compute_us"}


def test_v1_client_misreads_a_v2_product_list(v2, events):
    _, service, v1_client = v2

    listing = v1_client.list_products(limit=3)

    assert listing["total"] == 24
    for product in listing["products"]:
        real = service.get_product_details(product["id"])
        assert product["price"] == 0.0                    # double attendu, varint reçu : champ ignoré
        assert product["stock"] == real["stock"] // 10    # « reserved » pris pour le stock
        assert product["name"] == real["name"] and product["dimensions"] == real["dimensions"]
    received = _stage(_of_call(events, v1_client.last_call_id), "client.receive").detail["segments"]
    assert {segment["field"] for segment in received if segment.get("mismatch")} == {"price"}


def test_v2_server_serves_an_up_to_date_client_correctly(v2):
    handle, service, _ = v2
    handle.strict = True
    path = f"/{V1_SERVICE_NAME}/"

    with grpc.insecure_channel(handle.address) as channel:
        update = channel.unary_unary(
            path + "UpdateStock",
            request_serializer=pb_v2.UpdateStockRequest.SerializeToString,
            response_deserializer=pb_v2.UpdateStockReply.FromString,
        )
        get_product = channel.unary_unary(
            path + "GetProduct",
            request_serializer=pb_v2.ProductRequest.SerializeToString,
            response_deserializer=pb_v2.Product.FromString,
        )
        factorial = channel.unary_unary(
            path + "CalculateFactorial",
            request_serializer=pb_v2.FactorialRequest.SerializeToString,
            response_deserializer=pb_v2.FactorialReply.FromString,
        )
        reply = update(pb_v2.UpdateStockRequest(product_id="SKU-1001", warehouse="PAR-01", delta=-3), timeout=5)
        product = get_product(pb_v2.ProductRequest(product_id="SKU-1001"), timeout=5)
        algorithm = factorial(pb_v2.FactorialRequest(n=5, use_cache=True), timeout=5).algorithm

    assert (reply.previous_stock, reply.new_stock, reply.delta) == (84, 81, -3)
    assert (product.price_cents, product.stock, product.reserved) == (12990, 81, 8)
    assert algorithm == conv.V2_FACTORIAL_ALGORITHM
    assert service.get_product_details("SKU-1001")["stock"] == 81


# --- Conversions -------------------------------------------------------------

@pytest.mark.parametrize("method", sorted(conv.CODECS))
def test_converters_round_trip_business_dictionaries(method):
    service = InventoryService()
    requests = {
        "calculate_factorial": {"n": 12},
        "get_product_details": {"product_id": "SKU-1016"},
        "update_stock": {"product_id": "SKU-1016", "delta": -4, "idempotency_key": "k"},
        "list_products": {"limit": 3, "category": "Audio"},
        "stream_analytics": {"samples": 2, "interval_ms": 0},
        "bulk_update_stock": {"product_id": "SKU-1016", "delta": 2, "idempotency_key": ""},
        "check_stock": {"product_id": "SKU-1016"},
    }
    replies = {
        "calculate_factorial": service.calculate_factorial(12),
        "get_product_details": service.get_product_details("SKU-1016"),
        "update_stock": service.update_stock("SKU-1016", -4),
        "list_products": service.list_products(3, "Audio"),
        "stream_analytics": next(service.stream_analytics(2, 0)),
        "bulk_update_stock": service.bulk_update_stock([{"product_id": "SKU-1016", "delta": 2}, {"product_id": "x"}]),
        "check_stock": service.check_stock("SKU-1016"),
    }
    codec = conv.CODECS[method]

    request = codec.request_from_proto(type(codec.request_to_proto(requests[method])).FromString(
        codec.request_to_proto(requests[method]).SerializeToString()
    ))
    reply_message = codec.reply_to_proto(replies[method])
    reply = codec.reply_from_proto(type(reply_message).FromString(reply_message.SerializeToString()))

    assert request == requests[method]
    assert reply == replies[method] and _shape(reply) == _shape(replies[method])
    assert hasattr(pb.DESCRIPTOR.services_by_name["InventoryService"].methods_by_name[codec.rpc], "input_type")


def test_error_detail_survives_the_metadata_round_trip():
    data = {"product_id": "SKU-1001", "stock": 3, "note": "référence épuisée"}

    metadata = conv.error_detail_to_metadata(data)

    assert metadata[0][0] == conv.ERROR_DETAIL_KEY and isinstance(metadata[0][1], bytes)
    assert conv.error_detail_from_metadata(metadata) == data
    assert conv.error_detail_to_metadata({}) == ()
    assert conv.error_detail_from_metadata(None) == {}
    assert conv.error_detail_from_metadata(((conv.ERROR_DETAIL_KEY, b"\xff pas du JSON"),)) == {}
