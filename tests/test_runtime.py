"""Orchestrateur du laboratoire : cycle de vie, clients, réseau simulé, contrat v2, état, remise à zéro.

Chaque laboratoire de ce fichier écoute sur des ports éphémères et publie sur un
bus privé : les tests ne dépendent ni des ports par défaut ni du bus global, et
peuvent tourner pendant qu'un vrai laboratoire est ouvert sur la machine.
"""
from __future__ import annotations

import dataclasses
import json
import os
import socket
import threading
import time
from typing import Any, Callable, Iterator

import pytest

from common.client_api import InventoryClient
from common.config import APP_NAME, HOST, PROTOCOLS, REMOTE_PROTOCOLS, VERSION, Ports, default_ports
from common.errors import (
    METHOD_NOT_FOUND,
    InvalidArgumentError,
    MethodNotFoundError,
    NotFoundError,
    RpcTimeoutError,
    RpcTransportError,
)
from common.inventory import InventoryService
from common.telemetry import BUS, PIPELINE, EventBus, TraceEvent
from lab import CONTRACT_PROTOCOLS, LabRuntime
from netsim import NetworkConditions

PRODUCT = "SKU-1001"
INITIAL_STOCK = 84
UNKNOWN_PRODUCT = "SKU-0000"
LATENCY_MS = 120
SERVER_IDS = ["custom", "grpc", "rest", "custom_v2", "grpc_v2"]
# Les huit ports qu'occupe un laboratoire (celui du dashboard ne lui appartient pas).
LAB_PORTS = ("custom", "custom_proxy", "custom_v2", "grpc", "grpc_proxy", "grpc_v2", "rest", "rest_proxy")
UNSET_PORTS = Ports(**dict.fromkeys(Ports().as_dict(), 0))
ROUTES = [(protocol, via_proxy) for protocol in PROTOCOLS for via_proxy in (False, True)]
ROUTE_IDS = [f"{protocol}-{'proxy' if via_proxy else 'direct'}" for protocol, via_proxy in ROUTES]


# --- Outils ------------------------------------------------------------------

def new_lab() -> LabRuntime:
    return LabRuntime.ephemeral(bus=EventBus())


def components(runtime: LabRuntime) -> list[Any]:
    return [*runtime.servers.values(), *runtime.proxies.values()]


def can_bind(port: int) -> bool:
    """Vrai si plus personne n'écoute sur ``port`` (liaison exclusive, comme le ferait un nouveau serveur)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        if os.name == "nt":
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            candidate.bind((HOST, port))
        except OSError:
            return False
    return True


def elapsed_ms(call: Callable[[], Any]) -> float:
    started = time.perf_counter()
    call()
    return (time.perf_counter() - started) * 1000


def wait_until(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.perf_counter() + timeout
    while not condition():
        if time.perf_counter() > deadline:
            return False
        time.sleep(0.01)
    return True


@pytest.fixture(scope="module")
def shared_lab() -> Iterator[LabRuntime]:
    with new_lab() as runtime:
        yield runtime


@pytest.fixture
def lab(shared_lab: LabRuntime) -> Iterator[LabRuntime]:
    """Le laboratoire partagé du module, remis à zéro avant et après chaque test."""
    shared_lab.reset()
    yield shared_lab
    shared_lab.reset()


# --- Cycle de vie ------------------------------------------------------------

def test_default_runtime_follows_the_configured_port_plan() -> None:
    runtime = LabRuntime()   # rien n'écoute tant que start() n'est pas appelé

    assert runtime.ports == default_ports()
    assert runtime.bus is BUS
    assert runtime.host == HOST
    assert not runtime.started
    assert not any(component.running for component in components(runtime))


def test_ephemeral_ports_are_resolved_by_start() -> None:
    runtime = new_lab()
    assert runtime.ports == UNSET_PORTS

    with runtime:
        ports = runtime.ports.as_dict()
        assert ports.pop("dashboard") == 0   # le dashboard n'appartient pas au laboratoire
        assert all(ports.values())
        assert len(set(ports.values())) == len(LAB_PORTS)
        for name, server in runtime.servers.items():
            assert server.port == ports[name]
        for name, proxy in runtime.proxies.items():
            assert proxy.port == ports[f"{name}_proxy"]
            assert proxy.target_port == ports[name]   # le proxy vise le port RÉEL de son serveur


def test_start_is_idempotent() -> None:
    with new_lab() as runtime:
        ports = runtime.ports

        assert runtime.start() is runtime
        assert runtime.started
        assert runtime.ports == ports
        with runtime.client("rest") as client:
            assert client.calculate_factorial(5)["result"] == "120"


def test_stop_is_idempotent_and_frees_every_port() -> None:
    new_lab().stop()   # jamais démarré : sans effet
    runtime = new_lab().start()
    ports = runtime.ports

    runtime.stop()
    runtime.stop()

    assert not runtime.started
    assert not any(component.running for component in components(runtime))
    assert [name for name in LAB_PORTS if not can_bind(getattr(ports, name))] == []


def test_context_manager_starts_and_stops() -> None:
    runtime = new_lab()

    with runtime as entered:
        assert entered is runtime
        assert runtime.started
        assert all(component.running for component in components(runtime))

    assert not runtime.started


def test_shutdown_leaves_no_thread_and_no_listener() -> None:
    before = set(threading.enumerate())

    with new_lab() as runtime:   # une session complète : appels, flux, appels asynchrones, sondes d'état
        ports = runtime.ports
        for protocol, via_proxy in ROUTES:
            with runtime.client(protocol, via_proxy=via_proxy) as client:
                assert client.calculate_factorial(5)["result"] == "120"
                assert len(list(client.stream_analytics(samples=2, interval_ms=0))) == 2
                assert client.submit("calculate_factorial", {"n": 6}).result(timeout=5)["result"] == "720"
        for protocol in CONTRACT_PROTOCOLS:
            with runtime.client_v2(protocol) as client:
                client.calculate_factorial(5)
        runtime.status()

    def survivors() -> list[str]:
        return [thread.name for thread in threading.enumerate() if thread not in before]

    # Les pools de threads (workers du serveur, appels asynchrones des clients) se vident d'eux-mêmes
    # dès leur arrêt demandé : on leur laisse un instant, mais aucun ne doit rester.
    wait_until(lambda: not survivors())
    assert survivors() == []
    assert [name for name in LAB_PORTS if not can_bind(getattr(ports, name))] == []


def test_stop_goes_to_the_end_even_if_a_component_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = new_lab().start()
    faulty = runtime.proxies["grpc"]
    genuine_stop = faulty.stop

    def failing_stop() -> None:
        genuine_stop()
        raise OSError("arrêt défaillant")

    monkeypatch.setattr(faulty, "stop", failing_stop)

    with pytest.raises(OSError, match="arrêt défaillant"):
        runtime.stop()

    # L'erreur est remontée, mais les composants arrêtés après le fautif l'ont bien été.
    assert not runtime.started
    assert not any(component.running for component in components(runtime))


def test_restart_keeps_ports_and_settings() -> None:
    runtime = new_lab().start()
    ports = runtime.ports
    runtime.conditions.apply_preset("lan")
    runtime.grpc_v2.strict = True
    runtime.service.update_stock(PRODUCT, -4)
    runtime.stop()

    with runtime:
        assert runtime.ports == ports
        assert runtime.conditions.snapshot()["preset"] == "lan"
        assert runtime.grpc_v2.strict
        for protocol in REMOTE_PROTOCOLS:
            with runtime.client(protocol, via_proxy=True) as client:
                assert client.get_product_details(PRODUCT)["stock"] == INITIAL_STOCK - 4


@pytest.mark.parametrize("name", ["custom", "grpc", "rest", "grpc_proxy", "custom_v2", "grpc_v2"])
def test_busy_port_aborts_the_start_with_a_clear_message(name: str) -> None:
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind((HOST, 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    runtime = LabRuntime(dataclasses.replace(UNSET_PORTS, **{name: port}), bus=EventBus())
    try:
        with pytest.raises(RuntimeError) as failure:
            runtime.start()

        message = str(failure.value)
        assert f"{HOST}:{port}" in message
        assert f"Le port {port} est déjà utilisé" in message
        assert "RPCX_PORT_OFFSET" in message
        assert isinstance(failure.value.__cause__, OSError)
        # Tout ou rien : les composants démarrés avant l'échec ont été arrêtés.
        assert not runtime.started
        assert not any(component.running for component in components(runtime))
    finally:
        squatter.close()

    with runtime:   # le port est libéré : le même laboratoire démarre
        assert getattr(runtime.ports, name) == port
        assert all(component.running for component in components(runtime))


def test_two_labs_run_side_by_side() -> None:
    with new_lab() as first, new_lab() as second:
        used = [getattr(runtime.ports, name) for runtime in (first, second) for name in LAB_PORTS]
        assert len(set(used)) == 2 * len(LAB_PORTS)

        with first.client("grpc", via_proxy=True) as one, second.client("grpc", via_proxy=True) as two:
            one.update_stock(PRODUCT, -9)
            assert two.get_product_details(PRODUCT)["stock"] == INITIAL_STOCK   # inventaires indépendants

            first.conditions.apply_preset("outage")                             # réseaux indépendants
            with pytest.raises(RpcTransportError):
                one.calculate_factorial(5)
            assert two.calculate_factorial(5)["result"] == "120"

        # Bus privés : chaque collecteur ne voit que les appels de son laboratoire.
        assert (first.collector.totals["grpc"]["calls"], first.collector.totals["grpc"]["errors"]) == (2, 1)
        assert (second.collector.totals["grpc"]["calls"], second.collector.totals["grpc"]["errors"]) == (2, 0)


# --- Clients -----------------------------------------------------------------

@pytest.mark.parametrize(("protocol", "via_proxy"), ROUTES, ids=ROUTE_IDS)
def test_every_route_returns_the_same_product(lab: LabRuntime, protocol: str, via_proxy: bool) -> None:
    expected = lab.service.get_product_details(PRODUCT)

    with lab.client(protocol, via_proxy=via_proxy) as client:
        assert isinstance(client, InventoryClient)
        assert client.protocol == protocol
        assert client.get_product_details(PRODUCT) == expected

    # Seul le proxy du protocole demandé a vu passer une connexion (aucun pour « local » ou en direct).
    crossed = [name for name, proxy in lab.proxies.items() if proxy.stats()["connections_total"]]
    assert crossed == ([protocol] if via_proxy and protocol != "local" else [])


@pytest.mark.parametrize(("protocol", "via_proxy"), ROUTES, ids=ROUTE_IDS)
def test_every_route_reports_the_same_error(lab: LabRuntime, protocol: str, via_proxy: bool) -> None:
    with lab.client(protocol, via_proxy=via_proxy) as client:
        with pytest.raises(NotFoundError) as failure:
            client.get_product_details(UNKNOWN_PRODUCT)

    assert failure.value.message == f"Produit inconnu : {UNKNOWN_PRODUCT}"
    assert failure.value.protocol == protocol
    assert failure.value.detail["product_id"] == UNKNOWN_PRODUCT


def test_muted_bus_silences_every_component(lab: LabRuntime) -> None:
    expected = lab.service.get_product_details(PRODUCT)
    events: list[TraceEvent] = []

    with lab.bus.muted():   # ce que font les benchmarks : aucune étape ne doit être publiée, nulle part
        unsubscribe = lab.bus.subscribe(events.append)
        for protocol, via_proxy in ROUTES:
            with lab.client(protocol, via_proxy=via_proxy) as client:
                assert client.get_product_details(PRODUCT) == expected
        unsubscribe()

    assert events == []
    assert lab.collector.recent() == []


@pytest.mark.parametrize("protocol", REMOTE_PROTOCOLS)
def test_client_timeout_bounds_every_call(lab: LabRuntime, protocol: str) -> None:
    with lab.client(protocol, via_proxy=True, timeout=0.25) as client:
        client.calculate_factorial(5)   # connexion établie avant que le réseau n'avale tout
        lab.conditions.apply_preset("blackhole")

        started = time.perf_counter()
        with pytest.raises(RpcTimeoutError):
            client.calculate_factorial(5)
        waited = time.perf_counter() - started

    assert 0.2 <= waited < 2.0   # le délai du client (0,25 s), pas celui par défaut (5 s)


def test_endpoint_gives_direct_and_proxied_addresses(lab: LabRuntime) -> None:
    assert lab.endpoint("grpc") == (HOST, lab.ports.grpc)
    assert lab.endpoint("grpc", via_proxy=True) == (HOST, lab.ports.grpc_proxy)


def test_unknown_protocols_are_rejected(lab: LabRuntime) -> None:
    with pytest.raises(ValueError, match="soap"):
        lab.client("soap")
    with pytest.raises(ValueError, match="rest"):
        lab.client_v2("rest")   # pas de serveur REST « contrat v2 »
    with pytest.raises(ValueError):
        lab.client_v2("local")
    with pytest.raises(ValueError):
        lab.endpoint("local")   # un appel local n'a pas d'adresse réseau
    with pytest.raises(ValueError, match="local"):
        lab.arm("reset", "local")
    with pytest.raises(ValueError, match="meteor"):
        lab.arm("meteor", "custom")


def test_remote_clients_need_resolved_ports() -> None:
    runtime = new_lab()   # ports éphémères, pas encore attribués

    with pytest.raises(RuntimeError, match="start"):
        runtime.client("custom")
    with pytest.raises(RuntimeError, match="start"):
        runtime.client_v2("grpc")
    with runtime.client("local") as client:   # aucun réseau : utilisable sans démarrer
        assert client.calculate_factorial(4)["result"] == "24"


@pytest.mark.parametrize("protocol", REMOTE_PROTOCOLS)
def test_client_and_server_traces_meet_in_the_collector(lab: LabRuntime, protocol: str) -> None:
    with lab.client(protocol, via_proxy=True) as client:
        client.calculate_factorial(6)
        call_id = client.last_call_id

    trace = lab.collector.get(call_id)
    assert trace is not None
    assert trace.protocol == protocol
    # Stub et squelette publient sous le même identifiant : la trace raconte l'appel entier, dans l'ordre.
    assert [event.stage for event in trace.events] == list(PIPELINE)
    assert trace.summary()["status"] == "ok"
    assert all(event.call_id != call_id for event in BUS.recent(4000))   # rien n'a fui vers le bus global


# --- Réseau simulé -----------------------------------------------------------

def test_three_proxies_share_one_network(lab: LabRuntime) -> None:
    assert list(lab.proxies) == list(REMOTE_PROTOCOLS)
    assert all(proxy.conditions is lab.conditions for proxy in lab.proxies.values())
    assert [proxy.name for proxy in lab.proxies.values()] == list(REMOTE_PROTOCOLS)


@pytest.mark.parametrize("protocol", REMOTE_PROTOCOLS)
def test_latency_applies_to_every_protocol(lab: LabRuntime, protocol: str) -> None:
    def call(client: InventoryClient) -> float:
        return elapsed_ms(lambda: client.calculate_factorial(5))

    with lab.client(protocol) as direct, lab.client(protocol, via_proxy=True) as proxied:
        for client in (direct, proxied):   # connexions établies hors mesure
            call(client)

        lab.conditions.update(latency_ms=LATENCY_MS)
        delayed = call(proxied)
        untouched = min(call(direct) for _ in range(3))

        lab.conditions.reset()
        recovered = min(call(proxied) for _ in range(3))

    assert delayed >= LATENCY_MS * 0.95      # l'aller-retour entier est retardé
    assert untouched < LATENCY_MS / 2        # l'accès direct ignore le réseau simulé
    assert recovered < LATENCY_MS / 2        # retour à la normale sans rien redémarrer


def test_arm_cuts_the_next_call_of_one_protocol_only(lab: LabRuntime) -> None:
    with lab.client("rest", via_proxy=True) as rest, lab.client("custom", via_proxy=True) as custom:
        lab.arm("reset", "rest")
        assert {name: proxy.armed()["reset"] for name, proxy in lab.proxies.items()} == {
            "custom": 0, "grpc": 0, "rest": 1,
        }

        assert custom.update_stock(PRODUCT, -1)["new_stock"] == INITIAL_STOCK - 1   # autre protocole : épargné
        with pytest.raises(RpcTransportError):
            rest.update_stock(PRODUCT, -1)
        # La requête coupée n'a jamais atteint le serveur ; la panne consommée, l'appel suivant passe.
        assert lab.service.get_product_details(PRODUCT)["stock"] == INITIAL_STOCK - 1
        assert rest.update_stock(PRODUCT, -1)["new_stock"] == INITIAL_STOCK - 2

    assert lab.proxies["rest"].stats()["resets"] == 1
    assert lab.proxies["custom"].stats()["resets"] == 0


@pytest.mark.parametrize("protocol", REMOTE_PROTOCOLS)
def test_lost_reply_hides_an_executed_call(lab: LabRuntime, protocol: str) -> None:
    with lab.client(protocol, via_proxy=True) as client:
        lab.arm("lost_reply", protocol)

        with pytest.raises(RpcTransportError):
            client.update_stock(PRODUCT, -2)

    # Le piège du RPC : l'appelant voit un échec, alors que le serveur a bel et bien exécuté l'appel.
    assert lab.service.get_product_details(PRODUCT)["stock"] == INITIAL_STOCK - 2
    assert lab.proxies[protocol].stats()["lost_replies"] == 1


def test_network_events_are_published_on_the_lab_bus(lab: LabRuntime) -> None:
    events: list[TraceEvent] = []
    unsubscribe = lab.bus.subscribe(events.append)
    try:
        with lab.client("grpc", via_proxy=True) as client:
            lab.conditions.update(latency_ms=20)
            client.calculate_factorial(5)
    finally:
        unsubscribe()

    delays = [event for event in events if event.stage == "network.delay"]
    assert delays
    assert {(event.side, event.protocol) for event in delays} == {("network", "grpc")}


# --- Contrat v2 --------------------------------------------------------------

def test_custom_v2_breaks_a_v1_client_at_run_time(lab: LabRuntime) -> None:
    with lab.client_v2("custom") as client:
        assert client.protocol == "custom"

        with pytest.raises(MethodNotFoundError) as renamed:       # get_product_details → get_product
            client.get_product_details(PRODUCT)
        with pytest.raises(InvalidArgumentError) as incomplete:   # « warehouse » devenu obligatoire
            client.update_stock(PRODUCT, -1)
        product = client.list_products(limit=1)["products"][0]    # clés renommées dans le résultat
        factorial = client.calculate_factorial(5)                 # clé ajoutée : compatible

    assert renamed.value.detail["jsonrpc_code"] == -32601
    assert incomplete.value.detail["jsonrpc_code"] == -32602
    assert "stock" not in product and "price" not in product
    assert (product["quantity"], product["price_cents"]) == (INITIAL_STOCK, 12990)
    assert factorial["result"] == "120" and "algorithm" in factorial


def test_grpc_v2_breaks_a_v1_client_silently_or_loudly(lab: LabRuntime) -> None:
    with lab.client_v2("grpc") as client:
        assert client.protocol == "grpc"

        with pytest.raises(MethodNotFoundError) as renamed:   # GetProductDetails → GetProduct
            client.get_product_details(PRODUCT)
        silent = client.update_stock(PRODUCT, -3)             # « delta » lu à un autre numéro de champ : 0
        lab.grpc_v2.strict = True
        with pytest.raises(InvalidArgumentError):             # la validation stricte rend la rupture visible
            client.update_stock(PRODUCT, -3)

    assert renamed.value.code == METHOD_NOT_FOUND
    assert renamed.value.detail["grpc_status"] == "UNIMPLEMENTED"
    assert (silent["delta"], silent["previous_stock"], silent["new_stock"]) == (0, INITIAL_STOCK, INITIAL_STOCK)
    assert lab.service_v2.get_product_details(PRODUCT)["stock"] == INITIAL_STOCK


def test_v2_servers_work_on_a_separate_inventory(lab: LabRuntime) -> None:
    with lab.client("custom") as v1, lab.client_v2("custom") as v2:
        v1.update_stock(PRODUCT, -10)
        assert v2.list_products(limit=1)["products"][0]["quantity"] == INITIAL_STOCK

    assert lab.service is not lab.service_v2
    assert lab.service.stats()["calls"] == {"update_stock": 1}
    assert lab.service_v2.stats()["calls"] == {"list_products": 1}


# --- État --------------------------------------------------------------------

def test_status_describes_the_whole_lab(lab: LabRuntime) -> None:
    status = lab.status()

    assert json.loads(json.dumps(status)) == status   # sérialisable tel quel par le dashboard
    assert list(status) == ["app", "servers", "network", "proxies", "totals", "inventory"]
    assert (status["app"]["name"], status["app"]["version"]) == (APP_NAME, VERSION)
    assert [server["id"] for server in status["servers"]] == SERVER_IDS
    for server in status["servers"]:
        assert set(server) == {"id", "label", "transport", "host", "port", "proxy_port", "up"}
        assert server["host"] == HOST
        assert server["port"] == getattr(lab.ports, server["id"])
        assert server["label"] and server["transport"]
        assert server["up"] is True
    assert {server["id"]: server["proxy_port"] for server in status["servers"]} == {
        "custom": lab.ports.custom_proxy,
        "grpc": lab.ports.grpc_proxy,
        "rest": lab.ports.rest_proxy,
        "custom_v2": None,
        "grpc_v2": None,
    }
    assert len({server["label"] for server in status["servers"]}) == len(SERVER_IDS)
    assert status["network"] == NetworkConditions().snapshot()
    assert list(status["proxies"]) == list(REMOTE_PROTOCOLS)
    for name, stats in status["proxies"].items():
        assert stats.keys() == lab.proxies[name].stats().keys()
        assert stats["connections_total"] == 0
    assert status["totals"] == dict.fromkeys(
        PROTOCOLS, {"calls": 0, "errors": 0, "bytes_out": 0, "bytes_in": 0, "avg_ms": 0.0}
    )
    assert status["inventory"] == lab.service.stats()


def test_status_uptime_grows_while_started(lab: LabRuntime) -> None:
    first = lab.status()["app"]["uptime_s"]
    time.sleep(0.02)

    assert lab.status()["app"]["uptime_s"] > first >= 0


def test_status_totals_follow_the_collected_calls(lab: LabRuntime) -> None:
    for protocol in PROTOCOLS:
        with lab.client(protocol) as client:
            client.calculate_factorial(5)
            client.calculate_factorial(6)
            with pytest.raises(NotFoundError):
                client.get_product_details(UNKNOWN_PRODUCT)

    totals = lab.status()["totals"]

    for protocol in PROTOCOLS:
        recorded = lab.collector.totals[protocol]
        assert (totals[protocol]["calls"], totals[protocol]["errors"]) == (3, 1)
        assert totals[protocol]["avg_ms"] == round(recorded["total_us"] / 3 / 1000, 3) > 0
    for protocol in REMOTE_PROTOCOLS:
        assert totals[protocol]["bytes_out"] > 0 and totals[protocol]["bytes_in"] > 0
    assert (totals["local"]["bytes_out"], totals["local"]["bytes_in"]) == (0, 0)   # aucun octet : pas de réseau


def test_status_probes_each_server_at_most_once_per_second(monkeypatch: pytest.MonkeyPatch) -> None:
    probes: list[tuple[int, float]] = []
    connect = socket.create_connection

    def recording(address: tuple[str, int], timeout: float) -> socket.socket:
        probes.append((address[1], timeout))
        return connect(address, timeout=timeout)

    def refusing(address: tuple[str, int], timeout: float) -> socket.socket:
        raise ConnectionRefusedError(f"port {address[1]} fermé")

    with new_lab() as runtime:
        monkeypatch.setattr(socket, "create_connection", recording)
        server_ports = sorted(server.port for server in runtime.servers.values())

        assert all(server["up"] for server in runtime.status()["servers"])
        runtime.status()
        # Une sonde par serveur, sur son port direct (jamais celui d'un proxy), puis le résultat est réutilisé.
        assert sorted(port for port, _ in probes) == server_ports
        assert {timeout for _, timeout in probes} == {0.15}

        monkeypatch.setattr("lab.runtime._PROBE_TTL_S", 0.0)   # sondes périmées : elles sont refaites
        runtime.status()
        assert len(probes) == 2 * len(server_ports)

        monkeypatch.setattr(socket, "create_connection", refusing)
        assert not any(server["up"] for server in runtime.status()["servers"])


def test_status_reports_a_server_that_went_down() -> None:
    with new_lab() as runtime:
        runtime.servers["rest"].stop()

        up = {server["id"]: server["up"] for server in runtime.status()["servers"]}

    assert up == {"custom": True, "grpc": True, "rest": False, "custom_v2": True, "grpc_v2": True}


def test_status_of_a_stopped_lab_needs_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = new_lab().start()
    ports = runtime.ports
    runtime.stop()
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("sonde inattendue"))

    status = runtime.status()

    assert json.loads(json.dumps(status)) == status
    assert status["app"]["uptime_s"] == 0.0
    assert [server["up"] for server in status["servers"]] == [False] * len(SERVER_IDS)
    assert [server["port"] for server in status["servers"]] == [getattr(ports, name) for name in SERVER_IDS]


# --- Remise à zéro -----------------------------------------------------------

def test_reset_restores_the_initial_state(lab: LabRuntime) -> None:
    with lab.client("custom", via_proxy=True) as v1, lab.client_v2("grpc") as v2:
        v1.update_stock(PRODUCT, -5, idempotency_key="commande-1")
        v2.calculate_factorial(3)
    lab.service_v2.update_stock(PRODUCT, 7)
    lab.conditions.apply_preset("wan")
    lab.arm("lost_reply", "grpc", 2)
    lab.grpc_v2.strict = True
    assert lab.collector.recent()
    assert lab.proxies["custom"].stats()["bytes_up"] > 0

    lab.reset()

    pristine = InventoryService().stats()
    assert lab.started   # rien n'a été redémarré
    assert lab.service.stats() == pristine
    assert lab.service_v2.stats() == pristine
    assert lab.conditions.snapshot() == NetworkConditions().snapshot()
    assert all(proxy.armed() == {"reset": 0, "lost_reply": 0} for proxy in lab.proxies.values())
    assert lab.grpc_v2.strict is False
    assert lab.collector.recent() == []
    assert all(counters["calls"] == 0 for counters in lab.status()["totals"].values())
    stats = lab.proxies["custom"].stats()
    assert (stats["connections_total"], stats["bytes_up"], stats["bytes_down"]) == (0, 0, 0)
    with lab.client("rest") as client:   # la clé d'idempotence est oubliée, elle aussi
        assert client.update_stock(PRODUCT, -5, idempotency_key="commande-1")["applied"] is True
