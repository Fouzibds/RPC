"""Tests du middleware RPC maison (Phase 1) : protocole, squelette, stub, branchement métier."""
from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
from typing import Any, Iterator

import pytest
from jsonrpc import JSONRPCResponseManager
from jsonrpc.dispatcher import Dispatcher

from common import errors
from common.client_api import LocalInventoryClient
from common.config import HOST
from common.errors import (
    DomainError,
    FailedPreconditionError,
    InsufficientStock,
    InternalRemoteError,
    InvalidArgument,
    InvalidArgumentError,
    MethodNotFoundError,
    NotFoundError,
    ProductNotFound,
    RpcError,
    RpcProtocolError,
    RpcRemoteError,
    RpcTimeoutError,
    RpcTransportError,
)
from common.inventory import InventoryService
from common.telemetry import BUS, PIPELINE, EventBus, TraceEvent
from rpc_custom import protocol
from rpc_custom.client_stub import RpcClientStub, RpcFuture
from rpc_custom.inventory_binding import (
    CustomInventoryClient,
    build_inventory_skeleton,
    build_inventory_skeleton_v2,
)
from rpc_custom.protocol import ErrorObject, Request, Response
from rpc_custom.server_skeleton import RpcServerSkeleton


# --- Procédures d'essai ----------------------------------------------------------

class Unavailable(DomainError):
    """Erreur métier dont le code canonique n'a pas de code JSON-RPC attitré."""

    code = errors.UNAVAILABLE


DOMAIN_FAILURES: dict[str, DomainError] = {
    "invalid": InvalidArgument("argument refusé", param="x"),
    "missing": ProductNotFound("produit introuvable", product_id="SKU-0"),
    "conflict": InsufficientStock("stock insuffisant", stock=1, requested=5),
    "generic": DomainError("échec métier"),
    "unmapped": Unavailable("entrepôt fermé"),
}


def add(a: int, b: int = 0) -> int:
    """Additionne deux entiers."""
    return a + b


def subtract(minuend: int, subtrahend: int) -> int:
    return minuend - subtrahend


def total(*values: int) -> int:
    return sum(values)


def get_data() -> list[Any]:
    return ["hello", 5]


def echo(value: Any) -> Any:
    return value


def nap(ms: int, tag: Any = None) -> Any:
    time.sleep(ms / 1000)
    return tag


def boom() -> None:
    raise RuntimeError("secret interne 4242")


def unserializable() -> Any:
    return {1, 2, 3}


def fail(kind: str) -> None:
    raise DOMAIN_FAILURES[kind]


def countdown(n: int) -> Iterator[int]:
    yield from range(n, 0, -1)


def ticker(count: int, gap_ms: int) -> Iterator[int]:
    for index in range(count):
        if index:
            time.sleep(gap_ms / 1000)
        yield index


def broken_stream() -> Iterator[int]:
    yield 1
    yield 2
    raise ProductNotFound("flux interrompu", product_id="SKU-0")


class Recorder:
    """Procédure à effet de bord, pour observer qu'une notification a bien été exécutée."""

    def __init__(self) -> None:
        self.values: list[Any] = []
        self.called = threading.Event()

    def record(self, value: Any) -> None:
        self.values.append(value)
        self.called.set()


def make_server(bus: EventBus, port: int = 0) -> RpcServerSkeleton:
    skeleton = RpcServerSkeleton(HOST, port, bus=bus)
    for procedure in (add, subtract, get_data, echo, nap, boom, unserializable, fail, countdown, ticker,
                      broken_stream):
        skeleton.register(procedure)
    skeleton.register(total, "sum")
    skeleton.register(echo, "notify_hello")
    return skeleton


class RawClient:
    """Client minimal : écrit et lit des trames brutes, sans passer par le stub."""

    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection((HOST, port), timeout=3.0)

    def send(self, body: bytes) -> None:
        self.sock.sendall(protocol.frame(body))

    def send_json(self, document: Any) -> None:
        self.send(json.dumps(document).encode("utf-8"))

    def receive(self) -> Any:
        body = protocol.read_frame(self.sock)
        return None if body is None else json.loads(body)

    def exchange(self, document: Any) -> Any:
        self.send_json(document)
        return self.receive()

    def assert_silent(self, seconds: float = 0.15) -> None:
        """Vérifie qu'aucun octet n'arrive pendant ``seconds``."""
        self.sock.settimeout(seconds)
        try:
            with pytest.raises(TimeoutError):
                self.sock.recv(1)
        finally:
            self.sock.settimeout(3.0)

    def close(self) -> None:
        self.sock.close()


class DribbleSocket:
    """Fausse socket : ne rend jamais plus de ``chunk`` octets par lecture, comme un réseau lent."""

    def __init__(self, data: bytes, chunk: int = 1) -> None:
        self.data = data
        self.chunk = chunk
        self.position = 0

    def recv_into(self, buffer: memoryview) -> int:
        piece = self.data[self.position:self.position + min(self.chunk, len(buffer))]
        buffer[:len(piece)] = piece
        self.position += len(piece)
        return len(piece)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.perf_counter() + timeout
    while not predicate():
        assert time.perf_counter() < deadline, "condition non atteinte à temps"
        time.sleep(0.005)


def request(method: str, params: Any = None, request_id: Any = 1) -> dict[str, Any]:
    document: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": request_id}
    if params is not None:
        document["params"] = params
    return document


def stages_of(bus: EventBus, call_id: str) -> list[str]:
    return [event.stage for event in bus.recent() if event.call_id == call_id]


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def server(bus: EventBus, recorder: Recorder) -> Iterator[RpcServerSkeleton]:
    skeleton = make_server(bus)
    skeleton.register(recorder.record)
    skeleton.start()
    yield skeleton
    skeleton.stop()


@pytest.fixture
def stub(server: RpcServerSkeleton, bus: EventBus) -> Iterator[RpcClientStub]:
    with RpcClientStub(HOST, server.port, timeout=3.0, connect_timeout=0.3, bus=bus) as client:
        yield client


@pytest.fixture
def raw(server: RpcServerSkeleton) -> Iterator[RawClient]:
    client = RawClient(server.port)
    yield client
    client.close()


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Journal des connexions TCP ouvertes pendant le test."""
    opened: list[tuple[str, int]] = []
    original = socket.create_connection

    def counting(address: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
        opened.append(address)
        return original(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", counting)
    return opened


# --- Tramage ---------------------------------------------------------------------

def test_frame_is_a_big_endian_length_followed_by_the_body() -> None:
    assert protocol.HEADER_SIZE == 4
    assert protocol.frame(b'{"a":1}') == b"\x00\x00\x00\x07" + b'{"a":1}'
    assert protocol.frame(b"") == b"\x00\x00\x00\x00"


@pytest.mark.parametrize("chunk", [1, 3, 5, 4096])
def test_read_frame_reassembles_partial_reads(chunk: int) -> None:
    first, second = b'{"first":"message"}', "é".encode("utf-8") * 300
    sock = DribbleSocket(protocol.frame(first) + protocol.frame(second), chunk)
    assert protocol.read_frame(sock) == first
    assert protocol.read_frame(sock) == second
    assert protocol.read_frame(sock) is None


def test_read_frame_waits_for_a_frame_split_across_tcp_segments() -> None:
    left, right = socket.socketpair()
    data = protocol.frame(b"x" * 5000)

    def dribble() -> None:
        for start, end in ((0, 2), (2, 4), (4, 1000), (1000, len(data))):
            left.sendall(data[start:end])
            time.sleep(0.01)

    writer = threading.Thread(target=dribble)
    writer.start()
    try:
        assert protocol.read_frame(right) == b"x" * 5000
    finally:
        writer.join()
        left.close()
        right.close()


def test_read_frame_returns_none_on_clean_close() -> None:
    assert protocol.read_frame(DribbleSocket(b"")) is None


@pytest.mark.parametrize("cut", [2, 4, 10], ids=["mid-header", "no-body", "mid-body"])
def test_read_frame_rejects_a_truncated_frame(cut: int) -> None:
    truncated = protocol.frame(b'{"jsonrpc":"2.0"}')[:cut]
    with pytest.raises(RpcProtocolError):
        protocol.read_frame(DribbleSocket(truncated, chunk=3))


def test_read_frame_rejects_an_oversize_length_before_reading_the_body() -> None:
    sock = DribbleSocket(struct.pack(">I", protocol.MAX_FRAME_BYTES + 1) + b"x" * 64, chunk=64)
    with pytest.raises(RpcProtocolError, match="trop volumineuse"):
        protocol.read_frame(sock)
    assert sock.position == protocol.HEADER_SIZE


def test_oversize_messages_are_refused_at_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "MAX_FRAME_BYTES", 32)
    assert len(protocol.frame(b"x" * 32)) == 36
    with pytest.raises(RpcProtocolError):
        protocol.frame(b"x" * 33)
    with pytest.raises(RpcProtocolError):
        protocol.encode_request(Request("echo", ["x" * 64], 1))


def test_describe_frame_splits_header_and_body() -> None:
    data = protocol.frame(b'{"jsonrpc":"2.0"}')
    layout = protocol.describe_frame(data)
    assert layout["frame_header_hex"] == "00000011"
    assert [(s["start"], s["end"], s["kind"]) for s in layout["segments"]] == [(0, 4, "frame"), (4, 21, "body")]
    assert layout["segments"][0]["value"] == 17


# --- Messages : encodage et décodage ----------------------------------------------

@pytest.mark.parametrize("message", [
    Request("add", [2, 3], 1),
    Request("add", {"a": 2, "b": 3}, "custom-000001"),
    Request("get_data", None, 7),
    Request("log", ["évènement"], is_notification=True),
    Request("ping", None, is_notification=True),
    Request("odd", [None, True, 1.5, {"k": []}], None),
    Response(1, result=5),
    Response("custom-000001", result={"produit": "Écran 27\"", "tags": ["a", "b"]}),
    Response(3, result=None),
    Response(None, error=ErrorObject(-32700, "Parse error")),
    Response("x", error=ErrorObject(-32001, "Produit inconnu", {"canonical": "NOT_FOUND", "product_id": "SKU-0"})),
])
def test_message_round_trip(message: Request | Response) -> None:
    encode = protocol.encode_request if isinstance(message, Request) else protocol.encode_response
    assert protocol.decode_message(encode(message)) == message


def test_batch_round_trip() -> None:
    batch = [Request("add", [1, 2], 1), Request("log", ["x"], is_notification=True), Response(1, result=3)]
    assert protocol.decode_message(protocol.encode_batch(batch)) == batch


def test_encoding_is_compact_utf8_json_in_spec_order() -> None:
    body = protocol.encode_request(Request("echo", {"nom": "Écran"}, 1))
    assert body == '{"jsonrpc":"2.0","method":"echo","params":{"nom":"Écran"},"id":1}'.encode("utf-8")
    assert protocol.encode_response(Response(1, result=[1, 2])) == b'{"jsonrpc":"2.0","result":[1,2],"id":1}'


def test_notification_is_a_request_without_id_member() -> None:
    assert "id" not in json.loads(protocol.encode_request(Request("log", [1], is_notification=True)))
    assert json.loads(protocol.encode_request(Request("log", [1])))["id"] is None
    decoded = protocol.decode_message(b'{"jsonrpc":"2.0","method":"log","id":null}')
    assert decoded == Request("log", None, None, is_notification=False)


@pytest.mark.parametrize(("payload", "code"), [
    (b"", protocol.PARSE_ERROR),
    (b'{"jsonrpc":"2.0","method":"add"', protocol.PARSE_ERROR),
    (b"\xff\xfe\x00", protocol.PARSE_ERROR),
    (b"42", protocol.INVALID_REQUEST),
    (b"[]", protocol.INVALID_REQUEST),
    (b"[1]", protocol.INVALID_REQUEST),
    (b'{"method":"add","id":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"1.0","method":"add","id":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":2.0,"method":"add","id":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","method":7,"id":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","method":"add","params":"1,2","id":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","method":"add","params":null,"id":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","method":"add","id":[1]}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","method":"add","id":true}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","id":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","result":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","result":1,"error":{"code":1,"message":"x"},"id":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","error":"oops","id":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","error":{"code":"1","message":"x"},"id":1}', protocol.INVALID_REQUEST),
    (b'{"jsonrpc":"2.0","error":{"code":1},"id":1}', protocol.INVALID_REQUEST),
])
def test_decode_rejects_malformed_messages(payload: bytes, code: int) -> None:
    with pytest.raises(RpcProtocolError) as caught:
        protocol.decode_message(payload)
    assert caught.value.detail == {"jsonrpc_code": code}


@pytest.mark.parametrize("value", [{1, 2}, object(), float("nan"), float("inf")])
def test_encode_rejects_values_json_cannot_carry(value: Any) -> None:
    with pytest.raises(RpcProtocolError):
        protocol.encode_response(Response(1, result=value))


@pytest.mark.parametrize(("failure", "code", "canonical"), [
    (InvalidArgument("x", param="n"), -32602, "INVALID_ARGUMENT"),
    (ProductNotFound("x", product_id="SKU-0"), -32001, "NOT_FOUND"),
    (InsufficientStock("x", stock=1), -32002, "FAILED_PRECONDITION"),
    (DomainError("x"), -32603, "INTERNAL"),
    (Unavailable("x"), -32000, "UNAVAILABLE"),
])
def test_exception_to_error_maps_every_domain_error(failure: DomainError, code: int, canonical: str) -> None:
    error = protocol.exception_to_error(failure)
    assert (error.code, error.message) == (code, "x")
    assert error.data == {"canonical": canonical, **failure.data}


def test_exception_to_error_hides_unexpected_exceptions() -> None:
    error = protocol.exception_to_error(KeyError("mot de passe"))
    assert error == ErrorObject(-32603, "Erreur interne du serveur", {"canonical": "INTERNAL"})


@pytest.mark.parametrize(("error", "expected", "canonical"), [
    (ErrorObject(-32700, "x"), RpcProtocolError, "PROTOCOL"),
    (ErrorObject(-32600, "x"), RpcProtocolError, "PROTOCOL"),
    (ErrorObject(-32601, "x"), MethodNotFoundError, "METHOD_NOT_FOUND"),
    (ErrorObject(-32602, "x"), InvalidArgumentError, "INVALID_ARGUMENT"),
    (ErrorObject(-32603, "x"), InternalRemoteError, "INTERNAL"),
    (ErrorObject(-32000, "x"), InternalRemoteError, "INTERNAL"),
    (ErrorObject(-32001, "x"), NotFoundError, "NOT_FOUND"),
    (ErrorObject(-32002, "x"), FailedPreconditionError, "FAILED_PRECONDITION"),
    (ErrorObject(-31999, "x", "texte libre"), InternalRemoteError, "INTERNAL"),
    (ErrorObject(-32000, "x", {"canonical": "NOT_FOUND"}), NotFoundError, "NOT_FOUND"),
])
def test_error_to_exception_maps_every_code(error: ErrorObject, expected: type[RpcError], canonical: str) -> None:
    exception = protocol.error_to_exception(error, method="m")
    assert type(exception) is expected
    assert (exception.code, exception.message, exception.protocol, exception.method) == (canonical, "x", "custom", "m")
    assert exception.detail["jsonrpc_code"] == error.code


# --- Conformité avec l'implémentation de référence (paquet json-rpc) -----------------

@pytest.fixture
def reference() -> Dispatcher:
    dispatcher = Dispatcher()
    for procedure in (add, subtract, get_data, echo):
        dispatcher.add_method(procedure)
    dispatcher.add_method(total, name="sum")
    dispatcher.add_method(echo, name="notify_hello")
    return dispatcher


@pytest.mark.parametrize(("message", "result"), [
    (Request("subtract", [42, 23], 1), 19),
    (Request("subtract", {"subtrahend": 23, "minuend": 42}, "custom-000003"), 19),
    (Request("get_data", None, 9), ["hello", 5]),
    (Request("echo", [{"clé": "é", "n": 1.5, "vide": None}], 2), {"clé": "é", "n": 1.5, "vide": None}),
])
def test_reference_server_understands_our_requests(reference: Dispatcher, message: Request, result: Any) -> None:
    reply = JSONRPCResponseManager.handle(protocol.encode_request(message), reference)
    assert protocol.decode_message(reply.json.encode("utf-8")) == Response(message.id, result=result)


def test_reference_error_responses_decode_to_our_exceptions(reference: Dispatcher) -> None:
    def remote_error(payload: bytes) -> RpcError:
        reply = protocol.decode_message(JSONRPCResponseManager.handle(payload, reference).json.encode("utf-8"))
        assert isinstance(reply, Response) and reply.error is not None
        return protocol.error_to_exception(reply.error)

    assert isinstance(remote_error(protocol.encode_request(Request("nope", None, 1))), MethodNotFoundError)
    assert isinstance(remote_error(protocol.encode_request(Request("subtract", [1], 1))), InvalidArgumentError)
    assert isinstance(remote_error(b'{"jsonrpc":"2.0","method":'), RpcProtocolError)
    assert isinstance(remote_error(b'{"jsonrpc":"2.0","method":1,"id":1}'), RpcProtocolError)


def test_reference_server_handles_our_notifications_and_batches(reference: Dispatcher) -> None:
    notification = protocol.encode_request(Request("notify_hello", [7], is_notification=True))
    assert JSONRPCResponseManager.handle(notification, reference) is None

    batch = protocol.encode_batch([
        Request("sum", [1, 2, 4], "1"),
        Request("notify_hello", [7], is_notification=True),
        Request("subtract", [42, 23], "2"),
        Request("nope", None, "5"),
    ])
    replies = protocol.decode_message(JSONRPCResponseManager.handle(batch, reference).json.encode("utf-8"))
    assert isinstance(replies, list)
    assert [(reply.id, reply.result, reply.error.code if reply.error else None) for reply in replies] == [
        ("1", 7, None), ("2", 19, None), ("5", None, -32601),
    ]


SPEC_EXAMPLES = [
    '{"jsonrpc": "2.0", "method": "subtract", "params": [42, 23], "id": 1}',
    '{"jsonrpc": "2.0", "method": "subtract", "params": {"subtrahend": 23, "minuend": 42}, "id": 3}',
    '{"jsonrpc": "2.0", "method": "foobar", "id": "1"}',
    '{"jsonrpc": "2.0", "method": "foobar, "params": "bar", "baz]',
    '{"jsonrpc": "2.0", "method": 1, "params": "bar"}',
    '[]',
    '{"jsonrpc": "2.0", "method": "subtract", "params": [1, 2, 3], "id": 7}',
    '{"jsonrpc": "2.0", "method": "subtract", "params": {"minuend": 1, "other": 2}, "id": 8}',
    '[{"jsonrpc": "2.0", "method": "sum", "params": [1, 2, 4], "id": "1"},'
    ' {"jsonrpc": "2.0", "method": "notify_hello", "params": [7]},'
    ' {"jsonrpc": "2.0", "method": "subtract", "params": [42, 23], "id": "2"},'
    ' {"jsonrpc": "2.0", "method": "foo.get", "params": {"name": "myself"}, "id": "5"},'
    ' {"jsonrpc": "2.0", "method": "get_data", "id": "9"}]',
]


@pytest.mark.parametrize("payload", SPEC_EXAMPLES)
def test_skeleton_answers_like_the_reference_implementation(
    raw: RawClient, reference: Dispatcher, payload: str
) -> None:
    def outcome(reply: Any) -> Any:
        if isinstance(reply, list):
            return [outcome(item) for item in reply]
        return reply["jsonrpc"], reply["id"], reply.get("result"), reply.get("error", {}).get("code")

    raw.send(payload.encode("utf-8"))
    expected = json.loads(JSONRPCResponseManager.handle(payload, reference).json)
    assert outcome(raw.receive()) == outcome(expected)


# --- Squelette : comportement observé sur le fil ---------------------------------------

def test_unparseable_frame_gets_a_parse_error_and_the_connection_survives(raw: RawClient) -> None:
    raw.send(b'{"jsonrpc":"2.0","method":"add","params":[1,')
    reply = raw.receive()
    assert reply["id"] is None
    assert reply["error"]["code"] == -32700
    assert reply["error"]["data"] == {"canonical": "PROTOCOL"}
    assert raw.exchange(request("add", [1, 2]))["result"] == 3


@pytest.mark.parametrize("document", [
    42,
    "add",
    {"method": "add", "id": 1},
    {"jsonrpc": "2.0", "method": "add", "params": 3, "id": 1},
    {"jsonrpc": "2.0", "result": 3, "id": 1},
])
def test_invalid_request_objects_are_rejected(raw: RawClient, document: Any) -> None:
    reply = raw.exchange(document)
    assert (reply["id"], reply["error"]["code"]) == (None, -32600)


def test_positional_and_named_params_reach_the_same_procedure(raw: RawClient) -> None:
    assert raw.exchange(request("subtract", [42, 23]))["result"] == 19
    assert raw.exchange(request("subtract", {"subtrahend": 23, "minuend": 42}))["result"] == 19
    assert raw.exchange(request("add", [5]))["result"] == 5
    assert raw.exchange(request("get_data"))["result"] == ["hello", 5]
    assert raw.exchange(request("add", [1, 1], request_id=None)) == {"jsonrpc": "2.0", "result": 2, "id": None}


def test_notification_is_executed_but_never_answered(raw: RawClient, recorder: Recorder) -> None:
    raw.send_json({"jsonrpc": "2.0", "method": "record", "params": ["effet de bord"]})
    assert recorder.called.wait(2.0)
    assert recorder.values == ["effet de bord"]
    # Même en échec, une notification reste sans réponse.
    raw.send_json({"jsonrpc": "2.0", "method": "nope"})
    raw.send_json({"jsonrpc": "2.0", "method": "add", "params": ["trop", "d'", "arguments"]})
    raw.send_json({"jsonrpc": "2.0", "method": "boom"})
    raw.send_json({"jsonrpc": "2.0", "method": "countdown", "params": [3]})
    answer = raw.exchange(request("add", [1, 2], request_id="suivant"))
    assert answer == {"jsonrpc": "2.0", "result": 3, "id": "suivant"}
    raw.assert_silent()


def test_batch_mixes_results_errors_and_notifications(raw: RawClient, recorder: Recorder) -> None:
    replies = raw.exchange([
        request("add", [1, 2], "a"),
        {"jsonrpc": "2.0", "method": "record", "params": ["lot"]},
        request("nope", None, "b"),
        {"foo": "boo"},
        request("fail", ["missing"], "c"),
        request("subtract", {"minuend": 9, "subtrahend": 4}, "d"),
    ])
    summary = [(reply["id"], reply.get("result"), reply.get("error", {}).get("code")) for reply in replies]
    assert summary == [("a", 3, None), ("b", None, -32601), (None, None, -32600), ("c", None, -32001), ("d", 5, None)]
    assert recorder.values == ["lot"]


def test_batch_edge_cases_follow_the_specification(raw: RawClient, recorder: Recorder) -> None:
    empty = raw.exchange([])
    assert (empty["id"], empty["error"]["code"]) == (None, -32600)
    invalid = raw.exchange([1, 2, 3])
    assert [(reply["id"], reply["error"]["code"]) for reply in invalid] == [(None, -32600)] * 3
    assert raw.exchange([request("add", [2, 2], 5)]) == [{"jsonrpc": "2.0", "result": 4, "id": 5}]
    # Un lot composé uniquement de notifications ne reçoit rien du tout.
    raw.send_json([{"jsonrpc": "2.0", "method": "record", "params": [1]},
                   {"jsonrpc": "2.0", "method": "record", "params": [2]}])
    raw.assert_silent()
    assert recorder.values == [1, 2]


@pytest.mark.parametrize(("kind", "code", "canonical", "data"), [
    ("invalid", -32602, "INVALID_ARGUMENT", {"param": "x"}),
    ("missing", -32001, "NOT_FOUND", {"product_id": "SKU-0"}),
    ("conflict", -32002, "FAILED_PRECONDITION", {"stock": 1, "requested": 5}),
    ("generic", -32603, "INTERNAL", {}),
    ("unmapped", -32000, "UNAVAILABLE", {}),
])
def test_domain_errors_travel_with_code_message_and_data(
    raw: RawClient, kind: str, code: int, canonical: str, data: dict[str, Any]
) -> None:
    reply = raw.exchange(request("fail", [kind], request_id=kind))
    assert reply == {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": DOMAIN_FAILURES[kind].message, "data": {"canonical": canonical, **data}},
        "id": kind,
    }


@pytest.mark.parametrize("method", ["boom", "unserializable"])
def test_internal_errors_never_leak_details(raw: RawClient, method: str) -> None:
    raw.send_json(request(method))
    body = protocol.read_frame(raw.sock)
    assert body is not None
    assert json.loads(body) == {
        "jsonrpc": "2.0",
        "error": {"code": -32603, "message": "Erreur interne du serveur", "data": {"canonical": "INTERNAL"}},
        "id": 1,
    }
    for secret in (b"secret", b"4242", b"RuntimeError", b"Traceback", b"set"):
        assert secret not in body
    assert raw.exchange(request("add", [1, 2]))["result"] == 3


def test_oversize_frame_is_refused_then_the_connection_is_closed(
    raw: RawClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol, "MAX_FRAME_BYTES", 2048)
    raw.sock.sendall(struct.pack(">I", 5000))
    reply = raw.receive()
    assert (reply["id"], reply["error"]["code"]) == (None, -32600)
    assert raw.receive() is None


def test_stream_is_item_notifications_then_a_final_response(raw: RawClient) -> None:
    raw.send_json(request("countdown", [3], request_id="s1"))
    frames = [raw.receive() for _ in range(4)]
    assert frames[:3] == [
        {"jsonrpc": "2.0", "method": "rpc.stream.item", "params": {"id": "s1", "seq": seq, "item": item}}
        for seq, item in ((1, 3), (2, 2), (3, 1))
    ]
    assert frames[3] == {"jsonrpc": "2.0", "result": {"stream": "end", "count": 3}, "id": "s1"}


def test_registration_api(bus: EventBus) -> None:
    skeleton = RpcServerSkeleton(bus=bus)

    @skeleton.expose
    def ping() -> str:
        return "pong"

    @skeleton.expose(name="math.double")
    def double(x: int) -> int:
        return 2 * x

    class Gadget:
        def visible(self) -> int:
            return 1

        def _hidden(self) -> int:
            return 2

    skeleton.register_instance(Gadget())
    skeleton.register_instance(InventoryService(), names=["check_stock"])
    assert [method["name"] for method in skeleton.methods()] == ["ping", "math.double", "visible", "check_stock"]
    assert ping() == "pong" and double(4) == 8
    with pytest.raises(ValueError, match="réservé"):
        skeleton.register(ping, "rpc.ping")


def test_discover_describes_procedures(stub: RpcClientStub, server: RpcServerSkeleton) -> None:
    described = stub.discover()
    assert described == {"methods": server.methods()}
    by_name = {method["name"]: method for method in described["methods"]}
    assert "rpc.discover" not in by_name
    assert by_name["add"] == {
        "name": "add",
        "doc": "Additionne deux entiers.",
        "streaming": False,
        "params": [
            {"name": "a", "kind": "POSITIONAL_OR_KEYWORD", "default": None, "annotation": "int", "required": True},
            {"name": "b", "kind": "POSITIONAL_OR_KEYWORD", "default": 0, "annotation": "int", "required": False},
        ],
    }
    assert (by_name["sum"]["params"][0]["kind"], by_name["sum"]["params"][0]["required"]) == ("VAR_POSITIONAL", False)
    assert by_name["countdown"]["streaming"] is True


# --- Stub : appels, erreurs, lots, flux ---------------------------------------------------

def test_stub_calls_with_positional_or_named_arguments(stub: RpcClientStub) -> None:
    assert stub.call("add", 2, 3) == 5
    assert stub.call("add", a=2, b=3) == 5
    assert stub.add(2) == 2
    assert stub.subtract(subtrahend=23, minuend=42) == 19
    assert stub.echo({"clé": ["é", 1.5, None, True]}) == {"clé": ["é", 1.5, None, True]}
    with pytest.raises(TypeError, match="positionnels OU nommés"):
        stub.add(2, b=3)


def test_dynamic_proxy_does_not_swallow_private_or_dunder_attributes(stub: RpcClientStub) -> None:
    with pytest.raises(AttributeError):
        stub._undefined
    assert not hasattr(stub, "__wrapped__")
    assert getattr(stub, "__deepcopy__", None) is None
    assert stub.last_call_id == ""


def test_unknown_method_raises_method_not_found(stub: RpcClientStub) -> None:
    with pytest.raises(MethodNotFoundError) as caught:
        stub.no_such_procedure(1)
    error = caught.value
    assert isinstance(error, RpcRemoteError)
    assert (error.code, error.protocol, error.method) == ("METHOD_NOT_FOUND", "custom", "no_such_procedure")
    assert error.detail == {"jsonrpc_code": -32601}
    with pytest.raises(MethodNotFoundError):
        stub.call("rpc.unknown")


@pytest.mark.parametrize(("args", "kwargs", "reason"), [
    ((), {}, "paramètre obligatoire manquant « a »"),
    ((1, 2, 3), {}, "3 paramètres reçus, 2 acceptés au plus"),
    ((), {"a": 1, "c": 3}, "paramètre inconnu « c »"),
    ((), {"b": 1}, "paramètre obligatoire manquant « a »"),
])
def test_arguments_that_do_not_fit_the_signature_are_invalid_params(
    stub: RpcClientStub, args: tuple[Any, ...], kwargs: dict[str, Any], reason: str
) -> None:
    with pytest.raises(InvalidArgumentError) as caught:
        stub.call("add", *args, **kwargs)
    assert caught.value.message == f"Paramètres invalides pour add : {reason}"
    assert caught.value.detail == {"jsonrpc_code": -32602}
    assert not caught.value.retryable
    with pytest.raises(InvalidArgumentError, match="manquant « minuend », « subtrahend »"):
        stub.subtract()


@pytest.mark.parametrize(("kind", "expected", "detail"), [
    ("invalid", InvalidArgumentError, {"jsonrpc_code": -32602, "param": "x"}),
    ("missing", NotFoundError, {"jsonrpc_code": -32001, "product_id": "SKU-0"}),
    ("conflict", FailedPreconditionError, {"jsonrpc_code": -32002, "stock": 1, "requested": 5}),
    ("generic", InternalRemoteError, {"jsonrpc_code": -32603}),
    ("unmapped", RpcTransportError, {"jsonrpc_code": -32000}),
])
def test_remote_domain_errors_become_common_rpc_errors(
    stub: RpcClientStub, kind: str, expected: type[RpcError], detail: dict[str, Any]
) -> None:
    with pytest.raises(RpcError) as caught:
        stub.fail(kind)
    assert type(caught.value) is expected
    assert caught.value.message == DOMAIN_FAILURES[kind].message
    assert caught.value.code == DOMAIN_FAILURES[kind].code
    assert caught.value.detail == detail


def test_unexpected_remote_exception_is_an_opaque_internal_error(stub: RpcClientStub) -> None:
    with pytest.raises(InternalRemoteError) as caught:
        stub.boom()
    assert caught.value.message == "Erreur interne du serveur"
    assert "secret" not in str(caught.value.to_dict())


def test_unserializable_argument_is_reported_through_the_call(stub: RpcClientStub) -> None:
    with pytest.raises(RpcProtocolError):
        stub.echo({1, 2})
    assert stub.add(1, 1) == 2


def test_batch_returns_results_in_order_with_failures_as_values(
    stub: RpcClientStub, connections: list[tuple[str, int]]
) -> None:
    results = stub.batch([
        ("add", [1, 2]),
        ("nope", None),
        ("subtract", {"minuend": 9, "subtrahend": 4}),
        ("fail", ("missing",)),
        ("get_data", None),
    ])
    assert results[0] == 3 and results[2] == 5 and results[4] == ["hello", 5]
    assert isinstance(results[1], MethodNotFoundError) and results[1].method == "nope"
    assert isinstance(results[3], NotFoundError) and results[3].detail["product_id"] == "SKU-0"
    assert stub.batch([]) == []
    assert len(connections) == 1


def test_batch_travels_in_a_single_frame_each_way(stub: RpcClientStub, bus: EventBus) -> None:
    stub.batch([("add", [index, 1]) for index in range(5)])
    stages = [event.stage for event in bus.recent()]
    assert stages.count("client.send") == stages.count("server.receive") == 1
    assert stages.count("server.send") == stages.count("client.receive") == 1
    assert stages.count("server.execute") == stages.count("client.return") == 5
    sent = next(event for event in bus.recent() if event.stage == "client.send")
    assert len(json.loads(sent.payload[protocol.HEADER_SIZE:])) == 5


def test_stream_yields_items_as_they_arrive(stub: RpcClientStub) -> None:
    assert list(stub.stream("countdown", 4)) == [4, 3, 2, 1]
    assert list(stub.stream("countdown", n=0)) == []
    assert list(stub.stream("add", 2, 3)) == [5]
    assert stub.add(1, 1) == 2


def test_stream_delivers_items_then_raises_the_remote_error(stub: RpcClientStub) -> None:
    received: list[int] = []
    with pytest.raises(NotFoundError, match="flux interrompu"):
        for item in stub.stream("broken_stream"):
            received.append(item)
    assert received == [1, 2]
    with pytest.raises(MethodNotFoundError):
        list(stub.stream("nope"))


def test_stream_timeout_applies_between_items(stub: RpcClientStub) -> None:
    # 5 éléments espacés de 60 ms : le flux dure plus que l'échéance, mais aucun silence ne la dépasse.
    assert list(stub.stream("ticker", 5, 60, timeout=0.2)) == [0, 1, 2, 3, 4]
    received: list[int] = []
    with pytest.raises(RpcTimeoutError):
        for item in stub.stream("ticker", 3, 400, timeout=0.1):
            received.append(item)
    assert received == [0]
    assert stub.add(1, 1) == 2


def test_abandoned_stream_does_not_disturb_later_calls(stub: RpcClientStub) -> None:
    stream = stub.stream("ticker", 50, 5)
    assert next(stream) == 0
    stream.close()
    assert [stub.add(index, 1) for index in range(5)] == [1, 2, 3, 4, 5]


def test_notify_returns_without_waiting_for_the_server(stub: RpcClientStub, recorder: Recorder) -> None:
    assert stub.notify("record", "depuis le stub") is None
    assert recorder.called.wait(2.0)
    assert recorder.values == ["depuis le stub"]
    stub.notify("nope")
    assert stub.add(1, 2) == 3


# --- Stub : multiplexage, échéances, pannes ---------------------------------------------

def test_fifty_concurrent_calls_share_one_connection(stub: RpcClientStub, connections: list[tuple[str, int]]) -> None:
    started = time.perf_counter()
    for index in range(10):
        assert stub.nap(40, index) == index
    sequential_s = time.perf_counter() - started

    started = time.perf_counter()
    futures = [stub.call_async("nap", 40, tag) for tag in range(50)]
    results = [future.result(timeout=5.0) for future in futures]
    concurrent_s = time.perf_counter() - started

    assert results == list(range(50))
    assert all(isinstance(future, RpcFuture) and future.method == "nap" for future in futures)
    assert len({future.call_id for future in futures}) == 50
    assert len(connections) == 1
    # 50 appels pipelinés vont plus vite que 10 appels séquentiels.
    assert concurrent_s < sequential_s


def test_responses_are_matched_by_id_even_out_of_order(stub: RpcClientStub) -> None:
    slow = stub.call_async("nap", 250, "lent")
    fast = stub.call_async("nap", 0, "rapide")
    assert fast.result(timeout=2.0) == "rapide"
    assert not slow.done()
    assert slow.cancel() is False       # la requête est partie : rien à annuler côté client
    assert slow.result(timeout=2.0) == "lent"


def test_stub_is_safe_to_share_between_threads(stub: RpcClientStub) -> None:
    failures: list[BaseException] = []

    def worker(base: int) -> None:
        try:
            for offset in range(25):
                assert stub.add(base, offset) == base + offset
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(1000 * index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
    assert failures == []


def test_timeout_raises_then_the_stub_keeps_working(stub: RpcClientStub, connections: list[tuple[str, int]]) -> None:
    started = time.perf_counter()
    with pytest.raises(RpcTimeoutError) as caught:
        stub.call("nap", 300, "en retard", timeout=0.05)
    assert 0.05 <= time.perf_counter() - started < 0.25
    assert (caught.value.code, caught.value.retryable, caught.value.method) == ("TIMEOUT", True, "nap")
    assert isinstance(caught.value, RpcTransportError)

    # La réponse « en retard » arrivera pendant cet appel : elle ne doit pas lui être attribuée.
    assert stub.call("nap", 350, "à l'heure") == "à l'heure"
    assert stub.add(1, 2) == 3
    assert len(connections) == 1


def test_async_call_times_out_through_its_future(stub: RpcClientStub) -> None:
    future = stub.call_async("nap", 300, timeout=0.05)
    with pytest.raises(RpcTimeoutError):
        future.result(timeout=2.0)
    assert stub.add(2, 2) == 4


def test_connection_refused_is_a_transport_error(bus: EventBus) -> None:
    with RpcClientStub(HOST, free_port(), connect_timeout=0.15, bus=bus) as stub:
        with pytest.raises(RpcTransportError) as caught:
            stub.add(1, 2)
        assert caught.value.retryable and caught.value.protocol == "custom"
        future = stub.call_async("add", 1, 2)
        assert isinstance(future.exception(timeout=2.0), RpcTransportError)
        with pytest.raises(RpcTransportError):
            stub.notify("add", 1, 2)
        with pytest.raises(RpcTransportError):
            stub.connect()
        assert all(isinstance(result, RpcTransportError) for result in stub.batch([("add", [1, 2])] * 3))
        with pytest.raises(RpcTransportError):
            list(stub.stream("countdown", 3))
        assert not stub.connected


def test_pending_calls_fail_when_the_connection_is_lost(stub: RpcClientStub, server: RpcServerSkeleton) -> None:
    futures = [stub.call_async("nap", 800, index) for index in range(5)]
    assert stub.connected
    started = time.perf_counter()
    server.stop()
    for future in futures:
        error = future.exception(timeout=2.0)
        assert type(error) is RpcTransportError and error.method == "nap"
    assert time.perf_counter() - started < 0.7


def test_stub_reconnects_after_the_server_restarts_on_the_same_port(
    stub: RpcClientStub, server: RpcServerSkeleton, connections: list[tuple[str, int]]
) -> None:
    assert stub.add(1, 2) == 3
    port = server.port
    server.stop()
    wait_until(lambda: not stub.connected)      # le thread lecteur a constaté la coupure
    with pytest.raises(RpcTransportError):
        stub.add(1, 2)
    server.start()
    assert server.port == port
    assert stub.add(3, 4) == 7
    assert stub.add(5, 6) == 11
    assert connections.count((HOST, port)) == 3     # connexion initiale, tentative refusée, reconnexion


def test_close_fails_pending_calls_and_the_stub_stays_usable(stub: RpcClientStub) -> None:
    future = stub.call_async("nap", 500)
    stub.close()
    assert isinstance(future.exception(timeout=2.0), RpcTransportError)
    assert not stub.connected
    stub.close()
    assert stub.add(1, 2) == 3
    with pytest.raises(RpcTimeoutError):
        stub.call("nap", 300, timeout=0.05)


def test_garbage_from_the_server_fails_pending_calls_with_a_protocol_error(bus: EventBus) -> None:
    with socket.create_server((HOST, 0)) as listener:
        def serve() -> None:
            peer, _ = listener.accept()
            with peer:
                protocol.read_frame(peer)
                peer.sendall(protocol.frame(b"ceci n'est pas du JSON"))
                peer.recv(1)

        rogue = threading.Thread(target=serve)
        rogue.start()
        with RpcClientStub(HOST, listener.getsockname()[1], timeout=2.0, bus=bus) as stub:
            with pytest.raises(RpcProtocolError):
                stub.add(1, 2)
        rogue.join(2.0)


# --- Squelette : cycle de vie ---------------------------------------------------------------

def test_stop_is_idempotent_and_releases_the_port(bus: EventBus) -> None:
    skeleton = make_server(bus).start()
    port = skeleton.port
    assert port != 0 and skeleton.running
    client = RawClient(port)
    assert client.exchange(request("add", [1, 2]))["result"] == 3
    skeleton.stop()
    skeleton.stop()
    assert not skeleton.running
    assert client.receive() is None             # la connexion ouverte a été fermée par le serveur
    client.close()
    with socket.socket() as probe:              # liaison exclusive : plus personne ne doit écouter sur le port
        if sys.platform == "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:                                   # sous Linux, sans SO_REUSEADDR, le TIME_WAIT bloquerait la liaison
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((HOST, port))
    successor = make_server(bus, port).start()
    try:
        assert successor.port == port
        with RpcClientStub(HOST, port, bus=bus) as stub:
            assert stub.add(2, 2) == 4
    finally:
        successor.stop()


def test_start_is_idempotent_and_reports_a_busy_port(server: RpcServerSkeleton, bus: EventBus) -> None:
    port = server.port
    assert server.start() is server and server.port == port
    with pytest.raises(OSError, match="Impossible d'écouter"):
        make_server(bus, port).start()


def test_server_context_manager_starts_and_stops(bus: EventBus) -> None:
    with make_server(bus) as skeleton:
        with RpcClientStub(HOST, skeleton.port, bus=bus) as stub:
            assert stub.add(1, 1) == 2
    assert not skeleton.running


# --- Télémétrie -------------------------------------------------------------------------------

def test_unary_call_emits_the_whole_pipeline_under_one_call_id(stub: RpcClientStub, bus: EventBus) -> None:
    assert stub.call("add", a=2, b=3) == 5
    events = bus.recent()
    assert [event.stage for event in events] == list(PIPELINE)
    assert {event.call_id for event in events} == {stub.last_call_id}
    assert stub.last_call_id.startswith("custom-")
    assert {event.method for event in events} == {"add"}
    assert {event.protocol for event in events} == {"custom"}
    assert [event.side for event in events] == ["client"] * 3 + ["server"] * 6 + ["client"] * 3
    assert [event.seq for event in events] == sorted(event.seq for event in events)

    by_stage = {event.stage: event for event in events}
    request_body = '{"jsonrpc":"2.0","method":"add","params":{"a":2,"b":3},"id":"%s"}' % stub.last_call_id
    response_body = '{"jsonrpc":"2.0","result":5,"id":"%s"}' % stub.last_call_id
    assert by_stage["client.call"].detail == {"args": [], "kwargs": {"a": 2, "b": 3}}
    assert by_stage["client.marshal"].payload == request_body.encode("utf-8")
    assert by_stage["client.marshal"].detail["text"] == request_body
    assert by_stage["server.marshal"].payload == response_body.encode("utf-8")
    assert by_stage["server.marshal"].detail["text"] == response_body
    assert by_stage["client.send"].payload == by_stage["server.receive"].payload == protocol.frame(
        request_body.encode("utf-8")
    )
    assert by_stage["server.send"].payload == by_stage["client.receive"].payload == protocol.frame(
        response_body.encode("utf-8")
    )
    assert by_stage["server.dispatch"].detail == {"target": f"{__name__}.add", "bound_args": {"a": 2, "b": 3}}
    assert by_stage["client.return"].detail == {"result_preview": "5"}

    for stage in ("client.send", "server.receive", "server.send", "client.receive"):
        event = by_stage[stage]
        assert event.size == len(event.payload)
        assert event.detail["frame_header_hex"] == event.payload[:4].hex()
        assert [(s["start"], s["end"], s["kind"]) for s in event.detail["segments"]] == [
            (0, 4, "frame"), (4, event.size, "body"),
        ]
    timed = set(PIPELINE) - {"client.call", "server.receive"}
    assert all(by_stage[stage].duration_us is not None and by_stage[stage].duration_us >= 0 for stage in timed)
    assert by_stage["client.return"].duration_us >= by_stage["server.execute"].duration_us


def test_failed_call_ends_with_client_error(stub: RpcClientStub, bus: EventBus) -> None:
    with pytest.raises(NotFoundError):
        stub.fail("missing")
    stages = stages_of(bus, stub.last_call_id)
    assert stages == [
        "client.call", "client.marshal", "client.send",
        "server.receive", "server.unmarshal", "server.dispatch", "server.execute", "server.error",
        "server.marshal", "server.send",
        "client.receive", "client.unmarshal", "client.error",
    ]
    terminal = bus.recent()[-1]
    assert terminal.detail == {"code": "NOT_FOUND", "message": "produit introuvable"}

    bus.clear()
    with pytest.raises(InternalRemoteError):
        stub.boom()
    server_error = next(event for event in bus.recent() if event.stage == "server.error")
    # La cause exacte reste dans la trace du serveur, jamais dans la réponse.
    assert server_error.detail["cause"] == "RuntimeError: secret interne 4242"
    assert b"secret" not in next(event for event in bus.recent() if event.stage == "server.send").payload

    bus.clear()
    with pytest.raises(RpcTimeoutError):
        stub.call("nap", 200, timeout=0.05)
    timeout_event = next(event for event in bus.recent() if event.stage == "client.error")
    assert timeout_event.detail["code"] == "TIMEOUT"
    assert timeout_event.duration_us >= 50_000


def test_stream_emits_one_event_per_item_on_each_side(stub: RpcClientStub, bus: EventBus) -> None:
    assert list(stub.stream("countdown", 3)) == [3, 2, 1]
    stages = stages_of(bus, stub.last_call_id)
    assert stages.count("server.stream_item") == stages.count("client.stream_item") == 3
    assert stages[-1] == "client.return"
    items = [event for event in bus.recent() if event.stage == "client.stream_item"]
    assert [event.detail["seq"] for event in items] == [1, 2, 3]
    assert json.loads(items[0].payload)["params"] == {"id": stub.last_call_id, "seq": 1, "item": 3}


def test_nothing_is_emitted_while_the_bus_is_muted(
    stub: RpcClientStub, bus: EventBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(bus, "emit", lambda **fields: emitted.append(fields))
    with bus.muted():
        assert stub.add(1, 2) == 3
        assert stub.batch([("add", [1, 2]), ("nope", None)])[0] == 3
        assert list(stub.stream("countdown", 2)) == [2, 1]
        stub.notify("echo", "silence")
        with pytest.raises(MethodNotFoundError):
            stub.nope()
        with pytest.raises(RpcTimeoutError):
            stub.call("nap", 150, timeout=0.03)
        assert stub.call_async("add", 4, 4).result(timeout=2.0) == 8
    assert emitted == []
    assert stub.add(1, 2) == 3
    assert [fields["stage"] for fields in emitted] == list(PIPELINE)


def test_default_bus_is_the_global_one_and_honours_muted() -> None:
    seen: list[TraceEvent] = []
    unsubscribe = BUS.subscribe(lambda event: seen.append(event) if event.method == "subtract" else None)
    skeleton = RpcServerSkeleton()
    skeleton.register(subtract)
    try:
        with skeleton, RpcClientStub(HOST, skeleton.port) as stub:
            with BUS.muted():
                assert stub.subtract(5, 3) == 2
                assert stub.call_async("subtract", 9, 4).result(timeout=2.0) == 5
            assert seen == []
            assert stub.subtract(7, 2) == 5
            assert [event.stage for event in seen] == list(PIPELINE)
            assert {event.call_id for event in seen} == {stub.last_call_id}
    finally:
        unsubscribe()


# --- Branchement du service d'inventaire ---------------------------------------------------------

@pytest.fixture
def inventory(bus: EventBus) -> Iterator[tuple[InventoryService, CustomInventoryClient]]:
    service = InventoryService()
    skeleton = build_inventory_skeleton(service, port=0, bus=bus).start()
    client = CustomInventoryClient(HOST, skeleton.port, timeout=3.0, bus=bus)
    yield service, client
    client.close()
    skeleton.stop()


def test_inventory_skeleton_exposes_the_seven_procedures(bus: EventBus) -> None:
    skeleton = build_inventory_skeleton(InventoryService(), bus=bus)
    assert [method["name"] for method in skeleton.methods()] == [
        "calculate_factorial", "get_product_details", "update_stock", "list_products",
        "stream_analytics", "bulk_update_stock", "check_stock",
    ]
    streaming = {method["name"] for method in skeleton.methods() if method["streaming"]}
    assert streaming == {"stream_analytics"}
    assert skeleton.name == "custom" and not skeleton.running


def test_custom_client_returns_exactly_what_the_local_client_returns(
    inventory: tuple[InventoryService, CustomInventoryClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, remote = inventory
    local = LocalInventoryClient(InventoryService(), bus=EventBus())
    monkeypatch.setattr(time, "time", lambda: 1_760_000_500.25)   # horodatages métier identiques des deux côtés

    def without_timing(result: dict[str, Any]) -> dict[str, Any]:
        assert isinstance(result["compute_us"], float)
        return {key: value for key, value in result.items() if key != "compute_us"}

    for n in (0, 20, 170):
        assert without_timing(remote.calculate_factorial(n)) == without_timing(local.calculate_factorial(n))
    for product_id in ("SKU-1001", "SKU-1003", "SKU-1024"):
        assert remote.get_product_details(product_id) == local.get_product_details(product_id)
    assert remote.update_stock("SKU-1002", -4) == local.update_stock("SKU-1002", -4)
    assert remote.update_stock("SKU-1002", 7, idempotency_key="k1") == local.update_stock(
        "SKU-1002", 7, idempotency_key="k1"
    )
    assert remote.update_stock("SKU-1002", 7, idempotency_key="k1") == local.update_stock(
        "SKU-1002", 7, idempotency_key="k1"
    )
    assert remote.list_products() == local.list_products()
    assert remote.list_products(3, "Stockage") == local.list_products(3, "Stockage")
    assert remote.list_products(limit=60) == local.list_products(limit=60)
    assert list(remote.stream_analytics(4, 0)) == list(local.stream_analytics(4, 0))
    assert remote.invoke("get_product_details", {"product_id": "SKU-1002"}) == local.invoke(
        "get_product_details", {"product_id": "SKU-1002"}
    )


def test_custom_client_raises_the_same_errors_as_the_local_client(
    inventory: tuple[InventoryService, CustomInventoryClient]
) -> None:
    _, remote = inventory
    local = LocalInventoryClient(InventoryService(), bus=EventBus())
    scenarios = [
        ("get_product_details", {"product_id": "SKU-0000"}, NotFoundError),
        ("update_stock", {"product_id": "SKU-1001", "delta": -10_000}, FailedPreconditionError),
        ("calculate_factorial", {"n": -1}, InvalidArgumentError),
        ("list_products", {"limit": "beaucoup"}, InvalidArgumentError),
    ]
    for method, params, expected in scenarios:
        with pytest.raises(expected) as remote_error:
            remote.invoke(method, params)
        with pytest.raises(expected) as local_error:
            local.invoke(method, params)
        assert remote_error.value.code == local_error.value.code
        assert remote_error.value.message == local_error.value.message
        assert remote_error.value.protocol == "custom"
    with pytest.raises(InvalidArgumentError):
        next(remote.stream_analytics(samples=0))
    with pytest.raises(MethodNotFoundError):
        remote.invoke("no_such_method")


def test_custom_client_sends_named_params_and_tracks_the_call_id(
    inventory: tuple[InventoryService, CustomInventoryClient], bus: EventBus
) -> None:
    _, client = inventory
    assert isinstance(client.stub, RpcClientStub) and client.protocol == "custom"
    client.update_stock("SKU-1001", -3)
    marshal = next(event for event in bus.recent() if event.stage == "client.marshal")
    assert json.loads(marshal.payload) == {
        "jsonrpc": "2.0",
        "method": "update_stock",
        "params": {"product_id": "SKU-1001", "delta": -3, "idempotency_key": ""},
        "id": client.last_call_id,
    }
    assert client.last_call_id.startswith("custom-")
    assert stages_of(bus, client.last_call_id) == list(PIPELINE)

    first = client.last_call_id
    assert len(list(client.stream_analytics(2, 0))) == 2
    assert client.last_call_id != first
    assert "client.stream_item" in stages_of(bus, client.last_call_id)


def test_submit_multiplexes_calls_on_the_shared_connection(
    inventory: tuple[InventoryService, CustomInventoryClient], connections: list[tuple[str, int]]
) -> None:
    _, client = inventory
    futures = [client.submit("calculate_factorial", {"n": n}) for n in range(30)]
    assert client.last_call_id == futures[-1].call_id
    assert [future.result(timeout=3.0)["n"] for future in futures] == list(range(30))
    assert all(isinstance(future, RpcFuture) for future in futures)
    assert len(connections) == 1

    assert isinstance(client.submit("update_stock", {"product_id": "SKU-0"}).exception(3.0), InvalidArgumentError)
    assert isinstance(client.submit("get_product_details", {"product_id": "SKU-0"}).exception(3.0), NotFoundError)
    assert isinstance(client.submit("no_such_method").exception(3.0), MethodNotFoundError)
    assert len(list(client.submit("stream_analytics", {"samples": 2, "interval_ms": 0}).result(3.0))) == 2


def test_custom_client_reports_an_unreachable_server(bus: EventBus) -> None:
    with CustomInventoryClient(HOST, free_port(), timeout=0.5, bus=bus) as client:
        client.stub.connect_timeout = 0.15
        with pytest.raises(RpcTransportError):
            client.calculate_factorial(5)
        assert isinstance(client.submit("calculate_factorial", {"n": 5}).exception(2.0), RpcTransportError)


def test_contract_v2_breaks_v1_clients_only_at_run_time(bus: EventBus) -> None:
    service = InventoryService()
    skeleton = build_inventory_skeleton_v2(service, port=0, bus=bus).start()
    try:
        with CustomInventoryClient(HOST, skeleton.port, timeout=3.0, bus=bus) as v1:
            # Évolution compatible : une clé de plus, ignorée par l'ancien client.
            factorial = v1.calculate_factorial(5)
            assert factorial["result"] == "120" and factorial["algorithm"] == "binary_split"
            assert set(factorial) - {"algorithm"} == set(InventoryService().calculate_factorial(5))

            # Procédure renommée : -32601.
            with pytest.raises(MethodNotFoundError) as renamed:
                v1.get_product_details("SKU-1001")
            assert renamed.value.detail == {"jsonrpc_code": -32601}

            # Paramètre obligatoire ajouté : -32602, et rien n'a été modifié.
            with pytest.raises(InvalidArgumentError, match="warehouse") as added:
                v1.update_stock("SKU-1001", -1)
            assert added.value.detail == {"jsonrpc_code": -32602}
            assert service.get_product_details("SKU-1001")["stock"] == 84

            # Clés de résultat renommées : l'appel réussit, c'est le code client qui casse.
            product = v1.list_products(limit=1)["products"][0]
            with pytest.raises(KeyError):
                product["stock"]
            assert "price" not in product
            assert (product["price_cents"], product["quantity"]) == (12990, 84)

            # Un client à jour, lui, se sert du contrat v2 sans difficulté.
            updated = v1.stub.update_stock(product_id="SKU-1001", delta=-1, warehouse="PAR-01")
            assert (updated["new_stock"], updated["warehouse"]) == (83, "PAR-01")
            assert v1.stub.get_product("SKU-1001")["quantity"] == 83
            with pytest.raises(InvalidArgumentError):
                v1.stub.update_stock("SKU-1001", -1, "")
            assert len(list(v1.stream_analytics(2, 0))) == 2
        names = [method["name"] for method in skeleton.methods()]
        assert "get_product" in names and "get_product_details" not in names
    finally:
        skeleton.stop()
