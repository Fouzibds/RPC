"""Laboratoire de pannes : forme des résultats, mesures de chaque scénario, remise en état.

Tous les tests partagent un laboratoire sur ports éphémères et bus privé. Les cinq scénarios y
sont joués une fois avec leurs réglages par défaut (JSON-RPC maison) ; les autres exécutions
raccourcissent latences et délais pour que le fichier reste rapide.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator

import pytest

from benchmark_lab.failure_simulation import (
    DEFAULT_OPTIONS,
    METRIC_INFO,
    SCENARIOS,
    STEP_KINDS,
    STEP_STATUSES,
    main,
    run_all,
    run_scenario,
)
from common.client_api import InventoryClient
from common.config import PROTOCOL_LABELS, REMOTE_PROTOCOLS
from common.errors import RpcTimeoutError
from common.inventory import InventoryService
from common.telemetry import EventBus
from lab import LabRuntime

SCENARIO_IDS = ["latency_trap", "timeout_spike", "connection_cut", "server_outage", "duplicate_execution"]
OTHER_PROTOCOLS = ["grpc", "rest"]
# Réglages raccourcis : mêmes phénomènes, quelques dixièmes de seconde au lieu de plusieurs secondes.
FAST: dict[str, dict[str, float]] = {
    "latency_trap": {"calls": 4, "latency_ms": 60},
    "timeout_spike": {"spike_ms": 500, "deadline_ms": 120},
    "server_outage": {"reset_timeout_ms": 300},
}
IDEAL_NETWORK = "ideal"
NO_ARMED_FAULT = {"reset": 0, "lost_reply": 0}
RESULT_KEYS = {"id", "title", "protocol", "summary", "verdict", "lesson", "metrics", "steps", "options", "duration_ms"}
STEP_KEYS = {"t_ms", "kind", "label", "status", "detail"}
MAX_SCENARIO_MS = 6000


# --- Outils --------------------------------------------------------------------------

@dataclass
class Campaign:
    """Les cinq scénarios joués d'une traite, et les étapes reçues en direct pendant ce temps."""

    results: dict[str, dict[str, Any]]
    observed: list[dict[str, Any]]


class Interrupted(Exception):
    """Levée au beau milieu d'un scénario, pour vérifier qu'il remet tout en état malgré tout."""


def stocks(service: InventoryService) -> dict[str, int]:
    return {product_id: service.check_stock(product_id)["stock"] for product_id in service.product_ids()}


INITIAL_STOCKS = stocks(InventoryService())


def assert_lab_untouched(runtime: LabRuntime) -> None:
    """Réseau idéal, aucune panne en attente, stocks du catalogue initial."""
    assert runtime.conditions.snapshot()["preset"] == IDEAL_NETWORK
    assert all(proxy.armed() == NO_ARMED_FAULT for proxy in runtime.proxies.values())
    assert stocks(runtime.service) == INITIAL_STOCKS


def steps_of(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [step for step in result["steps"] if step["kind"] == kind]


def statuses_of(result: dict[str, Any], kind: str) -> list[str]:
    return [step["status"] for step in steps_of(result, kind)]


@pytest.fixture(scope="module")
def lab() -> Iterator[LabRuntime]:
    with LabRuntime.ephemeral(bus=EventBus()) as runtime:
        yield runtime


@pytest.fixture(scope="module")
def campaign(lab: LabRuntime) -> Campaign:
    observed: list[dict[str, Any]] = []
    results = run_all(lab, protocol="custom", on_step=observed.append)
    return Campaign({result["id"]: result for result in results}, observed)


# --- Métadonnées ---------------------------------------------------------------------

def test_metadata_lists_the_five_scenarios_in_order() -> None:
    assert [scenario["id"] for scenario in SCENARIOS] == SCENARIO_IDS
    for scenario in SCENARIOS:
        assert set(scenario) == {"id", "title", "icon", "summary", "concept", "duration_hint_s", "protocols"}
        for name in ("title", "icon", "summary", "concept"):
            assert isinstance(scenario[name], str) and scenario[name].strip()
        assert 0 < scenario["duration_hint_s"] <= MAX_SCENARIO_MS / 1000
        assert scenario["protocols"] == list(REMOTE_PROTOCOLS)
    assert SCENARIOS[0]["title"] == "Le piège de la boucle innocente"
    json.dumps(SCENARIOS)


def test_options_and_metrics_are_documented() -> None:
    assert list(DEFAULT_OPTIONS) == SCENARIO_IDS
    assert DEFAULT_OPTIONS["latency_trap"] == {"calls": 12, "latency_ms": 200}
    assert DEFAULT_OPTIONS["timeout_spike"] == {"spike_ms": 1500, "deadline_ms": 300}
    for info in METRIC_INFO.values():
        assert set(info) == {"label", "unit"} and info["label"].strip()


# --- Forme des résultats -------------------------------------------------------------

def test_run_all_plays_every_scenario_in_order(campaign: Campaign) -> None:
    assert list(campaign.results) == SCENARIO_IDS

    # Dans le flux remis à on_step, chaque scénario s'ouvre par une étape-repère qui le nomme.
    openings = [step["detail"]["scenario"] for step in campaign.observed if "scenario" in step["detail"]]
    assert openings == SCENARIO_IDS
    assert campaign.observed == [step for result in campaign.results.values() for step in result["steps"]]


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_result_follows_the_contract(campaign: Campaign, scenario_id: str) -> None:
    result = campaign.results[scenario_id]
    metadata = next(scenario for scenario in SCENARIOS if scenario["id"] == scenario_id)

    assert set(result) == RESULT_KEYS
    assert (result["id"], result["protocol"]) == (scenario_id, "custom")
    assert (result["title"], result["summary"]) == (metadata["title"], metadata["summary"])
    assert result["options"] == DEFAULT_OPTIONS[scenario_id]
    assert result["duration_ms"] < MAX_SCENARIO_MS
    assert result["duration_ms"] < 2 * metadata["duration_hint_s"] * 1000     # la durée annoncée est réaliste
    json.dumps(result)

    # Verdict : une phrase, rédigée avec les nombres mesurés ; leçon : la règle générale.
    assert result["verdict"].endswith(".") and "\n" not in result["verdict"]
    assert any(character.isdigit() for character in result["verdict"])
    assert result["lesson"].endswith(".")

    assert result["metrics"] and set(result["metrics"]) <= set(METRIC_INFO)

    steps = result["steps"]
    assert steps[0]["detail"] == {"scenario": scenario_id, "protocol": "custom"}
    assert PROTOCOL_LABELS["custom"] in steps[0]["label"]
    for step in steps:
        assert set(step) == STEP_KEYS
        assert step["kind"] in STEP_KINDS and step["status"] in STEP_STATUSES
        assert step["label"].strip() and isinstance(step["detail"], dict)
    instants = [step["t_ms"] for step in steps]
    assert instants == sorted(instants) and instants[0] >= 0
    assert instants[-1] <= result["duration_ms"]


# --- Les cinq scénarios, réglages par défaut, JSON-RPC maison ---------------------------

def test_latency_trap_measures_the_cost_of_a_chatty_loop(campaign: Campaign) -> None:
    result = campaign.results["latency_trap"]
    metrics = result["metrics"]

    assert metrics["calls"] == 12 and metrics["latency_ms"] == 200
    # Douze allers-retours à 200 ms : la boucle distante ne peut pas descendre sous 2,4 s.
    assert 12 * 200 * 0.95 <= metrics["rpc_total_ms"] < 12 * 200 * 1.3
    assert metrics["per_call_ms"] == pytest.approx(metrics["rpc_total_ms"] / 12, abs=0.01)
    assert metrics["local_total_ms"] < 50
    assert metrics["slowdown_x"] > 20
    # Le remède : un seul aller-retour pour les douze fiches.
    assert metrics["batched_products"] == 12
    assert 200 * 0.95 <= metrics["batched_total_ms"] < 200 * 2
    assert metrics["batched_speedup_x"] > 5

    assert statuses_of(result, "local_call") == ["ok"] * 12
    assert statuses_of(result, "rpc_call") == ["slow"] * 12 + ["ok"]


def test_timeout_spike_deadline_frees_the_caller_but_not_the_server(campaign: Campaign) -> None:
    result = campaign.results["timeout_spike"]
    metrics = result["metrics"]
    spike_ms, deadline_ms = metrics["spike_ms"], metrics["deadline_ms"]

    assert (spike_ms, deadline_ms) == (1500, 300)
    assert spike_ms * 0.97 <= metrics["no_deadline_ms"] < spike_ms + 500      # bloqué pendant tout le pic
    assert deadline_ms * 0.97 <= metrics["with_deadline_ms"] < deadline_ms + 250     # l'échéance est tenue
    assert metrics["with_deadline_outcome"] == "TIMEOUT"
    assert metrics["time_saved_ms"] == pytest.approx(metrics["no_deadline_ms"] - metrics["with_deadline_ms"], abs=0.01)
    # Le stub maison garde sa connexion : la requête abandonnée finit par arriver, et le serveur l'exécute.
    assert metrics["server_executed"] is True
    assert metrics["stock_after"] == metrics["stock_before"] - 2

    assert statuses_of(result, "rpc_call") == ["slow", "timeout"]
    assert [step["detail"]["delay_ms"] for step in steps_of(result, "network") if step["status"] == "slow"] == [
        spike_ms, spike_ms,
    ]


def test_connection_cut_crashes_the_naive_client_and_spares_the_resilient_one(campaign: Campaign) -> None:
    result = campaign.results["connection_cut"]
    metrics = result["metrics"]

    assert metrics["naive_outcome"] == "UNAVAILABLE"
    assert metrics["naive_error"] == "RpcTransportError"     # la « NetworkError » du cahier des charges
    assert metrics["naive_ms"] < 500
    assert metrics["resilient_outcome"] == "OK"
    assert metrics["resilient_attempts"] == 2
    assert metrics["resilient_backoff_ms"] == 100
    assert metrics["resilient_total_ms"] >= metrics["resilient_backoff_ms"]

    assert statuses_of(result, "local_call") == ["ok"]
    assert statuses_of(result, "rpc_call") == ["error", "ok"]
    assert statuses_of(result, "attempt") == ["error", "ok"]
    assert statuses_of(result, "backoff") == ["retry"]
    cuts = [step for step in steps_of(result, "network") if step["status"] == "error"]
    assert [cut["detail"]["fault"] for cut in cuts] == ["reset", "reset"]


def test_server_outage_opens_the_breaker_then_recovers(campaign: Campaign) -> None:
    result = campaign.results["server_outage"]
    metrics = result["metrics"]

    assert metrics["failures_before_open"] == 3
    assert metrics["slow_fail_outcome"] == "UNAVAILABLE"
    assert metrics["slow_fail_ms"] >= 300      # deux attentes (100 puis 200 ms) entre les trois tentatives
    assert metrics["fast_fail_outcome"] == "CIRCUIT_OPEN"
    assert metrics["fast_fail_calls"] == 3
    assert metrics["fast_fail_ms"] < 5 and metrics["fast_fail_ms"] < metrics["slow_fail_ms"] / 20
    assert metrics["fast_fail_speedup_x"] > 20
    assert metrics["breaker_opened"] is True
    assert metrics["recovered"] is True and metrics["recovery_probes"] == 1
    assert metrics["breaker_state"] == "closed"

    # Ouvert, puis semi-ouvert (appel d'essai), puis refermé.
    assert [step["detail"]["to"] for step in steps_of(result, "breaker")] == ["open", "half_open", "closed"]
    assert statuses_of(result, "breaker") == ["open", "info", "ok"]
    assert statuses_of(result, "rpc_call") == ["ok", "error", "open", "open", "open", "ok"]
    refused = [step for step in steps_of(result, "rpc_call") if step["status"] == "open"]
    assert all(step["detail"]["attempts"] == 0 for step in refused)     # aucun appel n'est parti sur le réseau


def test_duplicate_execution_debits_twice_without_an_idempotency_key(campaign: Campaign) -> None:
    result = campaign.results["duplicate_execution"]
    metrics = result["metrics"]
    stock_before = metrics["stock_before"]

    assert stock_before == INITIAL_STOCKS["SKU-1018"]
    assert metrics["expected_after"] == stock_before - 1
    assert metrics["naive_after"] == stock_before - 2
    assert (metrics["naive_executions"], metrics["naive_attempts"]) == (2, 2)
    assert metrics["idempotent_after"] == stock_before - 1
    assert (metrics["idempotent_executions"], metrics["idempotent_attempts"]) == (1, 2)
    assert metrics["deduplicated"] is True

    assert statuses_of(result, "rpc_call") == ["ok", "dedup"]
    lost = [step for step in steps_of(result, "network") if step["detail"].get("fault") == "lost_reply"]
    assert len(lost) == 2
    truths = [step for step in steps_of(result, "state") if "executions" in step["detail"]]
    assert [(truth["status"], truth["detail"]["executions"]) for truth in truths] == [("error", 2), ("ok", 1)]


# --- gRPC et REST subissent les mêmes pannes ---------------------------------------------

@pytest.mark.parametrize("protocol", OTHER_PROTOCOLS)
def test_latency_trap_over_other_middlewares(lab: LabRuntime, protocol: str) -> None:
    result = run_scenario(lab, "latency_trap", protocol=protocol, options=FAST["latency_trap"])
    metrics = result["metrics"]

    assert result["protocol"] == protocol and result["options"] == FAST["latency_trap"]
    assert metrics["calls"] == 4 and metrics["batched_products"] == 4
    assert metrics["rpc_total_ms"] >= 4 * 60 * 0.95
    assert metrics["slowdown_x"] > 20
    assert metrics["batched_total_ms"] < metrics["rpc_total_ms"] / 2
    assert_lab_untouched(lab)


@pytest.mark.parametrize("protocol", OTHER_PROTOCOLS)
def test_connection_cut_over_other_middlewares(lab: LabRuntime, protocol: str) -> None:
    result = run_scenario(lab, "connection_cut", protocol=protocol)
    metrics = result["metrics"]

    assert metrics["naive_outcome"] == "UNAVAILABLE" and metrics["naive_error"] == "RpcTransportError"
    assert metrics["resilient_outcome"] == "OK"
    assert 2 <= metrics["resilient_attempts"] <= 4      # gRPC peut échouer une fois de plus en se reconnectant
    assert statuses_of(result, "rpc_call") == ["error", "ok"]
    assert statuses_of(result, "attempt")[-1] == "ok"
    assert_lab_untouched(lab)


@pytest.mark.parametrize("protocol", OTHER_PROTOCOLS)
def test_duplicate_execution_over_other_middlewares(lab: LabRuntime, protocol: str) -> None:
    result = run_scenario(lab, "duplicate_execution", protocol=protocol)
    metrics = result["metrics"]
    stock_before = metrics["stock_before"]

    assert metrics["naive_after"] == stock_before - 2 and metrics["naive_executions"] == 2
    assert metrics["idempotent_after"] == stock_before - 1 and metrics["idempotent_executions"] == 1
    assert metrics["expected_after"] == stock_before - 1
    assert metrics["deduplicated"] is True
    assert statuses_of(result, "rpc_call") == ["ok", "dedup"]
    assert_lab_untouched(lab)


@pytest.mark.parametrize("protocol", OTHER_PROTOCOLS)
def test_timeout_spike_over_other_middlewares(lab: LabRuntime, protocol: str) -> None:
    result = run_scenario(lab, "timeout_spike", protocol=protocol, options=FAST["timeout_spike"])
    metrics = result["metrics"]
    spike_ms, deadline_ms = metrics["spike_ms"], metrics["deadline_ms"]

    assert (spike_ms, deadline_ms) == (500, 120)
    assert metrics["no_deadline_ms"] >= spike_ms * 0.97
    assert deadline_ms * 0.97 <= metrics["with_deadline_ms"] < deadline_ms + 250
    assert metrics["with_deadline_outcome"] == "TIMEOUT"
    # L'issue est réellement inconnue : selon le middleware, l'appel abandonné est annulé en route ou aboutit
    # quand même. Seule la vérité terrain tranche — une exécution (l'appel patient) ou deux.
    executions = metrics["stock_before"] - metrics["stock_after"]
    assert executions == (2 if metrics["server_executed"] else 1)
    assert statuses_of(result, "rpc_call") == ["slow", "timeout"]
    assert_lab_untouched(lab)


@pytest.mark.parametrize("protocol", OTHER_PROTOCOLS)
def test_server_outage_over_other_middlewares(lab: LabRuntime, protocol: str) -> None:
    result = run_scenario(lab, "server_outage", protocol=protocol, options=FAST["server_outage"])
    metrics = result["metrics"]

    assert metrics["failures_before_open"] == 3 and metrics["breaker_opened"] is True
    assert metrics["slow_fail_outcome"] == "UNAVAILABLE"
    assert metrics["fast_fail_outcome"] == "CIRCUIT_OPEN"
    assert metrics["fast_fail_ms"] < metrics["slow_fail_ms"] / 20
    # gRPC se reconnecte par paliers : le premier appel d'essai peut échouer et rouvrir le disjoncteur.
    assert metrics["recovered"] is True and 1 <= metrics["recovery_probes"] <= 4
    assert metrics["breaker_state"] == "closed"
    transitions = [step["detail"]["to"] for step in steps_of(result, "breaker")]
    assert transitions[0] == "open" and transitions[-2:] == ["half_open", "closed"]
    assert_lab_untouched(lab)


# --- Observation en direct ---------------------------------------------------------------

def test_on_step_receives_every_step_while_the_scenario_runs(lab: LabRuntime) -> None:
    received: list[tuple[float, dict[str, Any]]] = []
    started = time.perf_counter()

    result = run_scenario(
        lab, "latency_trap", protocol="custom", options=FAST["latency_trap"],
        on_step=lambda step: received.append((time.perf_counter() - started, step)),
    )
    finished = time.perf_counter() - started

    assert [step for _, step in received] == result["steps"]
    instants = [step["t_ms"] for _, step in received]
    assert instants == sorted(instants)
    # En direct : les étapes locales sont remises avant même que la boucle distante ne commence à répondre.
    last_local = max(index for index, (_, step) in enumerate(received) if step["kind"] == "local_call")
    assert received[last_local][0] < finished / 2


def test_muted_bus_keeps_measurements_and_verdict(lab: LabRuntime) -> None:
    with lab.bus.muted():
        result = run_scenario(lab, "duplicate_execution", protocol="rest")

    # Bus coupé : les tentatives ne sont plus relayées dans la chronologie, mais la vérité terrain reste mesurée.
    assert steps_of(result, "attempt") == []
    metrics = result["metrics"]
    assert (metrics["naive_executions"], metrics["idempotent_executions"]) == (2, 1)
    assert (metrics["naive_attempts"], metrics["idempotent_attempts"]) == (2, 2)
    assert_lab_untouched(lab)


# --- Remise en état ----------------------------------------------------------------------

def test_campaign_leaves_the_lab_as_it_found_it(lab: LabRuntime, campaign: Campaign) -> None:
    assert len(campaign.results) == len(SCENARIO_IDS)
    assert_lab_untouched(lab)


def test_previous_network_conditions_are_restored(lab: LabRuntime) -> None:
    lab.conditions.apply_preset("wan")
    before = lab.conditions.snapshot()
    try:
        result = run_scenario(lab, "latency_trap", protocol="rest", options=FAST["latency_trap"])

        assert lab.conditions.snapshot() == before
        # Le scénario s'est joué sur SON réseau (60 ms), pas sur celui qu'il a trouvé (40 ms ± 8).
        assert result["metrics"]["per_call_ms"] >= 60 * 0.95
    finally:
        lab.conditions.reset()


def test_observer_error_interrupts_the_scenario_and_everything_is_restored(lab: LabRuntime) -> None:
    def fail_once_the_server_is_down(step: dict[str, Any]) -> None:
        if step["detail"].get("down") is True:
            raise Interrupted(step["label"])

    with pytest.raises(Interrupted, match="injoignable"):
        run_scenario(lab, "server_outage", protocol="custom", on_step=fail_once_the_server_is_down)

    assert lab.conditions.snapshot()["down"] is False
    assert_lab_untouched(lab)


def test_armed_fault_does_not_outlive_an_interrupted_scenario(
    lab: LabRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    arm = lab.arm

    def arm_then_fail(kind: str, protocol: str, count: int = 1) -> None:
        arm(kind, protocol, count)
        raise Interrupted("juste après l’armement, avant l’appel qui devait subir la panne")

    monkeypatch.setattr(lab, "arm", arm_then_fail)

    with pytest.raises(Interrupted):
        run_scenario(lab, "duplicate_execution", protocol="custom")

    # Sans désarmement, la panne frapperait le premier appel venu, bien après le scénario.
    assert lab.proxies["custom"].armed() == NO_ARMED_FAULT
    assert_lab_untouched(lab)


def test_request_still_in_flight_cannot_debit_the_stock_after_restoration(
    lab: LabRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_client = lab.client

    def interrupted_at_the_deadline(protocol: str, **settings: Any) -> InventoryClient:
        """Client dont l'appel abandonné sur échéance interrompt le scénario (comme le ferait un Ctrl+C)."""
        client = new_client(protocol, **settings)
        update_stock = client.update_stock

        def update_stock_or_interrupt(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return update_stock(*args, **kwargs)
            except RpcTimeoutError as error:
                raise Interrupted("échéance dépassée : la requête, elle, est toujours en route") from error

        client.update_stock = update_stock_or_interrupt
        return client

    monkeypatch.setattr(lab, "client", interrupted_at_the_deadline)

    with pytest.raises(Interrupted):
        run_scenario(lab, "timeout_spike", protocol="custom", options=FAST["timeout_spike"])

    assert_lab_untouched(lab)
    # La requête retenue par le pic atteint le serveur APRÈS l'interruption : la remise en état a dû l'attendre.
    time.sleep(FAST["timeout_spike"]["spike_ms"] / 1000)
    assert stocks(lab.service) == INITIAL_STOCKS


def test_concurrent_scenarios_are_played_one_after_the_other(lab: LabRuntime) -> None:
    """Le réseau simulé est commun : deux scénarios lancés ensemble ne doivent pas se dérégler l'un l'autre."""
    spans: dict[str, list[float]] = {"latency_trap": [], "duplicate_execution": []}
    results: dict[str, dict[str, Any]] = {}

    def play(scenario_id: str) -> None:
        results[scenario_id] = run_scenario(
            lab, scenario_id, protocol="rest", options=FAST.get(scenario_id),
            on_step=lambda step: spans[scenario_id].append(time.perf_counter()),
        )

    threads = [threading.Thread(target=play, args=(scenario_id,)) for scenario_id in spans]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    first, second = sorted(spans.values(), key=min)
    assert max(first) <= min(second)
    assert results["latency_trap"]["metrics"]["slowdown_x"] > 20
    assert results["duplicate_execution"]["metrics"]["naive_executions"] == 2
    assert_lab_untouched(lab)


# --- Demandes invalides ------------------------------------------------------------------

@pytest.mark.parametrize(
    ("scenario_id", "protocol", "options", "message"),
    [
        ("meteor_strike", "custom", None, "Scénario inconnu"),
        ("latency_trap", "local", None, "Protocole inconnu"),
        ("latency_trap", "soap", None, "Protocole inconnu"),
        ("latency_trap", "custom", {"speed": 3}, "Option inconnue"),
        ("connection_cut", "custom", {"latency_ms": 10}, "Option inconnue"),
        ("latency_trap", "custom", {"calls": 2.5}, "entier"),
        ("latency_trap", "custom", {"calls": True}, "entier"),
        ("latency_trap", "custom", {"latency_ms": "200"}, "nombre"),
        ("latency_trap", "custom", {"latency_ms": 0}, "compris entre"),
        ("timeout_spike", "custom", {"spike_ms": 400, "deadline_ms": 400}, "inférieur"),
    ],
)
def test_invalid_requests_are_rejected_before_anything_runs(
    lab: LabRuntime, scenario_id: str, protocol: str, options: dict[str, Any] | None, message: str
) -> None:
    observed: list[dict[str, Any]] = []

    with pytest.raises(ValueError, match=message):
        run_scenario(lab, scenario_id, protocol=protocol, options=options, on_step=observed.append)

    assert observed == []
    assert_lab_untouched(lab)


def test_run_all_rejects_options_for_an_unknown_scenario(lab: LabRuntime) -> None:
    with pytest.raises(ValueError, match="Scénario inconnu"):
        run_all(lab, options={"meteor_strike": {}})


def test_scenario_needs_a_started_lab() -> None:
    runtime = LabRuntime.ephemeral(bus=EventBus())

    with pytest.raises(RuntimeError, match="démarré"):
        run_scenario(runtime, "connection_cut")


# --- Ligne de commande -------------------------------------------------------------------

def test_command_line_plays_one_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--scenario", "connection_cut", "--protocol", "rest"]) == 0

    output = capsys.readouterr().out
    assert "La coupure en plein appel" in output
    assert PROTOCOL_LABELS["rest"] in output
    assert "Verdict" in output and "À retenir" in output
    assert "RpcTransportError" in output


def test_command_line_rejects_an_unknown_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--scenario", "meteor_strike"])

    assert exit_info.value.code == 2
    assert "Scénario inconnu" in capsys.readouterr().err
