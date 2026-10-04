"""API du dashboard (plan §13) : routes HTTP, tâches de fond, flux WebSocket, fichiers statiques.

Un seul laboratoire à ports éphémères, sur un bus de traces privé, sert tout le module ;
les rapports sont redirigés vers un dossier temporaire.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

with warnings.catch_warnings():
    # Starlette 1.x recommande httpx2, absent de l'environnement : l'avertissement d'import ne nous apprend rien.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from benchmark_lab.contract_evolution import CONTRACT_SCENARIOS
from common import config
from common.config import PROTOCOLS, REMOTE_PROTOCOLS
from common.inventory import InventoryService
from common.telemetry import PIPELINE, EventBus, TraceCollector
from dashboard import create_app
from dashboard.hub import DEFAULT_TOPICS, QUEUE_LIMIT, Hub, _Subscriber, tick_rates
from dashboard.summary import build_summary
from lab import LabRuntime

PRODUCT = "SKU-1001"
READ_PRODUCT = {"method": "get_product_details", "params": {"product_id": PRODUCT}}
TINY_BENCHMARK = {
    "iterations": 20,
    "warmup": 2,
    "serialization_iterations": 30,
    "sweep_latencies_ms": [0, 5],
    "sweep_iterations": 3,
    "wire_calls": 3,
    "scaling_limits": [1, 10],
}
JOB_PATIENCE_S = 40.0

Message = dict[str, Any]


# --- Outils --------------------------------------------------------------------------

class Inbox:
    """Lit un WebSocket de test dans un thread : chaque attente a une échéance, aucun test ne peut rester bloqué."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.seen: list[Message] = []
        self._messages: queue.Queue[Message | None] = queue.Queue()
        threading.Thread(target=self._pump, name="test-ws-inbox", daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                self._messages.put(self.session.receive_json())
        except Exception:   # session fermée par le test : fin de la lecture
            self._messages.put(None)

    def wait_for(self, wanted: Callable[[Message], bool], timeout: float = 6.0) -> Message:
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = self._messages.get(timeout=max(deadline - time.monotonic(), 0.01))
            except queue.Empty:
                received = [message["type"] for message in self.seen]
                raise AssertionError(f"message attendu non reçu en {timeout} s (reçus : {received})") from None
            assert message is not None, "WebSocket fermé avant le message attendu"
            self.seen.append(message)
            if wanted(message):
                return message


def of_type(kind: str, **fields: Any) -> Callable[[Message], bool]:
    return lambda message: message["type"] == kind and all(message.get(key) == value for key, value in fields.items())


@contextmanager
def live(client: TestClient, *topics: str) -> Iterator[Inbox]:
    """Connexion WebSocket prête à l'emploi : « hello » reçu, sujets supplémentaires confirmés."""
    with client.websocket_connect("/ws") as session:
        inbox = Inbox(session)
        inbox.wait_for(of_type("hello"))
        if topics:
            session.send_json({"type": "subscribe", "topics": list(topics)})
            inbox.wait_for(of_type("subscribed"))
        yield inbox


def call(client: TestClient, protocol: str, **body: Any) -> Message:
    response = client.post("/api/call", json={"protocol": protocol, **READ_PRODUCT, **body})
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_job(client: TestClient, job_id: str) -> Message:
    deadline = time.monotonic() + JOB_PATIENCE_S
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"la tâche {job_id} ne s'est pas terminée en {JOB_PATIENCE_S} s")


def error_of(response: Any, status: int) -> Message:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] and error["message"]
    return error


# --- Laboratoire partagé -------------------------------------------------------------

@pytest.fixture(scope="module")
def reports_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    folder = tmp_path_factory.mktemp("reports")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, "REPORTS_DIR", folder)
        yield folder


@pytest.fixture(scope="module")
def runtime() -> Iterator[LabRuntime]:
    with LabRuntime.ephemeral(bus=EventBus()) as lab:
        yield lab


@pytest.fixture(scope="module")
def client(runtime: LabRuntime, reports_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(runtime)) as api:
        yield api


@pytest.fixture(scope="module")
def benchmark(client: TestClient) -> Message:
    """Un banc d'essai complet, aux réglages minuscules, joué une fois pour le module."""
    with live(client) as inbox:
        response = client.post("/api/benchmark/run", json=TINY_BENCHMARK)
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        done = inbox.wait_for(of_type("job", job_id=job_id, state="done"), timeout=JOB_PATIENCE_S)
    return {"job_id": job_id, "done": done, "messages": [m for m in inbox.seen if m["type"] == "job"]}


@pytest.fixture
def ideal_network(client: TestClient) -> Iterator[None]:
    """Rend le réseau simulé parfait après un test qui le dérègle."""
    yield
    assert client.put("/api/network", json={"preset": "ideal"}).status_code == 200
    for protocol in REMOTE_PROTOCOLS:
        client.post("/api/network/arm", json={"kind": "reset", "protocol": protocol, "count": 0})


# --- État et catalogue ---------------------------------------------------------------

def test_health_and_status(client: TestClient, runtime: LabRuntime) -> None:
    assert client.get("/api/health").json() == {
        "status": "ok", "app": config.APP_NAME, "version": config.VERSION, "started": True,
    }
    status = client.get("/api/status").json()
    assert list(status)[:6] == ["app", "servers", "network", "proxies", "totals", "inventory"]
    assert [server["id"] for server in status["servers"]] == ["custom", "grpc", "rest", "custom_v2", "grpc_v2"]
    assert all(server["up"] for server in status["servers"])
    assert status["servers"][0]["port"] == runtime.ports.custom
    assert set(PROTOCOLS) <= set(status["totals"])
    assert status["job"] is None


def test_catalog_describes_methods_protocols_and_products(client: TestClient) -> None:
    catalog = client.get("/api/catalog").json()
    methods = {method["name"]: method for method in catalog["methods"]}
    assert len(methods) == 7
    assert methods["update_stock"]["required"] == ["product_id", "delta"]
    assert methods["list_products"]["required"] == []
    assert methods["check_stock"]["kind"] == "bidi_stream"
    assert [protocol["id"] for protocol in catalog["protocols"]] == list(PROTOCOLS)
    assert all(protocol["label"] and protocol["transport"] for protocol in catalog["protocols"])
    assert len(catalog["products"]) == 24
    assert set(catalog["products"][0]) == {"id", "name", "category", "stock"}
    assert "Réseau" in catalog["categories"]
    assert catalog["pipeline"] == list(PIPELINE)
    assert catalog["stages"]["client.marshal"]["label"] == "Marshalling"
    assert len(catalog["version"]) == 12


# --- /api/call -----------------------------------------------------------------------

@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_sync_call_on_each_protocol(client: TestClient, protocol: str) -> None:
    call(client, protocol)      # ouvre la connexion de la voie
    result = call(client, protocol)
    assert result["ok"] is True and result["error"] is None
    assert result["mode"] == "sync" and result["protocol"] == protocol
    assert result["result"]["id"] == PRODUCT
    assert result["call_id"].startswith(f"{protocol}-")
    assert 0 < result["duration_ms"] < 2000
    assert result["cold"] is False      # la voie garde son client : cet appel ne paie pas de connexion
    assert [attempt["code"] for attempt in result["attempts"]] == ["OK"]
    if protocol == "local":
        assert result["request_bytes"] is None and result["response_bytes"] is None
    else:
        assert result["request_bytes"] > 0 and result["response_bytes"] > 100
        assert result["server_ms"] > 0


def test_every_protocol_returns_the_same_result(client: TestClient) -> None:
    results = [call(client, protocol)["result"] for protocol in PROTOCOLS]
    assert all(result == results[0] for result in results)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_rpc_error_is_a_normal_result(client: TestClient, protocol: str) -> None:
    result = call(client, protocol, params={"product_id": "SKU-9999"})
    assert result["ok"] is False and result["result"] is None
    assert result["error"]["code"] == "NOT_FOUND"
    assert result["error"]["type"] == "NotFoundError"
    assert "SKU-9999" in result["error"]["message"]
    assert result["attempts"][0]["outcome"] == "error"


def test_method_unavailable_on_a_protocol_is_an_rpc_error(client: TestClient) -> None:
    body = {"method": "bulk_update_stock", "params": {"updates": [{"product_id": PRODUCT, "delta": 0}]}}
    refused = client.post("/api/call", json={"protocol": "rest", **body}).json()
    assert refused["ok"] is False and refused["error"]["code"] == "METHOD_NOT_FOUND"
    accepted = client.post("/api/call", json={"protocol": "grpc", **body}).json()
    assert accepted["ok"] is True and accepted["result"]["applied"] == 1


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"protocol": "soap", **READ_PRODUCT}, 422),
        ({"protocol": "grpc", "method": "get_product_details", "params": {"sku": PRODUCT}}, 422),
        ({"protocol": "grpc", "method": "update_stock", "params": {"product_id": PRODUCT}}, 422),
        ({"protocol": "grpc", "method": "stream_analytics", "params": {}}, 422),
        ({"protocol": "grpc", **READ_PRODUCT, "mode": "stream"}, 422),
        ({"protocol": "grpc", **READ_PRODUCT, "mode": "async", "count": 201}, 422),
        ({"protocol": "grpc", **READ_PRODUCT, "viaProxy": True}, 422),
        ({"protocol": "grpc", **READ_PRODUCT, "policy": {"max_attempts": 0}}, 422),
        ({"protocol": "grpc", "method": "teleport", "params": {}}, 404),
        ({"method": "get_product_details"}, 422),
    ],
)
def test_invalid_call_is_explained(client: TestClient, body: Message, status: int) -> None:
    error = error_of(client.post("/api/call", json=body), status)
    assert error["code"] in ("INVALID_ARGUMENT", "NOT_FOUND")


def test_malformed_body_is_a_400(client: TestClient) -> None:
    headers = {"Content-Type": "application/json"}
    error_of(client.post("/api/call", content=b"{protocol:", headers=headers), 400)
    error_of(client.post("/api/call", content=b'{"count": NaN}', headers=headers), 400)
    error_of(client.post("/api/call", content=b"[1, 2]", headers=headers), 422)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_async_mode_reports_each_call_and_the_speedup(client: TestClient, protocol: str) -> None:
    batch = call(client, protocol, method="calculate_factorial", params={"n": 300}, mode="async", count=24)
    assert batch["ok"] is True and batch["mode"] == "async"
    assert batch["count"] == 24 and batch["errors"] == 0
    assert len(batch["calls"]) == 24
    assert len({entry["call_id"] for entry in batch["calls"]}) == 24      # un identifiant de trace par appel
    assert all(entry["ok"] and entry["error"] is None and entry["duration_ms"] > 0 for entry in batch["calls"])
    assert batch["sum_ms"] == pytest.approx(sum(entry["duration_ms"] for entry in batch["calls"]), abs=0.01)
    assert batch["wall_ms"] > 0
    assert batch["speedup"] == pytest.approx(batch["sum_ms"] / batch["wall_ms"], abs=0.01)   # arrondi à 2 décimales
    assert batch["strategy"]
    trace = client.get(f"/api/traces/{batch['calls'][-1]['call_id']}").json()
    assert trace["summary"]["method"] == "calculate_factorial"


def test_async_mode_reports_failures_per_call(client: TestClient) -> None:
    batch = call(client, "custom", params={"product_id": "SKU-9999"}, mode="async", count=5)
    assert batch["ok"] is False and batch["errors"] == 5
    assert {entry["error"]["code"] for entry in batch["calls"]} == {"NOT_FOUND"}


def test_policy_retries_after_a_connection_reset(client: TestClient, ideal_network: None) -> None:
    armed = client.post("/api/network/arm", json={"kind": "reset", "protocol": "custom"}).json()
    assert armed["armed"]["custom"]["reset"] == 1
    result = call(client, "custom", via_proxy=True, policy={"max_attempts": 3, "base_delay_ms": 5})
    assert result["ok"] is True and result["outcome"] == "ok"
    assert [attempt["code"] for attempt in result["attempts"]] == ["UNAVAILABLE", "OK"]
    assert result["attempts"][0]["backoff_ms"] > 0
    assert result["call_id"] == result["attempts"][-1]["call_id"]
    assert result["duration_ms"] >= result["attempts"][0]["backoff_ms"]


def test_without_policy_a_reset_fails_then_the_lane_recovers(client: TestClient, ideal_network: None) -> None:
    client.post("/api/network/arm", json={"kind": "reset", "protocol": "rest"})
    failed = call(client, "rest", via_proxy=True)
    assert failed["ok"] is False and failed["error"]["code"] == "UNAVAILABLE"
    assert failed["error"]["retryable"] is True
    recovered = call(client, "rest", via_proxy=True)
    assert recovered["ok"] is True and recovered["cold"] is True     # client neuf après la panne


def test_timeout_is_honoured_and_reported(client: TestClient, ideal_network: None) -> None:
    assert client.put("/api/network", json={"preset": "blackhole"}).json()["conditions"]["blackhole"] is True
    started = time.perf_counter()
    result = call(client, "custom", via_proxy=True, timeout_ms=150)
    assert time.perf_counter() - started < 3.0
    assert result["ok"] is False and result["error"]["code"] == "TIMEOUT"
    assert 100 <= result["duration_ms"] < 2000
    client.put("/api/network", json={"preset": "ideal"})
    assert call(client, "custom", via_proxy=True)["ok"] is True


def test_breaker_is_shared_between_calls(client: TestClient, ideal_network: None) -> None:
    client.put("/api/network", json={"preset": "outage"})
    policy = {"max_attempts": 1, "breaker": True}
    for _ in range(3):
        failed = call(client, "custom", via_proxy=True, policy=policy)
        assert failed["error"]["code"] == "UNAVAILABLE" and len(failed["attempts"]) == 1
    refused = call(client, "custom", via_proxy=True, policy=policy)
    assert refused["error"]["code"] == "CIRCUIT_OPEN" and refused["outcome"] == "circuit_open"
    assert refused["attempts"] == [] and refused["call_id"] == ""
    assert refused["breaker"]["state"] == "open"
    assert client.get("/api/status").json()["breakers"]["custom"]["state"] == "open"


# --- /api/inspect et traces ----------------------------------------------------------

def test_inspect_returns_the_pipeline_of_three_protocols(client: TestClient) -> None:
    for protocol in REMOTE_PROTOCOLS:
        call(client, protocol)      # connexions ouvertes : l'inspection mesure l'appel seul
    response = client.post("/api/inspect", json=READ_PRODUCT)
    assert response.status_code == 200, response.text
    traces = response.json()["traces"]
    assert [trace["protocol"] for trace in traces] == list(REMOTE_PROTOCOLS)
    for trace in traces:
        events = trace["events"]
        assert trace["ok"] is True and trace["result"]["id"] == PRODUCT
        assert [event["stage"] for event in events] == list(PIPELINE)
        assert {event["call_id"] for event in events} == {trace["call_id"]}
        offsets = [event["offset_us"] for event in events]
        assert offsets[0] == 0 and offsets == sorted(offsets) and offsets[-1] > 0
        assert trace["summary"]["status"] == "ok" and trace["summary"]["events"] == len(PIPELINE)
        for event in events:
            if event["stage"] in ("client.send", "server.receive", "server.send", "client.receive"):
                assert len(event["payload_hex"]) == 2 * event["size"]
                segments = event["detail"]["segments"]
                assert segments[0]["start"] == 0 and segments[-1]["end"] == event["size"]
    custom, grpc, rest = traces
    assert bytes.fromhex(custom["events"][2]["payload_hex"])[4:].startswith(b'{"jsonrpc":"2.0"')
    assert grpc["events"][2]["payload_text"] is None                # binaire : pas de lecture en clair
    assert rest["events"][2]["payload_text"].startswith("GET /api/products/SKU-1001 HTTP/1.1")
    assert grpc["summary"]["response_bytes"] < custom["summary"]["response_bytes"]


def test_inspect_warms_fresh_connections_without_a_trace(runtime: LabRuntime) -> None:
    update = {"method": "update_stock", "params": {"product_id": PRODUCT, "delta": -1}}
    stock = runtime.service.get_product_details(PRODUCT)["stock"]
    traced = {protocol: totals["calls"] for protocol, totals in runtime.collector.totals.items()}
    factorials = runtime.service.stats()["calls"].get("calculate_factorial", 0)
    with TestClient(create_app(runtime)) as fresh:      # nouvelle application : aucune connexion ouverte
        traces = fresh.post("/api/inspect", json=update).json()["traces"]
    # Chaque voie neuve s'est échauffée hors trace : l'appel inspecté ne paie pas l'ouverture de sa connexion.
    assert [(trace["protocol"], trace["ok"], trace["cold"]) for trace in traces] == [
        (protocol, True, False) for protocol in REMOTE_PROTOCOLS
    ]
    assert all(trace["summary"]["method"] == "update_stock" for trace in traces)
    # L'échauffement n'a laissé ni trace ni effet : un débit par protocole, rien de plus.
    assert {protocol: totals["calls"] for protocol, totals in runtime.collector.totals.items()} == {
        protocol: traced.get(protocol, 0) + (protocol in REMOTE_PROTOCOLS) for protocol in {*traced, *REMOTE_PROTOCOLS}
    }
    assert runtime.service.get_product_details(PRODUCT)["stock"] == stock - len(REMOTE_PROTOCOLS)
    assert runtime.service.stats()["calls"]["calculate_factorial"] == factorials + len(REMOTE_PROTOCOLS)


def test_inspect_local_call_and_validation(client: TestClient) -> None:
    body = {**READ_PRODUCT, "protocols": ["local", "rest"]}
    local, rest = client.post("/api/inspect", json=body).json()["traces"]
    assert [event["stage"] for event in local["events"]] == ["client.call", "client.return"]
    assert len(rest["events"]) == len(PIPELINE)
    failed = client.post("/api/inspect", json={**READ_PRODUCT, "params": {"product_id": "SKU-9999"}}).json()
    assert all(trace["ok"] is False and trace["error"]["code"] == "NOT_FOUND" for trace in failed["traces"])
    assert all(trace["events"][-1]["stage"] == "client.error" for trace in failed["traces"])
    error_of(client.post("/api/inspect", json={"method": "stream_analytics", "params": {}}), 422)
    error_of(client.post("/api/inspect", json={**READ_PRODUCT, "protocols": ["soap"]}), 422)
    error_of(client.post("/api/inspect", json={**READ_PRODUCT, "protocols": []}), 422)


def test_traces_list_and_detail(client: TestClient) -> None:
    result = call(client, "grpc")
    summaries = client.get("/api/traces", params={"limit": 5}).json()["traces"]
    assert 1 <= len(summaries) <= 5
    assert summaries[0]["call_id"] == result["call_id"]         # le plus récent d'abord
    assert summaries[0]["status"] == "ok" and summaries[0]["protocol"] == "grpc"
    detail = client.get(f"/api/traces/{result['call_id']}").json()
    assert detail["summary"]["call_id"] == result["call_id"]
    assert detail["summary"]["duration_us"] == pytest.approx(result["duration_ms"] * 1000, abs=1)
    assert [event["stage"] for event in detail["events"]] == list(PIPELINE)
    assert detail["events"][0]["offset_us"] == 0
    error_of(client.get("/api/traces/grpc-999999"), 404)
    error_of(client.get("/api/traces", params={"limit": "beaucoup"}), 422)


# --- Réseau simulé -------------------------------------------------------------------

def test_network_conditions_presets_and_validation(client: TestClient, ideal_network: None) -> None:
    network = client.get("/api/network").json()
    assert set(network) == {"conditions", "presets", "proxies", "armed"}
    assert network["conditions"]["preset"] == "ideal"
    assert [preset["id"] for preset in network["presets"]][:3] == ["ideal", "lan", "wan"]
    assert all(preset["label"] and preset["description"] for preset in network["presets"])
    assert set(network["proxies"]) == set(REMOTE_PROTOCOLS)

    wan = client.put("/api/network", json={"preset": "wan"}).json()["conditions"]
    assert (wan["preset"], wan["latency_ms"], wan["jitter_ms"]) == ("wan", 40.0, 8.0)
    custom = client.put("/api/network", json={"latency_ms": 12, "jitter_ms": 0}).json()["conditions"]
    assert (custom["preset"], custom["latency_ms"]) == ("custom", 12.0)
    assert client.get("/api/status").json()["network"] == custom

    slowed = call(client, "rest", via_proxy=True)
    assert slowed["ok"] is True and slowed["duration_ms"] >= 10      # la latence injectée se voit dans l'appel

    for refused in ({"latence_ms": 5}, {"latency_ms": -1}, {"spike_probability": 2}, {"preset": "5g"},
                    {"down": "oui"}):
        error = error_of(client.put("/api/network", json=refused), 400)
        assert error["code"] == "INVALID_ARGUMENT"
    error_of(client.put("/api/network", json={}), 422)
    assert client.get("/api/network").json()["conditions"] == custom   # un refus ne modifie rien


def test_arm_a_fault(client: TestClient, ideal_network: None) -> None:
    armed = client.post("/api/network/arm", json={"kind": "lost_reply", "protocol": "grpc", "count": 2}).json()
    assert armed["armed"]["grpc"] == {"reset": 0, "lost_reply": 2}
    assert client.get("/api/network").json()["armed"]["grpc"]["lost_reply"] == 2
    disarmed = client.post("/api/network/arm", json={"kind": "lost_reply", "protocol": "grpc", "count": 0}).json()
    assert disarmed["armed"]["grpc"]["lost_reply"] == 0
    error_of(client.post("/api/network/arm", json={"kind": "explosion", "protocol": "grpc"}), 400)
    error_of(client.post("/api/network/arm", json={"kind": "reset", "protocol": "local"}), 400)
    error_of(client.post("/api/network/arm", json={"kind": "reset", "protocol": "grpc", "count": -1}), 422)
    error_of(client.post("/api/network/arm", json={"protocol": "grpc"}), 422)


# --- Banc d'essai --------------------------------------------------------------------

def test_benchmark_job_runs_to_completion(client: TestClient, benchmark: Message, reports_dir: Path) -> None:
    job = client.get(f"/api/jobs/{benchmark['job_id']}").json()
    assert job["kind"] == "benchmark" and job["state"] == "done" and job["error"] is None
    assert job["progress"] == 1.0 and job["phase"] == "done"
    report = job["result"]
    assert report["config"]["iterations"] == TINY_BENCHMARK["iterations"]
    assert [result["protocol"] for result in report["latency"]["results"]] == list(PROTOCOLS)
    assert all(result["count"] == 20 and result["errors"] == 0 for result in report["latency"]["results"])
    assert len(report["payload"]["rows"]) == 8 and len(report["network"]["points"]) == 2
    assert report["highlights"]
    assert job["partial"] == {}     # une fois la tâche finie, le rapport complet remplace les sections partielles

    # Progression diffusée en direct, puis un dernier message qui porte le rapport.
    running = [message for message in benchmark["messages"] if message["state"] == "running"]
    assert {message["phase"] for message in running} >= {"payload", "serialization", "latency", "network"}
    fractions = [message["progress"] for message in running]
    assert fractions == sorted(fractions) and 0 <= fractions[0] and fractions[-1] <= 1
    assert all(message["kind"] == "benchmark" and message["message"] for message in running)
    assert any(message["partial"] and "latency" in message["partial"] for message in running)
    assert benchmark["done"]["result"]["id"] == report["id"]

    assert client.get("/api/benchmark/latest").json()["id"] == report["id"]
    assert (reports_dir / f"{report['id']}.json").is_file()
    listed = client.get("/api/reports").json()["reports"]
    assert listed[0]["name"] == f"{report['id']}.json" and listed[0]["kind"] == "benchmark"
    assert client.get("/api/jobs").json()["active"] is None


def test_latest_benchmark_survives_a_restart(runtime: LabRuntime, benchmark: Message, client: TestClient) -> None:
    expected = client.get("/api/benchmark/latest").json()["id"]
    with TestClient(create_app(runtime)) as restarted:      # nouvelle application : rien en mémoire
        assert restarted.get("/api/benchmark/latest").json()["id"] == expected


def test_no_benchmark_yet_is_null(runtime: LabRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "vide")
    with TestClient(create_app(runtime)) as fresh:
        assert fresh.get("/api/benchmark/latest").json() is None
        assert fresh.get("/api/reports").json() == {"reports": []}


@pytest.mark.parametrize(
    "settings",
    [{"iterations": 0}, {"suites": ["vitesse"]}, {"protocols": ["soap"]}, {"method": "stream_analytics"},
     {"cadence": 3}, {"quick": "oui"}],
)
def test_benchmark_rejects_invalid_settings(client: TestClient, settings: Message) -> None:
    response = client.post("/api/benchmark/run", json=settings)
    assert response.status_code in (400, 422), response.text
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert client.get("/api/jobs").json()["active"] is None


def test_lab_is_busy_while_a_benchmark_runs(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = threading.Event(), threading.Event()

    def stalled_benchmark(runtime: LabRuntime, config: Message, progress: Callable[..., None]) -> Message:
        progress("latency", 0.5, "Mesure en cours", {"latency": {"results": []}})
        started.set()
        assert release.wait(JOB_PATIENCE_S)
        raise RuntimeError("banc d'essai interrompu par le test")

    monkeypatch.setattr("dashboard.jobs.run_full_benchmark", stalled_benchmark)
    job_id = client.post("/api/benchmark/run", json={"quick": True}).json()["job_id"]
    try:
        assert started.wait(JOB_PATIENCE_S)
        running = client.get(f"/api/jobs/{job_id}").json()
        assert (running["state"], running["progress"], running["phase"]) == ("running", 0.5, "latency")
        assert running["partial"] == {"latency": {"results": []}}      # de quoi dessiner en arrivant en cours de route
        assert client.get("/api/status").json()["job"]["id"] == job_id

        second = error_of(client.post("/api/benchmark/run", json={"quick": True}), 409)
        assert second["code"] == "BUSY" and second["job"]["id"] == job_id
        # Tout ce qui fausserait les mesures en cours est refusé de la même façon…
        error_of(client.post("/api/failures/run", json={"scenario": "connection_cut"}), 409)
        error_of(client.put("/api/network", json={"preset": "wan"}), 409)
        error_of(client.post("/api/network/arm", json={"kind": "reset", "protocol": "grpc"}), 409)
        error_of(client.post("/api/inspect", json=READ_PRODUCT), 409)
        error_of(client.post("/api/call", json={"protocol": "grpc", **READ_PRODUCT, "via_proxy": True}), 409)
        error_of(client.post("/api/reset"), 409)
        # … mais un appel direct reste possible.
        assert call(client, "grpc")["ok"] is True
    finally:
        release.set()
    failed = wait_for_job(client, job_id)
    assert failed["state"] == "error" and failed["result"] is None
    assert failed["error"] == {"code": "FAILED_PRECONDITION", "message": "banc d'essai interrompu par le test"}
    assert client.get("/api/status").json()["job"] is None


# --- Scénarios de pannes -------------------------------------------------------------

def test_failure_scenarios_metadata(client: TestClient) -> None:
    listing = client.get("/api/failures/scenarios").json()
    assert [scenario["id"] for scenario in listing["scenarios"]] == [
        "latency_trap", "timeout_spike", "connection_cut", "server_outage", "duplicate_execution",
    ]
    assert listing["scenarios"][0]["options"] == {"calls": 12, "latency_ms": 200}
    assert listing["metrics"]["slowdown_x"]["unit"] == "×"
    assert "attempt" in listing["step_kinds"] and "timeout" in listing["step_statuses"]


def test_failure_job_streams_its_steps_live(client: TestClient) -> None:
    with live(client) as inbox:
        response = client.post("/api/failures/run", json={"scenario": "connection_cut", "protocol": "custom"})
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        done = inbox.wait_for(of_type("job", job_id=job_id, state="done"), timeout=JOB_PATIENCE_S)
    steps = [m["step"] for m in inbox.seen if m["type"] == "job" and m.get("step") and m["job_id"] == job_id]
    assert steps[0]["kind"] == "note" and steps[0]["detail"] == {"scenario": "connection_cut", "protocol": "custom"}
    assert {"rpc_call", "network", "attempt", "backoff"} <= {step["kind"] for step in steps}
    assert [step["t_ms"] for step in steps] == sorted(step["t_ms"] for step in steps)
    assert all(m["state"] == "running" and m["kind"] == "failure" for m in inbox.seen if m.get("step"))

    result = done["result"]
    assert result["id"] == "connection_cut" and result["verdict"] and result["lesson"]
    assert result["metrics"]["naive_outcome"] == "UNAVAILABLE" and result["metrics"]["resilient_outcome"] == "OK"
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "done" and job["progress"] == 1.0
    assert job["steps"] == result["steps"] == steps
    assert client.get("/api/failures/scenarios").json()["results"]["connection_cut"]["verdict"] == result["verdict"]
    assert client.get("/api/network").json()["conditions"]["preset"] == "ideal"     # réseau rendu intact


def test_failure_run_validation(client: TestClient) -> None:
    error_of(client.post("/api/failures/run", json={"scenario": "meteor"}), 404)
    error_of(client.post("/api/failures/run", json={"scenario": "connection_cut", "protocol": "local"}), 422)
    error_of(client.post("/api/failures/run", json={"scenario": "latency_trap", "options": {"appels": 3}}), 422)
    error_of(client.get("/api/jobs/failure-inconnue"), 404)
    # Une option hors bornes n'est contrôlée que par le scénario : la tâche se termine en erreur, expliquée.
    job_id = client.post(
        "/api/failures/run", json={"scenario": "latency_trap", "options": {"calls": 500}}
    ).json()["job_id"]
    job = wait_for_job(client, job_id)
    assert job["state"] == "error" and job["error"]["code"] == "INVALID_ARGUMENT"
    assert "calls" in job["error"]["message"]


# --- Contrat et transparence ---------------------------------------------------------

def test_contract_overview(client: TestClient) -> None:
    overview = client.get("/api/contract").json()
    assert "service InventoryService" in overview["proto_v1"]["source"]
    assert overview["proto_v2"]["path"].endswith("service_v2.proto")
    assert overview["diff"] and overview["changes"] and overview["rules"]
    assert [scenario["id"] for scenario in overview["scenarios"]] == [s["id"] for s in CONTRACT_SCENARIOS]
    assert overview["stats"]["breaking"] > overview["stats"]["compatible"] > 0
    assert overview["strict"] is False


def test_contract_run_single_scenario(client: TestClient) -> None:
    response = client.post("/api/contract/run", json={"scenario": "renamed_rpc"})
    assert response.status_code == 200, response.text
    (result,) = response.json()["results"]
    assert (result["id"], result["outcome"], result["status"]) == ("renamed_rpc", "rejected", "UNIMPLEMENTED")
    assert result["matches"] is True and result["explanation"]
    assert client.get("/api/contract").json()["results"]["renamed_rpc"]["outcome"] == "rejected"
    error_of(client.post("/api/contract/run", json={"scenario": "big_bang"}), 404)
    error_of(client.post("/api/contract/run", json={"scenario": "renamed_rpc", "strict": "oui"}), 422)


def test_contract_run_all_scenarios(client: TestClient, runtime: LabRuntime) -> None:
    body = client.post("/api/contract/run", json={"scenario": "all", "strict": True}).json()
    results = body["results"]
    assert [result["id"] for result in results] == [scenario["id"] for scenario in CONTRACT_SCENARIOS]
    assert all(result["matches"] for result in results)
    assert {result["outcome"] for result in results} == {"rejected", "silent_corruption", "crash", "compatible"}
    assert body["strict"] is True and runtime.grpc_v2.strict is True
    assert client.post("/api/contract/run", json={"strict": False}).json()["strict"] is False


def test_code_compare_and_its_execution(client: TestClient, runtime: LabRuntime) -> None:
    comparison = client.get("/api/code-compare").json()
    assert [snippet["id"] for snippet in comparison["snippets"]] == ["local", "custom", "grpc", "rest"]
    assert "def update_stock_rest_by_hand" in comparison["snippets"][3]["code"]
    assert comparison["javascript_fetch"]["language"] == "javascript" and comparison["takeaway"]

    stock = runtime.service.check_stock(PRODUCT)["stock"]
    run = client.post("/api/code-compare/run", json={"quantity": 2, "repeats": 2}).json()
    assert run["equivalent"] is True and run["expected_new_stock"] == stock - 2
    assert [entry["result"] for entry in run["runs"]] == [stock - 2] * 4
    assert runtime.service.check_stock(PRODUCT)["stock"] == stock
    error_of(client.post("/api/code-compare/run", json={"product_id": "SKU-9999"}), 404)
    error_of(client.post("/api/code-compare/run", json={"repeats": 0}), 422)


# --- Bilan ---------------------------------------------------------------------------

def test_summary_without_measures_invents_nothing(
    runtime: LabRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "vide")
    with TestClient(create_app(runtime)) as fresh:
        summary = fresh.get("/api/summary").json()
    assert summary["measured"] is False
    assert summary["sources"] == {
        "benchmark_id": None, "benchmark_created_at": None,
        "failure_scenarios": [], "contract_scenarios": [], "async_batches": [],
    }
    assert len(summary["advantages"]) >= 5 and len(summary["drawbacks"]) >= 5
    for item in summary["advantages"] + summary["drawbacks"]:
        assert set(item) == {"id", "title", "text", "metric", "evidence", "source"}
        assert item["title"] and item["text"] and item["evidence"]
        # Seuls les chiffres lus dans le code du dépôt sont disponibles sans expérience.
        assert item["metric"] is None or item["source"] == "code"
    assert len(summary["comparison"]) >= 10
    assert all(set(row) == {"criterion", *PROTOCOLS, "measured"} for row in summary["comparison"])
    assert not any(row["measured"] for row in summary["comparison"])


def test_summary_is_fed_by_the_measures(client: TestClient, benchmark: Message) -> None:
    call(client, "custom", method="calculate_factorial", params={"n": 50}, mode="async", count=10)
    client.post("/api/contract/run", json={"scenario": "field_reuse_silent"})
    job_id = client.post("/api/failures/run", json={"scenario": "duplicate_execution"}).json()["job_id"]
    assert wait_for_job(client, job_id)["state"] == "done"

    summary = client.get("/api/summary").json()
    report = client.get("/api/benchmark/latest").json()
    assert summary["measured"] is True
    assert summary["sources"]["benchmark_id"] == report["id"]
    assert "duplicate_execution" in summary["sources"]["failure_scenarios"]
    assert "field_reuse_silent" in summary["sources"]["contract_scenarios"]
    items = {item["id"]: item for item in summary["advantages"] + summary["drawbacks"]}

    row = next(
        row for row in report["payload"]["rows"]
        if row["method"] == "get_product_details" and row["direction"] == "response"
    )
    compact = items["compact_messages"]
    assert compact["source"] == "benchmark" and compact["metric"]["unit"] == "%"
    assert compact["metric"]["value"] == row["protobuf_vs_json_pct"] < 0
    fastest = min((r for r in report["latency"]["results"] if r["protocol"] != "local"), key=lambda r: r["mean_ms"])
    assert items["speed"]["metric"]["value"] == fastest["mean_ms"]
    assert items["remote_cost"]["metric"]["unit"] == "×" and items["remote_cost"]["metric"]["value"] > 1
    assert items["multiplexing"]["source"] == "calls" and items["multiplexing"]["metric"]["value"] > 0
    duplicate = items["duplicate_execution"]
    assert duplicate["source"] == "failures" and duplicate["metric"]["value"] == 2
    assert items["contract_coupling"]["source"] == "contract" and items["contract_coupling"]["metric"]["value"] >= 1
    assert sum(row["measured"] for row in summary["comparison"]) == 3


def test_summary_tolerates_a_partial_report() -> None:
    report = {"id": "benchmark-20260101-000000", "latency": None, "payload": {"rows": [], "wire": []}, "network": None}
    summary = build_summary(report, {}, {})
    assert summary["measured"] is True
    assert all(item["metric"] is None or item["source"] == "code" for item in summary["advantages"])


# --- Rapports ------------------------------------------------------------------------

def test_report_download_in_three_formats(client: TestClient, benchmark: Message) -> None:
    name = client.get("/api/reports").json()["reports"][0]["name"]
    stem = name.removesuffix(".json")

    as_json = client.get(f"/api/reports/{name}")
    assert as_json.status_code == 200 and as_json.headers["content-type"] == "application/json"
    assert as_json.headers["content-disposition"] == f'attachment; filename="{stem}.json"'
    assert as_json.json()["id"] == stem

    as_markdown = client.get(f"/api/reports/{stem}", params={"format": "md"})
    assert as_markdown.headers["content-type"].startswith("text/markdown")
    assert as_markdown.headers["content-disposition"] == f'attachment; filename="{stem}.md"'
    assert as_markdown.text.startswith("# ") and "| Protocole" in as_markdown.text

    as_csv = client.get(f"/api/reports/{name}", params={"format": "csv"})
    assert as_csv.headers["content-type"].startswith("text/csv")
    assert as_csv.headers["content-disposition"] == f'attachment; filename="{stem}.csv"'
    assert as_csv.text.splitlines()[0] == "section,subject,series,metric,value,unit"

    error_of(client.get(f"/api/reports/{name}", params={"format": "pdf"}), 422)
    error_of(client.get("/api/reports/benchmark-19990101-000000.json"), 404)


@pytest.mark.parametrize(
    "name",
    ["..%2F..%2Fpytest.ini", "..%5C..%5Cpytest.ini", "%2E%2E", "C:%5CWindows%5Cwin.ini", "..%2Fsecret.json",
     ".hidden.json", "a%00b.json"],
)
def test_report_names_cannot_escape_the_folder(client: TestClient, benchmark: Message, name: str) -> None:
    for export in ("json", "md", "csv"):
        response = client.get(f"/api/reports/{name}?format={export}")
        assert response.status_code in (400, 404), response.text
        assert "error" in response.json()


# --- Remise à zéro -------------------------------------------------------------------

def test_reset_restores_the_initial_lab(client: TestClient, runtime: LabRuntime) -> None:
    initial_units = client.get("/api/status").json()["inventory"]["total_units"]
    assert call(client, "grpc", method="update_stock", params={"product_id": PRODUCT, "delta": -7})["ok"]
    client.put("/api/network", json={"preset": "wan"})
    client.post("/api/network/arm", json={"kind": "reset", "protocol": "custom", "count": 3})
    client.post("/api/contract/run", json={"scenario": "added_field"})
    assert client.get("/api/status").json()["inventory"]["total_units"] == initial_units - 7

    response = client.post("/api/reset")
    assert response.status_code == 200 and response.json()["ok"] is True
    status = response.json()["status"]
    assert status["inventory"]["total_units"] == InventoryService().stats()["total_units"]
    assert status["inventory"]["operations"] == 0
    assert status["network"]["preset"] == "ideal"
    assert all(totals["calls"] == 0 for totals in status["totals"].values())
    assert status["breakers"] == {}
    assert client.get("/api/network").json()["armed"]["custom"] == {"reset": 0, "lost_reply": 0}
    assert client.get("/api/traces").json() == {"traces": []}
    assert client.get("/api/contract").json()["results"] == {}
    assert call(client, "custom")["ok"] is True     # les connexions des voies ont survécu


# --- WebSocket -----------------------------------------------------------------------

def test_ws_hello(client: TestClient) -> None:
    with client.websocket_connect("/ws") as session:
        hello = session.receive_json()
    assert hello["type"] == "hello"
    assert hello["app"] == {"name": config.APP_NAME, "tagline": config.APP_TAGLINE, "version": config.VERSION}
    assert hello["catalog_version"] == client.get("/api/catalog").json()["version"]
    assert hello["network"]["preset"] == "ideal"
    assert hello["topics"] == sorted(DEFAULT_TOPICS) and "trace" not in hello["topics"]
    assert "trace" in hello["available_topics"]
    assert hello["job"] is None


def test_ws_stats_tick_carries_status_and_rates(client: TestClient) -> None:
    with live(client) as inbox:
        inbox.wait_for(of_type("stats"))            # premier battement : sert de point de départ
        for _ in range(5):
            call(client, "rest")
        stats = inbox.wait_for(lambda m: m["type"] == "stats" and m["rates"]["rest"]["calls_per_s"] > 0)
    assert {"app", "servers", "network", "proxies", "totals", "inventory", "rates", "job", "ts"} <= set(stats)
    assert all(server["up"] for server in stats["servers"])
    assert set(stats["rates"]) >= set(PROTOCOLS)
    rest = stats["rates"]["rest"]
    assert set(rest) == {"calls_per_s", "errors_per_s", "bytes_out_per_s", "bytes_in_per_s", "avg_ms"}
    assert rest["errors_per_s"] == 0 and rest["avg_ms"] > 0 and rest["bytes_in_per_s"] > 0
    assert stats["rates"]["local"] == {
        "calls_per_s": 0, "errors_per_s": 0, "bytes_out_per_s": 0, "bytes_in_per_s": 0, "avg_ms": None,
    }
    assert stats["inventory"]["products"] == 24


def test_tick_rates_survive_a_counter_reset() -> None:
    before = (10.0, {"grpc": {"calls": 50, "errors": 5, "bytes_out": 500, "bytes_in": 900, "total_us": 80_000.0}})
    after = (12.0, {"grpc": {"calls": 4, "errors": 0, "bytes_out": 40, "bytes_in": 80, "total_us": 6_000.0}})
    rates = tick_rates(before, after)
    assert rates["grpc"] == {
        "calls_per_s": 2.0, "errors_per_s": 0.0, "bytes_out_per_s": 20.0, "bytes_in_per_s": 40.0, "avg_ms": 1.5,
    }
    assert rates["custom"]["calls_per_s"] == 0 and rates["custom"]["avg_ms"] is None


def test_ws_call_event_after_each_call(client: TestClient) -> None:
    with live(client) as inbox:
        succeeded = call(client, "custom")
        event = inbox.wait_for(of_type("call", call_id=succeeded["call_id"]))
        assert (event["protocol"], event["method"], event["status"]) == ("custom", "get_product_details", "ok")
        assert event["request_bytes"] == succeeded["request_bytes"]
        assert event["duration_us"] == pytest.approx(succeeded["duration_ms"] * 1000, abs=1)
        failed = call(client, "rest", params={"product_id": "SKU-9999"})
        event = inbox.wait_for(of_type("call", call_id=failed["call_id"]))
        assert event["status"] == "error" and event["error"]["code"] == "NOT_FOUND"


def test_ws_trace_only_after_subscribing(client: TestClient) -> None:
    with live(client) as inbox:
        first = call(client, "grpc")
        inbox.wait_for(of_type("call", call_id=first["call_id"]))
        assert not [message for message in inbox.seen if message["type"] == "trace"]

        inbox.session.send_json({"type": "subscribe", "topics": ["trace", "inconnu"]})
        assert "trace" in inbox.wait_for(of_type("subscribed"))["topics"]
        second = call(client, "grpc")
        inbox.wait_for(of_type("call", call_id=second["call_id"]))
        inbox.session.send_json({"type": "unsubscribe", "topics": ["trace"]})
        assert "trace" not in inbox.wait_for(of_type("subscribed"))["topics"]
        third = call(client, "grpc")
        inbox.wait_for(of_type("call", call_id=third["call_id"]))

    traced = [message["event"] for message in inbox.seen if message["type"] == "trace"]
    assert {event["call_id"] for event in traced} == {second["call_id"]}
    assert [event["stage"] for event in traced] == list(PIPELINE)
    sent = traced[2]
    assert sent["payload_hex"] and sent["detail"]["segments"] and sent["size"] == second["request_bytes"]


def test_ws_network_and_resilience_events(client: TestClient, ideal_network: None) -> None:
    with live(client) as inbox:
        client.post("/api/network/arm", json={"kind": "reset", "protocol": "custom"})
        result = call(client, "custom", via_proxy=True, policy={"max_attempts": 2, "base_delay_ms": 1})
        assert result["ok"] is True
        # Le client résilient publie le bilan de sa tentative une fois l'appel revenu : c'est le dernier message.
        inbox.wait_for(lambda m: m["type"] == "resilience" and m["event"]["detail"].get("outcome") == "ok")
    network = [message["event"] for message in inbox.seen if message["type"] == "network"]
    resilience = [message["event"] for message in inbox.seen if message["type"] == "resilience"]
    assert [event["stage"] for event in network] == ["network.reset"]
    assert network[0]["protocol"] == "custom" and network[0]["detail"]["reason"] == "armed"
    assert [event["stage"] for event in resilience] == [
        "resilience.attempt", "resilience.backoff", "resilience.attempt",
    ]


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_ws_stream_then_stream_end(client: TestClient, protocol: str) -> None:
    with live(client) as inbox:
        body = {"protocol": protocol, "method": "stream_analytics", "mode": "stream",
                "params": {"samples": 4, "interval_ms": 15}}
        response = client.post("/api/call", json=body)
        assert response.status_code == 200, response.text
        started = response.json()
        assert started["ok"] is True and started["mode"] == "stream" and started["stream_id"].startswith("stream-")
        end = inbox.wait_for(of_type("stream_end", stream_id=started["stream_id"]))
    items = [m for m in inbox.seen if m["type"] == "stream" and m["stream_id"] == started["stream_id"]]
    assert [message["seq"] for message in items] == [1, 2, 3, 4]
    assert [message["item"]["seq"] for message in items] == [1, 2, 3, 4]
    assert items[0]["item"]["total_units"] > 0
    elapsed = [message["elapsed_ms"] for message in items]
    assert elapsed == sorted(elapsed) and elapsed[-1] - elapsed[0] >= 30    # trois intervalles de 15 ms
    assert (end["count"], end["error"], end["result"]) == (4, None, None)
    assert end["duration_ms"] >= elapsed[-1] and end["protocol"] == protocol


def test_ws_client_and_bidirectional_streams(client: TestClient, runtime: LabRuntime) -> None:
    stock = runtime.service.check_stock("SKU-1005")["stock"]
    with live(client) as inbox:
        updates = [{"product_id": "SKU-1005", "delta": 4}, {"product_id": "SKU-1005", "delta": -4},
                   {"product_id": "SKU-9999", "delta": 1}]
        bulk = client.post("/api/call", json={
            "protocol": "grpc", "method": "bulk_update_stock", "mode": "stream", "params": {"updates": updates},
        }).json()
        end = inbox.wait_for(of_type("stream_end", stream_id=bulk["stream_id"]))
        assert end["count"] == 0 and end["error"] is None       # flux client : un bilan unique, aucun élément
        assert (end["result"]["applied"], end["result"]["rejected"], end["result"]["total_delta"]) == (2, 1, 0)
        assert end["call_id"].startswith("grpc-")

        check = client.post("/api/call", json={
            "protocol": "grpc", "method": "check_stock", "mode": "stream",
            "params": {"product_ids": ["SKU-1005", "SKU-1012"]},
        }).json()
        end = inbox.wait_for(of_type("stream_end", stream_id=check["stream_id"]))
        assert end["count"] == 2 and end["error"] is None
        levels = [m["item"] for m in inbox.seen if m["type"] == "stream" and m["stream_id"] == check["stream_id"]]
        assert [(level["product_id"], level["stock"]) for level in levels][0] == ("SKU-1005", stock)

        refused = client.post("/api/call", json={
            "protocol": "rest", "method": "check_stock", "mode": "stream", "params": {"product_ids": ["SKU-1005"]},
        }).json()
        end = inbox.wait_for(of_type("stream_end", stream_id=refused["stream_id"]))
        assert end["count"] == 0 and end["error"]["code"] == "METHOD_NOT_FOUND"

        invalid = client.post("/api/call", json={
            "protocol": "custom", "method": "stream_analytics", "mode": "stream", "params": {"samples": 0},
        }).json()
        end = inbox.wait_for(of_type("stream_end", stream_id=invalid["stream_id"]))
        assert end["error"]["code"] == "INVALID_ARGUMENT"


def test_slow_client_loses_its_oldest_messages_only() -> None:
    """File bornée : un client qui ne lit plus ne retient ni le laboratoire ni la mémoire du serveur."""
    sent: list[str] = []

    class StalledThenReading:
        async def send_text(self, text: str) -> None:
            sent.append(text)

    subscriber = _Subscriber(StalledThenReading())      # type: ignore[arg-type]
    for number in range(QUEUE_LIMIT + 5):               # personne ne lit : la file déborde de 5 messages
        subscriber.push(str(number))
    assert subscriber.dropped == 5

    async def drain() -> None:
        pump = asyncio.create_task(subscriber.pump())
        await asyncio.sleep(0.01)
        pump.cancel()

    asyncio.run(drain())
    assert sent == [str(number) for number in range(5, QUEUE_LIMIT + 5)]


def test_hub_does_no_work_for_a_topic_nobody_listens_to() -> None:
    bus = EventBus()
    hub = Hub(bus, TraceCollector(bus).start(), status=dict, greeting=dict)
    built: list[str] = []

    def costly_message() -> Message:
        built.append("sérialisé")
        return {"type": "trace"}

    async def scenario() -> None:
        hub.publish("trace", costly_message)            # hub arrêté : sans effet
        await hub.start()
        hub.publish("trace", costly_message)            # démarré, mais aucun abonné
        for stage in ("client.call", "client.return"):  # un appel complet : ni « trace » ni « call » à diffuser
            bus.emit(call_id="custom-1", protocol="custom", side="client", stage=stage)
        await asyncio.sleep(0.02)
        await hub.stop()

    asyncio.run(scenario())
    assert built == []


# --- Interface statique --------------------------------------------------------------

def test_missing_interface_gets_a_french_fallback_page(runtime: LabRuntime, tmp_path: Path) -> None:
    with TestClient(create_app(runtime, static_dir=tmp_path / "absent")) as bare:
        page = bare.get("/")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        assert "n'est pas encore construite" in page.text and 'lang="fr"' in page.text
        assert page.headers["cache-control"] == "no-store"
        assert bare.get("/api/health").json()["status"] == "ok"      # l'API ne dépend pas de l'interface
        error_of(bare.get("/js/app.js"), 404)


def test_static_files_are_served_after_the_api_and_never_cached(runtime: LabRuntime, tmp_path: Path) -> None:
    (tmp_path / "js").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Interface de test</title>", encoding="utf-8")
    (tmp_path / "js" / "app.js").write_text("export const ready = true;\n", encoding="utf-8")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "health").write_text("masqué par la route de l'API", encoding="utf-8")
    with TestClient(create_app(runtime, static_dir=tmp_path)) as site:
        index = site.get("/")
        assert "Interface de test" in index.text and index.headers["cache-control"] == "no-store"
        script = site.get("/js/app.js")
        assert script.text.startswith("export const ready") and script.headers["cache-control"] == "no-store"
        assert "javascript" in script.headers["content-type"]
        assert site.get("/api/health").json()["status"] == "ok"
        assert site.get("/api/health").headers["cache-control"] == "no-store"
        missing = site.get("/js/absent.js")
        assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
        error_of(site.get("/api/inconnue"), 404)
        error_of(site.delete("/api/status"), 405)


def test_api_reports_a_lab_that_is_not_started() -> None:
    with TestClient(create_app(LabRuntime.ephemeral(bus=EventBus()))) as cold:
        assert cold.get("/api/health").json()["started"] is False
        assert not any(server["up"] for server in cold.get("/api/status").json()["servers"])
        for path, body in (("/api/call", {"protocol": "grpc", **READ_PRODUCT}), ("/api/inspect", READ_PRODUCT),
                           ("/api/benchmark/run", {"quick": True}), ("/api/contract/run", {}),
                           ("/api/failures/run", {"scenario": "connection_cut"})):
            assert error_of(cold.post(path, json=body), 503)["code"] == "UNAVAILABLE"


def test_messages_are_plain_json(client: TestClient) -> None:
    """Les réponses sont du JSON strict en UTF-8 : accents intacts, aucun NaN."""
    raw = client.get("/api/catalog").content
    assert "Périphériques".encode("utf-8") in raw
    json.loads(raw, parse_constant=lambda name: pytest.fail(f"constante non standard : {name}"))
