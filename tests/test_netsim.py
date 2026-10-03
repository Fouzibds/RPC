"""Tests du réseau simulé : conditions, proxy de chaos et résilience côté client.

Le proxy est éprouvé contre un minuscule serveur d'écho TCP, la résilience contre un client
scénarisé : aucun middleware du laboratoire n'est nécessaire.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import random
import socket
import threading
import time
from collections import deque
from typing import Any, Callable, Iterator

import pytest

from common.client_api import InventoryClient
from common.config import HOST
from common.errors import (
    CircuitOpenError,
    MethodNotFoundError,
    NotFoundError,
    RpcTimeoutError,
    RpcTransportError,
)
from common.telemetry import EventBus, TraceEvent, new_call_id
from netsim import (
    PRESET_INFO,
    PRESETS,
    ChaosProxy,
    CircuitBreaker,
    NetworkConditions,
    ResilientClient,
    RetryPolicy,
    presets_dict,
)
from netsim import chaos_proxy as chaos_proxy_module

IDEAL = {
    "latency_ms": 0.0,
    "jitter_ms": 0.0,
    "spike_probability": 0.0,
    "spike_ms": 0.0,
    "reset_probability": 0.0,
    "blackhole": False,
    "down": False,
    "bandwidth_kbps": 0.0,
}
STATS_KEYS = [
    "connections_total",
    "connections_active",
    "bytes_up",
    "bytes_down",
    "chunks_up",
    "chunks_down",
    "resets",
    "refused",
    "blackholed",
    "lost_replies",
]


# --- Outils ------------------------------------------------------------------

class EchoServer:
    """Serveur d'écho TCP : renvoie chaque octet reçu et garde une copie de ce qu'il a lu."""

    def __init__(self) -> None:
        self._listener = socket.create_server((HOST, 0))
        self.port: int = self._listener.getsockname()[1]
        self.received = bytearray()
        self.connections = 0
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
                self._sockets.append(conn)
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn: socket.socket) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while data := conn.recv(65536):
                with self._lock:
                    self.received += data
                conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self._listener.close()
        with self._lock:
            sockets = list(self._sockets)
        for conn in sockets:
            conn.close()


class Recorder:
    """Bus privé : chaque test n'observe que ses propres évènements."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.events: list[TraceEvent] = []
        self.bus.subscribe(self.events.append)

    def of(self, stage: str) -> list[TraceEvent]:
        return [event for event in self.events if event.stage == stage]

    def stages(self) -> list[str]:
        return [event.stage for event in self.events]


class FakeClock:
    """Horloge manuelle : les délais du disjoncteur s'écoulent sans attendre."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeClient(InventoryClient):
    """Client scénarisé : rejoue une liste d'issues (résultat ou exception) et note chaque appel."""

    protocol = "fake"

    def __init__(self, *script: Any) -> None:
        self.script = deque(script)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def _play(self, method: str, **kwargs: Any) -> Any:
        self.last_call_id = new_call_id("fake")
        self.calls.append((method, kwargs))
        outcome = self.script.popleft() if self.script else {"ok": True}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def calculate_factorial(self, n: int, *, timeout: float | None = None) -> dict[str, Any]:
        return self._play("calculate_factorial", n=n, timeout=timeout)

    def get_product_details(self, product_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        return self._play("get_product_details", product_id=product_id, timeout=timeout)

    def update_stock(
        self, product_id: str, delta: int, *, idempotency_key: str = "", timeout: float | None = None
    ) -> dict[str, Any]:
        return self._play(
            "update_stock", product_id=product_id, delta=delta, idempotency_key=idempotency_key, timeout=timeout
        )

    def list_products(self, limit: int = 20, category: str = "", *, timeout: float | None = None) -> dict[str, Any]:
        return self._play("list_products", limit=limit, category=category, timeout=timeout)

    def stream_analytics(
        self, samples: int = 10, interval_ms: int = 200, *, timeout: float | None = None
    ) -> Iterator[dict[str, Any]]:
        return self._items(self._play("stream_analytics", samples=samples, interval_ms=interval_ms, timeout=timeout))

    @staticmethod
    def _items(items: list[Any]) -> Iterator[dict[str, Any]]:
        for item in items:
            if isinstance(item, Exception):
                raise item
            yield item

    def close(self) -> None:
        self.closed = True
        super().close()


class FakeGrpcClient(FakeClient):
    """Variante dotée des deux procédures propres à gRPC."""

    def bulk_update_stock(self, updates: list[dict[str, Any]], *, timeout: float | None = None) -> dict[str, Any]:
        return self._play("bulk_update_stock", updates=updates, timeout=timeout)

    def check_stock(self, product_ids: list[str], *, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        return self._items(self._play("check_stock", product_ids=product_ids, timeout=timeout))


def connect(proxy: ChaosProxy, timeout: float = 3.0) -> socket.socket:
    sock = socket.create_connection((HOST, proxy.port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def recv_exact(sock: socket.socket, size: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            break
        buffer += chunk
    return bytes(buffer)


def recv_until_eof(sock: socket.socket) -> bytes:
    buffer = bytearray()
    while chunk := sock.recv(65536):
        buffer += chunk
    return bytes(buffer)


def round_trip(sock: socket.socket, payload: bytes = b"ping") -> float:
    """Envoie ``payload``, attend son écho et renvoie la durée de l'aller-retour (ms)."""
    started = time.perf_counter()
    sock.sendall(payload)
    assert recv_exact(sock, len(payload)) == payload
    return (time.perf_counter() - started) * 1000


def expect_reset(sock: socket.socket, payload: bytes = b"") -> None:
    """La connexion doit être coupée net (RST) : une erreur, pas une fin de flux polie."""
    with pytest.raises(ConnectionError):
        if payload:
            sock.sendall(payload)
        sock.recv(4096)


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.perf_counter() + timeout
    while not predicate():
        assert time.perf_counter() < deadline, "condition non atteinte dans le délai imparti"
        time.sleep(0.005)


def h2_frame(kind: int, payload: bytes = b"", stream: int = 0) -> bytes:
    """Trame HTTP/2 : longueur (3 octets), type, drapeaux, identifiant de flux, contenu."""
    return len(payload).to_bytes(3, "big") + bytes([kind, 0]) + stream.to_bytes(4, "big") + payload


H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
H2_DATA, H2_HEADERS, H2_SETTINGS, H2_PING = 0x0, 0x1, 0x4, 0x6


@pytest.fixture
def echo() -> Iterator[EchoServer]:
    server = EchoServer()
    yield server
    server.close()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def make_proxy(echo: EchoServer, recorder: Recorder) -> Iterator[Callable[..., ChaosProxy]]:
    proxies: list[ChaosProxy] = []

    def factory(**options: Any) -> ChaosProxy:
        options.setdefault("bus", recorder.bus)
        target_port = options.pop("target_port", echo.port)
        proxy = ChaosProxy("test", 0, target_port, **options).start()
        proxies.append(proxy)
        return proxy

    yield factory
    for proxy in proxies:
        proxy.stop()


@pytest.fixture
def proxy(make_proxy: Callable[..., ChaosProxy]) -> ChaosProxy:
    return make_proxy()


@pytest.fixture
def resilient(recorder: Recorder) -> Callable[..., tuple[ResilientClient, list[float]]]:
    """Fabrique un client résilient dont les attentes sont consignées au lieu d'être dormies."""

    def factory(inner: InventoryClient, **options: Any) -> tuple[ResilientClient, list[float]]:
        sleeps: list[float] = []
        options.setdefault("rng", random.Random(0))
        return ResilientClient(inner, bus=recorder.bus, sleep=sleeps.append, **options), sleeps

    return factory


def timeout_error() -> RpcTimeoutError:
    return RpcTimeoutError("délai dépassé")


def transport_error() -> RpcTransportError:
    return RpcTransportError("connexion coupée")


# --- Conditions réseau -------------------------------------------------------

def test_conditions_start_ideal() -> None:
    conditions = NetworkConditions()
    assert conditions.snapshot() == {**IDEAL, "preset": "ideal"}
    assert list(conditions.snapshot()) == [*IDEAL, "preset"]


def test_update_changes_fields_and_marks_custom() -> None:
    conditions = NetworkConditions()
    result = conditions.update(latency_ms=120, blackhole=True)
    assert conditions.latency_ms == 120.0
    assert conditions.blackhole is True
    assert conditions.preset == "custom"
    assert result == conditions.snapshot() == {**IDEAL, "latency_ms": 120.0, "blackhole": True, "preset": "custom"}


@pytest.mark.parametrize(
    "fields",
    [
        {"latency_ms": -1},
        {"jitter_ms": float("nan")},
        {"bandwidth_kbps": float("inf")},
        {"spike_probability": 1.5},
        {"reset_probability": -0.1},
        {"spike_ms": "50"},
        {"latency_ms": True},
        {"blackhole": 1},
        {"down": "oui"},
        {"vitesse": 3},
        {"preset": "hyperespace"},
    ],
)
def test_update_rejects_invalid_fields(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        NetworkConditions().update(**fields)


def test_update_is_all_or_nothing() -> None:
    conditions = NetworkConditions()
    conditions.apply_preset("wan")
    before = conditions.snapshot()
    with pytest.raises(ValueError):
        conditions.update(latency_ms=10, reset_probability=2)
    assert conditions.snapshot() == before


def test_probability_bounds_are_inclusive() -> None:
    conditions = NetworkConditions()
    conditions.update(spike_probability=0, reset_probability=1)
    assert (conditions.spike_probability, conditions.reset_probability) == (0.0, 1.0)


@pytest.mark.parametrize("name", list(PRESETS))
def test_apply_preset_replaces_every_field(name: str) -> None:
    conditions = NetworkConditions()
    conditions.update(
        latency_ms=999, jitter_ms=5, spike_probability=0.5, spike_ms=10,
        reset_probability=0.5, blackhole=True, down=True, bandwidth_kbps=64,
    )
    assert conditions.apply_preset(name) == {**PRESETS[name], "preset": name}
    assert conditions.snapshot() == {**PRESETS[name], "preset": name}


def test_unknown_preset_is_rejected() -> None:
    conditions = NetworkConditions()
    with pytest.raises(ValueError):
        conditions.apply_preset("hyperespace")
    assert conditions.preset == "ideal"


def test_presets_match_the_specification() -> None:
    expected = {
        "ideal": {},
        "lan": {"latency_ms": 1.0, "jitter_ms": 0.4},
        "wan": {"latency_ms": 40.0, "jitter_ms": 8.0},
        "mobile_3g": {
            "latency_ms": 200.0, "jitter_ms": 60.0,
            "spike_probability": 0.05, "spike_ms": 800.0, "reset_probability": 0.01,
        },
        "satellite": {"latency_ms": 600.0, "jitter_ms": 40.0},
        "flaky": {
            "latency_ms": 80.0, "jitter_ms": 30.0,
            "spike_probability": 0.15, "spike_ms": 1500.0, "reset_probability": 0.08,
        },
        "blackhole": {"blackhole": True},
        "outage": {"down": True},
    }
    assert list(PRESETS) == list(expected)
    for name, overrides in expected.items():
        assert PRESETS[name] == {**IDEAL, **overrides}, name


def test_preset_info_is_complete_and_serialisable() -> None:
    assert list(PRESET_INFO) == list(PRESETS)
    for info in PRESET_INFO.values():
        assert set(info) == {"label", "description", "icon"}
        assert all(info.values())
        assert info["description"].endswith(".")
    listing = presets_dict()
    assert [entry["id"] for entry in listing] == list(PRESETS)
    assert json.loads(json.dumps(listing)) == listing
    for entry in listing:
        assert entry["label"] == PRESET_INFO[entry["id"]]["label"]
        assert entry["conditions"] == PRESETS[entry["id"]]
    listing[0]["conditions"]["latency_ms"] = 123  # la copie rendue ne doit pas exposer la table interne
    assert PRESETS["ideal"]["latency_ms"] == 0.0


def test_reset_returns_to_ideal() -> None:
    conditions = NetworkConditions()
    conditions.apply_preset("flaky")
    assert conditions.reset() == {**IDEAL, "preset": "ideal"}


@pytest.mark.parametrize("prepare", [
    lambda conditions: conditions.apply_preset("flaky"),
    lambda conditions: conditions.update(latency_ms=75, bandwidth_kbps=512),
])
def test_snapshot_can_be_restored_with_update(prepare: Callable[[NetworkConditions], Any]) -> None:
    conditions = NetworkConditions()
    prepare(conditions)
    saved = conditions.snapshot()
    conditions.apply_preset("outage")
    conditions.update(**saved)
    assert conditions.snapshot() == saved


def test_update_with_preset_uses_it_as_a_base() -> None:
    conditions = NetworkConditions()
    assert conditions.update(preset="wan")["preset"] == "wan"
    assert conditions.update(preset="wan", latency_ms=55) == {**PRESETS["wan"], "latency_ms": 55.0, "preset": "custom"}


def test_constructor_validates_and_labels() -> None:
    assert NetworkConditions(latency_ms=50).preset == "custom"
    with pytest.raises(ValueError):
        NetworkConditions(latency_ms=-5)
    conditions = NetworkConditions()
    assert dataclasses.asdict(conditions) == conditions.snapshot()


def test_conditions_can_be_copied_despite_their_lock() -> None:
    conditions = NetworkConditions()
    conditions.apply_preset("mobile_3g")
    clone = copy.deepcopy(conditions)
    assert clone == conditions and clone.snapshot() == conditions.snapshot()
    clone.update(latency_ms=1)
    assert conditions.latency_ms == 200.0  # la copie est indépendante


def test_conditions_stay_consistent_under_concurrency() -> None:
    conditions = NetworkConditions()
    conditions.apply_preset("flaky")
    allowed = [{**PRESETS[name], "preset": name} for name in ("flaky", "satellite")]
    torn: list[dict[str, Any]] = []
    stop = threading.Event()

    def write() -> None:
        while not stop.is_set():
            conditions.apply_preset("satellite")
            conditions.apply_preset("flaky")

    def read() -> None:
        while not stop.is_set():
            snapshot = conditions.snapshot()
            if snapshot not in allowed:
                torn.append(snapshot)

    threads = [threading.Thread(target=target) for target in (write, write, read, read, read)]
    for thread in threads:
        thread.start()
    time.sleep(0.15)
    stop.set()
    for thread in threads:
        thread.join(2.0)
    assert torn == []


# --- Proxy de chaos : relais -------------------------------------------------

def test_relay_is_transparent(proxy: ChaosProxy, echo: EchoServer) -> None:
    payloads = (b"bonjour", bytes(range(256)), b"\x00" * 1000)
    with connect(proxy) as sock:
        for payload in payloads:
            round_trip(sock, payload)
    assert bytes(echo.received) == b"".join(payloads)


@pytest.mark.parametrize("latency_ms", [0, 30])
def test_large_payload_arrives_intact(proxy: ChaosProxy, latency_ms: int) -> None:
    proxy.conditions.update(latency_ms=latency_ms)
    payload = random.Random(latency_ms).randbytes(1024 * 1024)
    with connect(proxy, timeout=10.0) as sock:
        sender = threading.Thread(target=sock.sendall, args=(payload,))
        sender.start()
        received = recv_exact(sock, len(payload))
        sender.join(5.0)
    assert received == payload
    assert proxy.stats()["bytes_up"] == proxy.stats()["bytes_down"] == len(payload)


def test_backpressure_bounds_the_delay_line_without_losing_bytes(
    proxy: ChaosProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chaos_proxy_module, "_HIGH_WATER_BYTES", 64 * 1024)
    proxy.conditions.update(latency_ms=20)
    payload = random.Random(1).randbytes(512 * 1024)
    with connect(proxy, timeout=10.0) as sock:
        sender = threading.Thread(target=sock.sendall, args=(payload,))
        sender.start()
        received = recv_exact(sock, len(payload))
        sender.join(5.0)
    assert received == payload


def test_port_is_assigned_and_start_is_idempotent(proxy: ChaosProxy) -> None:
    port = proxy.port
    assert port != 0 and proxy.running
    assert proxy.start() is proxy
    assert proxy.port == port


def test_context_manager_starts_and_stops(echo: EchoServer) -> None:
    with ChaosProxy("test", 0, echo.port) as proxy:
        with connect(proxy) as sock:
            round_trip(sock)
    assert not proxy.running


def test_listening_on_a_busy_port_fails_clearly(proxy: ChaosProxy, echo: EchoServer) -> None:
    with pytest.raises(OSError, match="impossible d'écouter"):
        ChaosProxy("doublon", proxy.port, echo.port).start()


def test_concurrent_connections_do_not_interfere(proxy: ChaosProxy) -> None:
    proxy.conditions.update(latency_ms=20)
    failures: list[Exception] = []

    def client(index: int) -> None:
        try:
            with connect(proxy) as sock:
                for turn in range(5):
                    round_trip(sock, b"client-%d-tour-%d" % (index, turn))
        except Exception as exc:  # noqa: BLE001 - remonté au fil principal par la liste
            failures.append(exc)

    threads = [threading.Thread(target=client, args=(index,)) for index in range(8)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert failures == []
    assert elapsed_ms < 600  # huit clients en parallèle : ~100 ms, contre 800 ms s'ils se gênaient
    stats = proxy.stats()
    assert stats["connections_total"] == 8
    assert stats["bytes_up"] == stats["bytes_down"] == 8 * 5 * len(b"client-0-tour-0")


def test_half_close_propagates_after_the_delay_line_drains(proxy: ChaosProxy) -> None:
    proxy.conditions.update(latency_ms=60)
    with connect(proxy) as sock:
        sock.sendall(b"derniers mots")
        sock.shutdown(socket.SHUT_WR)
        assert recv_until_eof(sock) == b"derniers mots"
    wait_until(lambda: proxy.stats()["connections_active"] == 0)
    assert proxy.stats()["resets"] == 0


# --- Proxy de chaos : latence ------------------------------------------------

def test_latency_is_added_to_the_round_trip(proxy: ChaosProxy) -> None:
    with connect(proxy) as sock:
        round_trip(sock)
        proxy.conditions.update(latency_ms=100)
        elapsed_ms = round_trip(sock)
    assert 90 <= elapsed_ms < 400


def test_pipelined_chunks_do_not_accumulate_delay(proxy: ChaosProxy) -> None:
    proxy.conditions.update(latency_ms=200)
    with connect(proxy) as sock:
        started = time.perf_counter()
        for index in range(5):
            sock.sendall(b"req-%d;" % index)
            time.sleep(0.01)
        assert recv_exact(sock, 30) == b"req-0;req-1;req-2;req-3;req-4;"
        elapsed_ms = (time.perf_counter() - started) * 1000
    # Une file d'attente « à verrou » aurait mis 5 × 200 ms.
    assert 190 <= elapsed_ms < 600


def test_conditions_change_takes_effect_on_a_live_connection(proxy: ChaosProxy) -> None:
    with connect(proxy) as sock:
        assert round_trip(sock) < 80
        proxy.conditions.update(latency_ms=120)
        assert round_trip(sock) >= 110
        proxy.conditions.reset()
        assert round_trip(sock) < 80


def test_conditions_are_shared_between_proxies(make_proxy: Callable[..., ChaosProxy]) -> None:
    conditions = NetworkConditions()
    first, second = make_proxy(conditions=conditions), make_proxy(conditions=conditions)
    assert first.conditions is second.conditions
    conditions.update(latency_ms=80)
    for proxy in (first, second):
        with connect(proxy) as sock:
            assert round_trip(sock) >= 70


def test_jitter_never_makes_the_delay_negative(make_proxy: Callable[..., ChaosProxy], recorder: Recorder) -> None:
    proxy = make_proxy(seed=3, conditions=NetworkConditions(latency_ms=10, jitter_ms=60))
    with connect(proxy) as sock:
        for _ in range(10):
            round_trip(sock)
    delays = [event.detail["delay_ms"] for event in recorder.of("network.delay")]
    assert delays and all(1.0 <= delay <= 35.0 for delay in delays)
    assert len(set(delays)) > 1
    assert len(delays) < 20  # certains tirages, ramenés à 0, n'ont ajouté aucun délai


def test_spike_applies_to_requests_only(proxy: ChaosProxy, recorder: Recorder) -> None:
    proxy.conditions.update(spike_probability=1, spike_ms=150)
    with connect(proxy) as sock:
        elapsed_ms = round_trip(sock)
    assert 140 <= elapsed_ms < 600
    delays = recorder.of("network.delay")
    assert [(event.detail["direction"], event.detail["spike"]) for event in delays] == [("up", True)]


def test_bandwidth_limit_throttles_the_flow(proxy: ChaosProxy) -> None:
    proxy.conditions.update(bandwidth_kbps=800)  # 100 ko/s : 20 ko prennent environ 0,2 s
    payload = bytes(range(250)) * 80
    with connect(proxy) as sock:
        elapsed_ms = round_trip(sock, payload)
    assert 180 <= elapsed_ms < 1500


# --- Proxy de chaos : pannes -------------------------------------------------

def test_reset_probability_one_cuts_every_request(proxy: ChaosProxy, echo: EchoServer, recorder: Recorder) -> None:
    proxy.conditions.update(reset_probability=1)
    with connect(proxy) as sock:
        expect_reset(sock, b"condamnee")
        # Les compteurs sont à jour dès que le client constate la coupure, sans attendre.
        stats = proxy.stats()
        assert (stats["resets"], stats["connections_active"], stats["bytes_up"]) == (1, 0, 0)
    assert bytes(echo.received) == b""
    assert [event.detail["reason"] for event in recorder.of("network.reset")] == ["probability"]


def test_armed_reset_never_reaches_the_server(proxy: ChaosProxy, echo: EchoServer, recorder: Recorder) -> None:
    proxy.arm("reset")
    with connect(proxy) as sock:
        expect_reset(sock, b"jamais remise")
    assert proxy.armed() == {"reset": 0, "lost_reply": 0}
    with connect(proxy) as sock:
        round_trip(sock, b"second essai")
    assert bytes(echo.received) == b"second essai"
    assert proxy.stats()["resets"] == 1
    (event,) = recorder.of("network.reset")
    assert event.detail["reason"] == "armed" and event.size == len(b"jamais remise")


def test_armed_reset_count_covers_several_requests(proxy: ChaosProxy, echo: EchoServer) -> None:
    proxy.arm("reset", 2)
    for _ in range(2):
        with connect(proxy) as sock:
            expect_reset(sock, b"x")
    with connect(proxy) as sock:
        round_trip(sock, b"ok")
    assert proxy.stats()["resets"] == 2
    assert bytes(echo.received) == b"ok"


def test_armed_lost_reply_reaches_the_server_but_not_the_client(
    make_proxy: Callable[..., ChaosProxy], echo: EchoServer, recorder: Recorder
) -> None:
    proxy = make_proxy(hold_ms=100)
    proxy.arm("lost_reply")
    with connect(proxy) as sock:
        started = time.perf_counter()
        expect_reset(sock, b"debit")
        elapsed_ms = (time.perf_counter() - started) * 1000
    assert 90 <= elapsed_ms < 1500
    assert bytes(echo.received) == b"debit"  # le serveur a bien reçu — et donc exécuté — la requête
    stats = proxy.stats()
    assert (stats["lost_replies"], stats["bytes_up"], stats["bytes_down"], stats["resets"]) == (1, 5, 0, 0)
    (event,) = recorder.of("network.lost_reply")
    assert event.detail["hold_ms"] == 100 and event.detail["bytes"] == 5
    with connect(proxy) as sock:
        round_trip(sock, b"suivant")


def test_lost_reply_waits_for_the_request_to_be_delivered(
    make_proxy: Callable[..., ChaosProxy], echo: EchoServer
) -> None:
    proxy = make_proxy(hold_ms=50, conditions=NetworkConditions(latency_ms=200))
    proxy.arm("lost_reply")
    with connect(proxy) as sock:
        started = time.perf_counter()
        expect_reset(sock, b"lente")
        elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms >= 140  # 100 ms d'aller, puis 50 ms de sursis
    assert bytes(echo.received) == b"lente"


def test_arm_validates_and_can_disarm(proxy: ChaosProxy) -> None:
    with pytest.raises(ValueError):
        proxy.arm("explosion")
    with pytest.raises(ValueError):
        proxy.arm("reset", -1)
    proxy.arm("reset", 3)
    proxy.arm("lost_reply")
    assert proxy.armed() == {"reset": 3, "lost_reply": 1}
    proxy.arm("reset", 0)
    assert proxy.armed() == {"reset": 0, "lost_reply": 1}
    proxy.disarm()
    assert proxy.armed() == {"reset": 0, "lost_reply": 0}


def test_blackhole_swallows_bytes_until_the_client_times_out(
    proxy: ChaosProxy, echo: EchoServer, recorder: Recorder
) -> None:
    proxy.conditions.apply_preset("blackhole")
    with connect(proxy, timeout=0.3) as sock:
        sock.sendall(b"il y a quelqu'un ?")
        with pytest.raises(TimeoutError):
            sock.recv(100)
    assert bytes(echo.received) == b""
    stats = proxy.stats()
    assert (stats["blackholed"], stats["bytes_up"], stats["resets"]) == (1, 0, 0)
    (event,) = recorder.of("network.blackhole")
    assert event.detail["direction"] == "up" and event.size == 18


def test_leaving_the_blackhole_resets_the_gapped_connection(proxy: ChaosProxy, echo: EchoServer) -> None:
    proxy.conditions.update(blackhole=True)
    with connect(proxy) as sock:
        sock.sendall(b"perdu")
        wait_until(lambda: proxy.stats()["blackholed"] == 1)
        proxy.conditions.update(blackhole=False)
        expect_reset(sock)
    with connect(proxy) as sock:
        round_trip(sock, b"reparti")
    assert bytes(echo.received) == b"reparti"


def test_down_fails_fast_then_recovers(proxy: ChaosProxy, echo: EchoServer, recorder: Recorder) -> None:
    proxy.conditions.apply_preset("outage")
    started = time.perf_counter()
    with pytest.raises(ConnectionError):
        with socket.create_connection((HOST, proxy.port), timeout=3.0) as sock:
            sock.sendall(b"allo")
            sock.recv(100)
    assert time.perf_counter() - started < 1.0
    stats = proxy.stats()
    assert (stats["refused"], stats["connections_total"], stats["connections_active"]) == (1, 1, 0)
    assert echo.connections == 0
    assert [event.detail["reason"] for event in recorder.of("network.refuse")] == ["down"]

    proxy.conditions.update(down=False)
    with connect(proxy) as sock:
        round_trip(sock, b"de retour")
    assert bytes(echo.received) == b"de retour"


def test_down_kills_live_connections(proxy: ChaosProxy) -> None:
    with connect(proxy) as sock:
        round_trip(sock)
        proxy.conditions.update(down=True)
        started = time.perf_counter()
        expect_reset(sock)
        assert time.perf_counter() - started < 1.0
        stats = proxy.stats()
        assert (stats["resets"], stats["connections_active"]) == (1, 0)


def test_unreachable_server_is_counted_as_refused(
    make_proxy: Callable[..., ChaosProxy], recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chaos_proxy_module, "CONNECT_TIMEOUT_S", 0.2)
    with socket.socket() as placeholder:
        placeholder.bind((HOST, 0))  # port réservé mais jamais mis en écoute : personne n'y répond
        proxy = make_proxy(target_port=placeholder.getsockname()[1])
        with connect(proxy) as sock:
            started = time.perf_counter()
            expect_reset(sock)
            assert time.perf_counter() - started < 1.5  # le délai de connexion borne l'attente du client
            stats = proxy.stats()
            assert (stats["refused"], stats["connections_active"]) == (1, 0)
    assert [event.detail["reason"] for event in recorder.of("network.refuse")] == ["upstream_unreachable"]


def test_seed_makes_random_faults_reproducible(make_proxy: Callable[..., ChaosProxy]) -> None:
    def outcomes() -> list[bool]:
        proxy = make_proxy(seed=42, conditions=NetworkConditions(reset_probability=0.5))
        delivered = []
        for _ in range(12):
            with connect(proxy) as sock:
                try:
                    sock.sendall(b"x")
                    delivered.append(sock.recv(10) == b"x")
                except ConnectionError:
                    delivered.append(False)
        return delivered

    first = outcomes()
    assert first == outcomes()
    assert True in first and False in first


def test_http2_service_frames_do_not_consume_an_armed_fault(proxy: ChaosProxy, echo: EchoServer) -> None:
    opening = H2_PREFACE + h2_frame(H2_SETTINGS)
    ping = h2_frame(H2_PING, b"12345678")
    request = h2_frame(H2_HEADERS, b"\x82\x86", stream=1) + h2_frame(H2_DATA, b"\x00" * 5, stream=1)
    proxy.arm("reset")
    with connect(proxy) as sock:
        round_trip(sock, opening)
        round_trip(sock, ping)
        assert proxy.armed()["reset"] == 1
        expect_reset(sock, request)
    assert proxy.armed()["reset"] == 0
    assert bytes(echo.received) == opening + ping


def test_http2_frame_header_split_across_chunks(proxy: ChaosProxy, echo: EchoServer) -> None:
    request = h2_frame(H2_HEADERS, b"\x82", stream=1)
    with connect(proxy) as sock:
        round_trip(sock, H2_PREFACE)
        proxy.arm("reset")
        round_trip(sock, request[:4])  # en-tête de trame incomplet : rien n'est encore décidé
        expect_reset(sock, request[4:])
    assert bytes(echo.received) == H2_PREFACE + request[:4]


# --- Proxy de chaos : compteurs, évènements, arrêt ----------------------------

def test_stats_count_relayed_traffic(proxy: ChaosProxy) -> None:
    with connect(proxy) as sock:
        round_trip(sock, b"x" * 100)
        round_trip(sock, b"y" * 50)
        stats = proxy.stats()
        assert list(stats) == STATS_KEYS
        assert stats == {**dict.fromkeys(STATS_KEYS, 0), "connections_total": 1, "connections_active": 1,
                         "bytes_up": 150, "bytes_down": 150, "chunks_up": 2, "chunks_down": 2}
    wait_until(lambda: proxy.stats()["connections_active"] == 0)
    proxy.reset_stats()
    assert proxy.stats() == dict.fromkeys(STATS_KEYS, 0)


def test_delay_events_describe_each_direction(proxy: ChaosProxy, recorder: Recorder) -> None:
    proxy.conditions.update(latency_ms=40)
    with connect(proxy) as sock:
        round_trip(sock, b"hello")
    events = recorder.of("network.delay")
    assert [event.detail for event in events] == [
        {"proxy": "test", "direction": direction, "delay_ms": 20.0, "spike": False, "bytes": 5}
        for direction in ("up", "down")
    ]
    for event in events:
        assert (event.side, event.protocol, event.call_id, event.size) == ("network", "test", "", 5)
        assert event.duration_us == pytest.approx(20_000)


def test_delays_under_one_millisecond_are_not_published(proxy: ChaosProxy, recorder: Recorder) -> None:
    proxy.conditions.update(latency_ms=1)
    with connect(proxy) as sock:
        round_trip(sock)
    assert recorder.events == []


def test_nothing_is_published_while_the_bus_is_muted(proxy: ChaosProxy, recorder: Recorder) -> None:
    proxy.conditions.update(latency_ms=10)
    proxy.arm("reset")
    with recorder.bus.muted():
        with connect(proxy) as sock:
            expect_reset(sock, b"x")
        with connect(proxy) as sock:
            round_trip(sock)
    assert recorder.events == []
    assert proxy.stats()["resets"] == 1


def test_stop_is_idempotent_cuts_connections_and_frees_the_port(make_proxy: Callable[..., ChaosProxy]) -> None:
    proxy = make_proxy()
    port = proxy.port
    with connect(proxy) as sock:
        round_trip(sock)
        proxy.stop()
        proxy.stop()
        expect_reset(sock)
    assert not proxy.running
    assert proxy.stats()["connections_active"] == 0
    assert not [thread.name for thread in threading.enumerate() if thread.name.startswith("chaos-test-")]
    with socket.socket() as probe:
        probe.bind((HOST, port))


def test_proxy_can_be_restarted(proxy: ChaosProxy) -> None:
    proxy.stop()
    proxy.start()
    with connect(proxy) as sock:
        round_trip(sock, b"encore")


# --- Résilience : politique de tentatives ------------------------------------

def test_retry_policy_defaults_follow_the_plan() -> None:
    policy = RetryPolicy()
    assert dataclasses.asdict(policy) == {
        "max_attempts": 3, "base_delay_ms": 100, "multiplier": 2.0, "max_delay_ms": 2000,
        "jitter": 0.2, "retry_on": ("TIMEOUT", "UNAVAILABLE"), "retry_non_idempotent": True,
    }
    assert RetryPolicy(retry_on=["TIMEOUT"]).retry_on == ("TIMEOUT",)


@pytest.mark.parametrize("options", [
    {"max_attempts": 0}, {"max_attempts": 2.5}, {"base_delay_ms": -1}, {"max_delay_ms": -1},
    {"multiplier": 0.5}, {"jitter": 1.5}, {"jitter": -0.1},
])
def test_retry_policy_rejects_invalid_values(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**options)


def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(base_delay_ms=100, multiplier=2.0, max_delay_ms=1000, jitter=0)
    assert [policy.delay_for(attempt) for attempt in range(1, 7)] == [100, 200, 400, 800, 1000, 1000]
    assert policy.delay_for(5000) == 1000


def test_backoff_jitter_stays_within_bounds_and_follows_the_generator() -> None:
    policy = RetryPolicy(base_delay_ms=100, jitter=0.2)
    delays = [policy.delay_for(2, random.Random(seed)) for seed in range(200)]
    assert all(160 <= delay <= 240 for delay in delays)
    assert min(delays) < 180 and max(delays) > 220
    assert policy.delay_for(2, random.Random(7)) == policy.delay_for(2, random.Random(7))


# --- Résilience : nouvelles tentatives ----------------------------------------

def test_retries_until_success(resilient: Callable[..., Any]) -> None:
    inner = FakeClient(timeout_error(), transport_error(), {"n": 5, "result": "120"})
    client, sleeps = resilient(inner, retry=RetryPolicy(max_attempts=4, jitter=0))
    assert client.calculate_factorial(5) == {"n": 5, "result": "120"}
    assert len(inner.calls) == 3
    assert sleeps == [0.1, 0.2]
    report = client.last_report
    assert report["outcome"] == "ok"
    assert [(a["n"], a["outcome"], a["code"], a["backoff_ms"]) for a in report["attempts"]] == [
        (1, "error", "TIMEOUT", 100.0), (2, "error", "UNAVAILABLE", 200.0), (3, "ok", "OK", 0.0),
    ]


def test_gives_up_after_max_attempts(resilient: Callable[..., Any], recorder: Recorder) -> None:
    errors = [timeout_error() for _ in range(3)]
    inner = FakeClient(*errors, {"jamais": "atteint"})
    client, sleeps = resilient(inner, retry=RetryPolicy(max_attempts=3, jitter=0))
    with pytest.raises(RpcTimeoutError) as raised:
        client.get_product_details("SKU-1001")
    assert raised.value is errors[-1]
    assert len(inner.calls) == 3 and sleeps == [0.1, 0.2]
    assert client.last_report["outcome"] == "gave_up"
    (event,) = recorder.of("resilience.give_up")
    assert event.detail["reason"] == "max_attempts" and event.detail["attempts"] == 3


def test_business_error_is_not_retried(resilient: Callable[..., Any], recorder: Recorder) -> None:
    inner = FakeClient(NotFoundError("Produit inconnu : SKU-0"))
    client, sleeps = resilient(inner, retry=RetryPolicy(max_attempts=5))
    with pytest.raises(NotFoundError):
        client.get_product_details("SKU-0")
    assert len(inner.calls) == 1 and sleeps == []
    assert client.last_report["outcome"] == "error"
    assert recorder.of("resilience.give_up") == []


def test_without_policy_there_is_exactly_one_attempt(resilient: Callable[..., Any]) -> None:
    inner = FakeClient(timeout_error(), {"ok": True})
    client, sleeps = resilient(inner)
    with pytest.raises(RpcTimeoutError):
        client.calculate_factorial(3)
    assert len(inner.calls) == 1 and sleeps == []
    assert client.last_report["outcome"] == "error"
    assert len(client.last_report["attempts"]) == 1


def test_only_listed_codes_are_retried(resilient: Callable[..., Any]) -> None:
    inner = FakeClient(timeout_error())
    client, _ = resilient(inner, retry=RetryPolicy(retry_on=("UNAVAILABLE",)))
    with pytest.raises(RpcTimeoutError):
        client.calculate_factorial(3)
    assert len(inner.calls) == 1


def test_non_idempotent_call_is_not_replayed_when_policy_forbids(
    resilient: Callable[..., Any], recorder: Recorder
) -> None:
    policy = RetryPolicy(retry_non_idempotent=False, jitter=0)
    inner = FakeClient(timeout_error(), {"applied": True})
    client, sleeps = resilient(inner, retry=policy)
    with pytest.raises(RpcTimeoutError):
        client.update_stock("SKU-1001", -1)
    assert len(inner.calls) == 1 and sleeps == []
    assert client.last_report["outcome"] == "gave_up"
    assert [event.detail["reason"] for event in recorder.of("resilience.give_up")] == ["non_idempotent"]

    reader = FakeClient(timeout_error(), {"id": "SKU-1001"})
    client, _ = resilient(reader, retry=policy)
    assert client.get_product_details("SKU-1001") == {"id": "SKU-1001"}  # une lecture, elle, se rejoue
    assert len(reader.calls) == 2


def test_non_idempotent_call_is_replayed_by_default(resilient: Callable[..., Any]) -> None:
    inner = FakeClient(timeout_error(), {"applied": True})
    client, _ = resilient(inner, retry=RetryPolicy())
    assert client.update_stock("SKU-1001", -1) == {"applied": True}
    assert [kwargs["idempotency_key"] for _, kwargs in inner.calls] == ["", ""]  # le piège du doublon


def test_idempotency_key_makes_the_replay_safe(resilient: Callable[..., Any]) -> None:
    inner = FakeClient(timeout_error(), {"applied": True})
    client, _ = resilient(inner, retry=RetryPolicy(retry_non_idempotent=False))
    assert client.update_stock("SKU-1001", -1, idempotency_key="commande-42") == {"applied": True}
    assert [kwargs["idempotency_key"] for _, kwargs in inner.calls] == ["commande-42", "commande-42"]


def test_auto_idempotency_key_is_reused_across_attempts(resilient: Callable[..., Any]) -> None:
    inner = FakeClient(timeout_error(), transport_error(), {"applied": True}, {"applied": True})
    client, _ = resilient(inner, retry=RetryPolicy(retry_non_idempotent=False), auto_idempotency_key=True)
    client.update_stock("SKU-1001", -1)
    client.update_stock("SKU-1001", -1)
    keys = [kwargs["idempotency_key"] for _, kwargs in inner.calls]
    assert len(keys) == 4 and all(keys)
    assert keys[0] == keys[1] == keys[2]  # une seule clé pour toutes les tentatives d'un même appel
    assert keys[3] != keys[0]             # mais une clé neuve pour l'appel suivant


def test_caller_idempotency_key_takes_precedence(resilient: Callable[..., Any]) -> None:
    inner = FakeClient()
    client, _ = resilient(inner, auto_idempotency_key=True)
    client.update_stock("SKU-1001", 2, idempotency_key="choisie")
    assert inner.calls[0][1]["idempotency_key"] == "choisie"


def test_timeout_is_forwarded_to_the_wrapped_client(resilient: Callable[..., Any]) -> None:
    inner = FakeClient()
    client, _ = resilient(inner, timeout=0.25)
    client.calculate_factorial(3)
    client.calculate_factorial(3, timeout=1.5)
    bare, _ = resilient(inner)
    bare.list_products(5, "Audio")
    assert [kwargs["timeout"] for _, kwargs in inner.calls] == [0.25, 1.5, None]
    assert inner.calls[2] == ("list_products", {"limit": 5, "category": "Audio", "timeout": None})


def test_dedup_is_reported_when_the_server_recognises_the_key(
    resilient: Callable[..., Any], recorder: Recorder
) -> None:
    inner = FakeClient(timeout_error(), {"product_id": "SKU-1001", "applied": False})
    client, _ = resilient(inner, retry=RetryPolicy(), auto_idempotency_key=True)
    assert client.update_stock("SKU-1001", -1)["applied"] is False
    (event,) = recorder.of("resilience.dedup")
    assert event.detail["idempotency_key"] == inner.calls[0][1]["idempotency_key"]
    assert event.detail["n"] == 2 and event.call_id == inner.last_call_id


# --- Résilience : disjoncteur --------------------------------------------------

def test_breaker_opens_after_consecutive_failures() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_s=2.0, name="inventaire", clock=clock)
    assert breaker.state == "closed" and breaker.allow()
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()  # un succès remet le compteur à zéro : les échecs doivent être consécutifs
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "open"
    assert not breaker.allow()

    clock.advance(1.5)
    snapshot = breaker.snapshot()
    assert snapshot["state"] == "open"
    assert (snapshot["failures"], snapshot["opened_at_age_s"], snapshot["retry_in_s"]) == (3, 1.5, 0.5)
    clock.advance(0.5)
    assert breaker.state == "half_open"


def test_half_open_breaker_allows_a_single_probe() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=2.0, clock=clock)
    breaker.record_failure()
    clock.advance(2.0)
    assert breaker.allow()        # l'appel d'essai
    assert not breaker.allow()    # un seul à la fois
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow() and breaker.allow()
    assert breaker.snapshot()["opened_at_age_s"] is None


def test_failed_probe_reopens_the_breaker_and_restarts_the_timer() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=2.0, clock=clock)
    breaker.record_failure()
    clock.advance(2.0)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open"
    clock.advance(1.9)
    assert breaker.state == "open"
    clock.advance(0.1)
    assert breaker.state == "half_open"


def test_breaker_snapshot_and_transitions() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=1.0, name="b", clock=clock)
    assert breaker.snapshot() == {
        "name": "b", "state": "closed", "failures": 0, "failure_threshold": 1, "reset_timeout_s": 1.0,
        "opened_at_age_s": None, "retry_in_s": 0.0, "opened_count": 0, "rejected_calls": 0,
    }
    breaker.record_failure()
    assert not breaker.allow()
    clock.advance(1.0)
    assert breaker.allow()
    breaker.record_success()
    assert [(t["from"], t["to"]) for t in breaker.drain_transitions()] == [
        ("closed", "open"), ("open", "half_open"), ("half_open", "closed"),
    ]
    assert breaker.drain_transitions() == []  # chaque transition n'est rendue qu'une fois
    snapshot = breaker.snapshot()
    assert (snapshot["opened_count"], snapshot["rejected_calls"]) == (1, 1)
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_breaker_reset_and_validation() -> None:
    breaker = CircuitBreaker(failure_threshold=1, clock=FakeClock())
    breaker.record_failure()
    breaker.reset()
    assert breaker.state == "closed" and breaker.snapshot()["failures"] == 0
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(reset_timeout_s=-1)


def test_breaker_survives_concurrent_use() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_s=0.001)
    errors: list[BaseException] = []

    def hammer(seed: int) -> None:
        rng = random.Random(seed)
        try:
            for _ in range(500):
                rng.choice((breaker.allow, breaker.record_failure, breaker.record_success, breaker.snapshot))()
        except BaseException as exc:  # noqa: BLE001 - tout plantage doit faire échouer le test
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(seed,)) for seed in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
    assert errors == []
    assert breaker.state in ("closed", "open", "half_open")


def test_business_error_proves_the_server_is_alive(resilient: Callable[..., Any]) -> None:
    breaker = CircuitBreaker(failure_threshold=2, clock=FakeClock())
    inner = FakeClient(timeout_error(), NotFoundError("inconnu"), timeout_error())
    client, _ = resilient(inner, breaker=breaker)
    for expected in (RpcTimeoutError, NotFoundError, RpcTimeoutError):
        with pytest.raises(expected):
            client.get_product_details("SKU-0")
    assert breaker.state == "closed"
    assert breaker.snapshot()["failures"] == 1


def test_open_breaker_short_circuits_without_touching_the_network(
    resilient: Callable[..., Any], recorder: Recorder
) -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout_s=5.0, clock=FakeClock())
    inner = FakeClient(transport_error(), transport_error(), {"jamais": "appelé"})
    client, _ = resilient(inner, breaker=breaker)
    for _ in range(2):
        with pytest.raises(RpcTransportError):
            client.calculate_factorial(3)
    assert breaker.state == "open"

    with pytest.raises(CircuitOpenError) as raised:
        client.calculate_factorial(3)
    assert len(inner.calls) == 2  # le troisième appel n'a jamais atteint le client enveloppé
    assert raised.value.code == "CIRCUIT_OPEN" and raised.value.method == "calculate_factorial"
    assert raised.value.detail["state"] == "open"
    report = client.last_report
    assert (report["outcome"], report["attempts"], report["breaker"]["state"]) == ("circuit_open", [], "open")
    assert [event.detail["reason"] for event in recorder.of("resilience.give_up")] == ["circuit_open"]


def test_half_open_probe_closes_the_breaker_through_the_client(
    resilient: Callable[..., Any], recorder: Recorder
) -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=2.0, name="inventaire", clock=clock)
    inner = FakeClient(transport_error(), {"n": 3, "result": "6"})
    client, _ = resilient(inner, breaker=breaker)
    with pytest.raises(RpcTransportError):
        client.calculate_factorial(3)
    with pytest.raises(CircuitOpenError):
        client.calculate_factorial(3)
    clock.advance(2.0)
    assert client.calculate_factorial(3) == {"n": 3, "result": "6"}
    assert breaker.state == "closed"
    transitions = recorder.of("resilience.breaker")
    assert [(event.detail["from"], event.detail["to"]) for event in transitions] == [
        ("closed", "open"), ("open", "half_open"), ("half_open", "closed"),
    ]
    assert all(event.detail["name"] == "inventaire" and event.detail["text"] for event in transitions)


def test_breaker_interrupts_a_retry_sequence(resilient: Callable[..., Any]) -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout_s=5.0, clock=FakeClock())
    inner = FakeClient(*[transport_error() for _ in range(5)])
    client, sleeps = resilient(inner, retry=RetryPolicy(max_attempts=5, jitter=0), breaker=breaker)
    with pytest.raises(CircuitOpenError) as raised:
        client.calculate_factorial(3)
    assert len(inner.calls) == 2 and len(sleeps) == 2
    assert isinstance(raised.value.__cause__, RpcTransportError)
    assert client.last_report["outcome"] == "circuit_open"
    assert len(client.last_report["attempts"]) == 2


# --- Résilience : rapport, évènements, flux ------------------------------------

def test_last_report_has_the_documented_shape(resilient: Callable[..., Any]) -> None:
    inner = FakeClient(timeout_error(), {"n": 4, "result": "24"})
    client, _ = resilient(inner, retry=RetryPolicy(jitter=0))
    assert client.last_report == {}
    client.calculate_factorial(4)
    report = client.last_report
    assert set(report) == {"method", "outcome", "total_ms", "breaker", "attempts"}
    assert (report["method"], report["outcome"], report["breaker"]) == ("calculate_factorial", "ok", None)
    assert report["total_ms"] >= 0
    for attempt in report["attempts"]:
        assert set(attempt) == {"n", "call_id", "outcome", "code", "duration_ms", "backoff_ms"}
        assert attempt["call_id"].startswith("fake-") and attempt["duration_ms"] >= 0
    first, second = report["attempts"]
    assert first["call_id"] != second["call_id"]
    assert client.last_call_id == inner.last_call_id == second["call_id"]
    assert json.loads(json.dumps(report)) == report

    guarded, _ = resilient(FakeClient(), breaker=CircuitBreaker(clock=FakeClock()))
    guarded.calculate_factorial(4)
    assert guarded.last_report["breaker"]["state"] == "closed"


def test_resilience_events_are_published(resilient: Callable[..., Any], recorder: Recorder) -> None:
    inner = FakeClient(timeout_error(), {"n": 4, "result": "24"})
    client, _ = resilient(inner, retry=RetryPolicy(jitter=0))
    client.calculate_factorial(4)
    assert recorder.stages() == ["resilience.attempt", "resilience.backoff", "resilience.attempt"]
    failed, backoff, succeeded = recorder.events
    attempts = client.last_report["attempts"]
    for event in recorder.events:
        assert (event.side, event.protocol, event.method) == ("resilience", "fake", "calculate_factorial")
    assert (failed.call_id, backoff.call_id, succeeded.call_id) == (
        attempts[0]["call_id"], attempts[0]["call_id"], attempts[1]["call_id"],
    )
    assert (failed.detail["n"], failed.detail["outcome"], failed.detail["code"]) == (1, "error", "TIMEOUT")
    assert failed.detail["max_attempts"] == 3
    assert (backoff.detail["backoff_ms"], backoff.detail["next_attempt"]) == (100.0, 2)
    assert backoff.duration_us == pytest.approx(100_000)
    assert (succeeded.detail["n"], succeeded.detail["outcome"], succeeded.detail["code"]) == (2, "ok", "OK")


def test_muted_bus_changes_nothing_but_the_events(resilient: Callable[..., Any], recorder: Recorder) -> None:
    breaker = CircuitBreaker(failure_threshold=1, clock=FakeClock())
    inner = FakeClient(timeout_error(), {"ok": True})
    client, sleeps = resilient(inner, retry=RetryPolicy(jitter=0), breaker=breaker)
    with recorder.bus.muted():
        with pytest.raises(CircuitOpenError):
            client.calculate_factorial(4)
    assert recorder.events == []
    assert len(inner.calls) == 1 and sleeps == [0.1]
    assert breaker.drain_transitions() == []  # relevées même sans publication


def test_stream_is_retried_before_the_first_item(resilient: Callable[..., Any]) -> None:
    items = [{"seq": 1}, {"seq": 2}]
    inner = FakeClient(transport_error(), [timeout_error()], items)
    client, sleeps = resilient(inner, retry=RetryPolicy(max_attempts=3, jitter=0))
    assert list(client.stream_analytics(2, 0)) == items
    assert len(inner.calls) == 3 and sleeps == [0.1, 0.2]
    assert client.last_report["outcome"] == "ok"
    assert [attempt["code"] for attempt in client.last_report["attempts"]] == ["UNAVAILABLE", "TIMEOUT", "OK"]


def test_stream_is_not_retried_once_started(resilient: Callable[..., Any], recorder: Recorder) -> None:
    inner = FakeClient([{"seq": 1}, transport_error()], [{"seq": 1}, {"seq": 2}])
    client, sleeps = resilient(inner, retry=RetryPolicy(max_attempts=3, jitter=0))
    stream = client.stream_analytics(2, 0)
    assert next(stream) == {"seq": 1}
    with pytest.raises(RpcTransportError):
        next(stream)
    assert len(inner.calls) == 1 and sleeps == []
    assert client.last_report["outcome"] == "gave_up"
    assert [event.detail["reason"] for event in recorder.of("resilience.give_up")] == ["stream_started"]


def test_stream_abandoned_by_the_caller_is_closed_cleanly(resilient: Callable[..., Any]) -> None:
    inner = FakeClient([{"seq": 1}, {"seq": 2}, {"seq": 3}])
    client, _ = resilient(inner)
    stream = client.stream_analytics(3, 0)
    assert next(stream) == {"seq": 1}
    stream.close()
    assert client.last_report["outcome"] == "ok"
    assert len(client.last_report["attempts"]) == 1


def test_open_breaker_refuses_a_stream(resilient: Callable[..., Any]) -> None:
    breaker = CircuitBreaker(failure_threshold=1, clock=FakeClock())
    breaker.record_failure()
    inner = FakeClient([{"seq": 1}])
    client, _ = resilient(inner, breaker=breaker)
    with pytest.raises(CircuitOpenError):
        list(client.stream_analytics(1, 0))
    assert inner.calls == []


# --- Résilience : contrat InventoryClient --------------------------------------

def test_wrapper_keeps_the_protocol_and_closes_the_wrapped_client(resilient: Callable[..., Any]) -> None:
    inner = FakeClient()
    client, _ = resilient(inner)
    assert isinstance(client, InventoryClient)
    assert client.protocol == "fake" and client.inner is inner
    with client:
        client.calculate_factorial(1)
    assert inner.closed


def test_invoke_and_submit_go_through_the_resilience_layer(resilient: Callable[..., Any]) -> None:
    inner = FakeClient(timeout_error(), {"n": 4}, timeout_error(), {"id": "SKU-1001"})
    client, sleeps = resilient(inner, retry=RetryPolicy(jitter=0))
    with client:
        assert client.invoke("calculate_factorial", {"n": 4}) == {"n": 4}
        future = client.submit("get_product_details", {"product_id": "SKU-1001"})
        assert future.result(timeout=5.0) == {"id": "SKU-1001"}
    assert len(inner.calls) == 4 and sleeps == [0.1, 0.1]


def test_optional_procedures_follow_the_wrapped_client(resilient: Callable[..., Any]) -> None:
    plain, _ = resilient(FakeClient())
    with pytest.raises(MethodNotFoundError):
        plain.invoke("bulk_update_stock", {"updates": []})
    with pytest.raises(MethodNotFoundError):
        plain.check_stock(["SKU-1001"])

    updates = [{"product_id": "SKU-1001", "delta": -2}]
    inner = FakeGrpcClient(
        timeout_error(), {"applied": 1},
        transport_error(), [{"product_id": "SKU-1001", "stock": 82}],
    )
    client, _ = resilient(inner, retry=RetryPolicy(jitter=0))
    assert client.invoke("bulk_update_stock", {"updates": iter(updates)}) == {"applied": 1}
    assert list(client.check_stock(iter(["SKU-1001"]))) == [{"product_id": "SKU-1001", "stock": 82}]
    assert [method for method, _ in inner.calls] == ["bulk_update_stock"] * 2 + ["check_stock"] * 2
    assert inner.calls[1][1]["updates"] == updates          # le flux client mémorisé est rejoué à l'identique
    assert inner.calls[3][1]["product_ids"] == ["SKU-1001"]
