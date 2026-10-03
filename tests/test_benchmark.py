"""Banc d'essai de performance : forme des rapports, cohérence des mesures, exports.

Tout tourne sur un laboratoire à ports éphémères et à bus privé, avec des boucles
minuscules : on vérifie que les mesures sont justes et que les calculs en
découlent, pas que la machine est rapide.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

from benchmark_lab import benchmark_perf as bench
from benchmark_lab import report as reports
from common import config
from common.catalog import method_spec
from common.config import PROTOCOL_LABELS, PROTOCOLS, REMOTE_PROTOCOLS
from common.inventory import InventoryService
from common.telemetry import BUS, EventBus
from lab import LabRuntime
from rpc_grpc import converters

PRODUCT = "SKU-1001"
SWEEP_LATENCY_MS = 40
TINY_CONFIG: dict[str, Any] = {
    "iterations": 40,
    "warmup": 5,
    "serialization_iterations": 30,
    "sweep_latencies_ms": [0, SWEEP_LATENCY_MS],
    "sweep_iterations": 3,
    "wire_calls": 5,
}
SIZE_KEYS = ("json_bytes", "json_rpc_bytes", "protobuf_bytes", "grpc_bytes", "rest_bytes")
STAT_ORDER = ("min_ms", "median_ms", "p90_ms", "p95_ms", "p99_ms", "max_ms")
RESULT_KEYS = {
    "protocol", "label", "count", "errors", "mean_ms", "median_ms", "p90_ms", "p95_ms", "p99_ms", "min_ms",
    "max_ms", "stdev_ms", "total_s", "rps", "overhead_vs_local_x", "histogram", "samples_ms", "aborted", "error",
}
NBSP = "\u00a0"

ProgressCall = tuple[str, float, str, "dict[str, Any] | None"]


# --- Outils ------------------------------------------------------------------

@dataclass(frozen=True)
class FullRun:
    """Un banc complet, exécuté une seule fois pour tout le module."""

    report: dict[str, Any]
    progress: list[ProgressCall]
    stock_before: int
    stock_after: int


def stock(runtime: LabRuntime) -> int:
    return runtime.service.check_stock(PRODUCT)["stock"]


def by_protocol(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {result["protocol"]: result for result in results}


def size_row(report: dict[str, Any], method: str, direction: str) -> dict[str, Any]:
    return next(
        row for row in report["payload"]["rows"] if row["method"] == method and row["direction"] == direction
    )


def fake_result(protocol: str, mean_ms: float) -> dict[str, Any]:
    """Résultat de latence synthétique : juste ce que lisent les faits marquants."""
    return {
        "protocol": protocol,
        "label": PROTOCOL_LABELS[protocol],
        "count": 100,
        "errors": 0,
        "mean_ms": mean_ms,
        "median_ms": mean_ms,
        "p99_ms": mean_ms * 2,
        "max_ms": mean_ms * 3,
        "rps": 1000 / mean_ms,
    }


def fake_latency(means: dict[str, float]) -> dict[str, Any]:
    return {
        "method": "get_product_details",
        "iterations": 100,
        "concurrency": 1,
        "via_proxy": False,
        "results": [fake_result(protocol, mean) for protocol, mean in means.items()],
    }


def highlight(items: list[dict[str, str]], identifier: str) -> dict[str, str]:
    return next(item for item in items if item["id"] == identifier)


@pytest.fixture(scope="module")
def lab() -> Iterator[LabRuntime]:
    with LabRuntime.ephemeral(bus=EventBus()) as runtime:
        yield runtime


@pytest.fixture(scope="module")
def full(lab: LabRuntime) -> FullRun:
    progress: list[ProgressCall] = []
    before = stock(lab)
    report = bench.run_full_benchmark(lab, TINY_CONFIG, lambda *call: progress.append(call))
    return FullRun(report, progress, before, stock(lab))


@pytest.fixture
def reports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Dossier des rapports redirigé : aucun test n'écrit dans le vrai ``reports/``."""
    folder = tmp_path / "reports"
    monkeypatch.setattr(config, "REPORTS_DIR", folder)
    return folder


# --- Rapport complet ---------------------------------------------------------

def test_full_report_has_every_section_and_is_plain_json(full: FullRun) -> None:
    report = full.report

    assert list(report) == [
        "id", "created_at", "config", "environment", "payload", "serialization", "latency", "network", "highlights",
    ]
    assert re.fullmatch(r"benchmark-\d{8}-\d{6}", report["id"])
    assert report["created_at"] == report["environment"]["timestamp"]
    assert all(report[suite] for suite in bench.SUITES)
    assert json.loads(json.dumps(report, allow_nan=False)) == report


def test_report_keeps_the_resolved_configuration(full: FullRun) -> None:
    settings = full.report["config"]

    assert settings == bench.resolve_config(TINY_CONFIG)
    assert settings["suites"] == list(bench.SUITES)
    assert settings["protocols"] == list(PROTOCOLS)
    assert settings["params"] == {"product_id": PRODUCT}
    assert settings["iterations"] == 40


def test_environment_describes_the_machine(full: FullRun) -> None:
    environment = full.report["environment"]

    assert set(environment) == {"python", "platform", "processor", "cpu_count", "grpcio", "protobuf", "timestamp"}
    assert environment["python"] == platform.python_version()
    assert environment["cpu_count"] >= 1
    assert environment["grpcio"] and environment["protobuf"]


def test_unselected_suites_are_left_empty(lab: LabRuntime) -> None:
    report = bench.run_full_benchmark(lab, {"suites": ["serialization"], "serialization_iterations": 9})

    assert report["config"]["suites"] == ["serialization"]
    assert report["serialization"]["rows"]
    assert report["payload"] is None and report["latency"] is None and report["network"] is None
    assert [item["id"] for item in report["highlights"]] == ["serialization_speed"]


def test_default_and_quick_configurations_are_valid() -> None:
    default, quick = bench.resolve_config(), bench.resolve_config(bench.QUICK_CONFIG)

    assert default == bench.resolve_config(bench.DEFAULT_CONFIG)
    assert default["params"] == bench.DEFAULT_PARAMS[default["method"]]
    assert default["suites"] is not bench.DEFAULT_CONFIG["suites"]
    assert quick["iterations"] < default["iterations"]
    assert len(quick["sweep_latencies_ms"]) < len(default["sweep_latencies_ms"])
    assert bench.resolve_config({"iterations": None})["iterations"] == default["iterations"]


# --- Tailles des messages ----------------------------------------------------

def test_payload_rows_cover_each_method_in_both_directions(full: FullRun) -> None:
    rows = full.report["payload"]["rows"]

    assert [(row["method"], row["direction"]) for row in rows] == [
        (method, direction) for method, _ in bench.PAYLOAD_CALLS for direction in ("request", "response")
    ]
    for row in rows:
        assert set(row) == {"method", "title", "direction", *SIZE_KEYS, "rest_body_bytes", "protobuf_vs_json_pct"}
        assert row["title"] == method_spec(row["method"]).title
        assert all(isinstance(row[key], int) and row[key] > 0 for key in SIZE_KEYS)


def test_protobuf_is_smaller_than_json_for_a_product(full: FullRun) -> None:
    row = size_row(full.report, "get_product_details", "response")

    assert row["protobuf_bytes"] < row["json_bytes"]
    assert row["grpc_bytes"] < row["json_rpc_bytes"] < row["rest_bytes"]
    expected = round((row["protobuf_bytes"] - row["json_bytes"]) / row["json_bytes"] * 100, 1)
    assert row["protobuf_vs_json_pct"] == expected < 0


def test_framed_sizes_add_each_protocol_envelope(full: FullRun) -> None:
    for row in full.report["payload"]["rows"]:
        assert row["json_rpc_bytes"] == row["json_bytes"] + 4      # longueur sur 4 octets
        assert row["grpc_bytes"] == row["protobuf_bytes"] + 5      # drapeau + longueur
        assert row["rest_bytes"] > row["rest_body_bytes"] >= 0     # ligne de départ et en-têtes HTTP
    # Un GET n'a pas de corps : tout l'appel REST tient dans l'URL et les en-têtes.
    assert size_row(full.report, "get_product_details", "request")["rest_body_bytes"] == 0
    assert size_row(full.report, "update_stock", "request")["rest_body_bytes"] > 0


def test_wire_bytes_are_those_counted_by_the_proxies(full: FullRun) -> None:
    wire = full.report["payload"]["wire"]
    rows = by_protocol(wire)
    request = size_row(full.report, bench.WIRE_METHOD, "request")
    response = size_row(full.report, bench.WIRE_METHOD, "response")

    assert [row["protocol"] for row in wire] == list(REMOTE_PROTOCOLS)
    for row in wire:
        assert row["label"] == PROTOCOL_LABELS[row["protocol"]]
        assert row["method"] == bench.WIRE_METHOD
        assert row["calls"] == TINY_CONFIG["wire_calls"]
        assert row["total_per_call"] == pytest.approx(row["bytes_up_per_call"] + row["bytes_down_per_call"])
    # Sockets nues et HTTP/1.1 : rien d'autre ne circule que ce que l'application a écrit…
    assert rows["custom"]["bytes_up_per_call"] == pytest.approx(request["json_rpc_bytes"], abs=3)
    assert rows["custom"]["bytes_down_per_call"] == pytest.approx(response["json_rpc_bytes"], abs=3)
    assert rows["rest"]["bytes_up_per_call"] == pytest.approx(request["rest_bytes"], abs=3)
    # …alors que HTTP/2 ajoute ses trames et ses en-têtes aux messages gRPC.
    assert rows["grpc"]["bytes_up_per_call"] > request["grpc_bytes"]
    assert rows["grpc"]["bytes_down_per_call"] > response["grpc_bytes"]


def test_payload_grows_with_the_list_size(full: FullRun) -> None:
    scaling = full.report["payload"]["scaling"]

    assert [row["items"] for row in scaling] == list(bench.SCALING_LIMITS)
    for key in SIZE_KEYS:
        sizes = [row[key] for row in scaling]
        assert sizes == sorted(set(sizes)), key
    for row in scaling:
        assert row["protobuf_bytes"] < row["json_bytes"]
        assert row["protobuf_vs_json_pct"] < 0


def test_payload_sizes_are_read_from_live_traces(lab: LabRuntime) -> None:
    with lab.bus.muted(), pytest.raises(RuntimeError, match="bus de télémétrie"):
        bench.measure_payload_sizes(lab)


def test_sizes_of_a_failing_call_are_left_empty(lab: LabRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench, "PAYLOAD_CALLS", (("get_product_details", {"product_id": "SKU-0000"}),))

    section = bench.measure_payload_sizes(lab, scaling_limits=(1,), wire_calls=2)

    assert [row["direction"] for row in section["rows"]] == ["request", "response"]
    for row in section["rows"]:
        assert all(row[key] is None for key in (*SIZE_KEYS, "rest_body_bytes", "protobuf_vs_json_pct"))
    # Un appel sans réponse exploitable ne prive pas la section de ses autres mesures.
    assert all(row["total_per_call"] > 0 for row in section["wire"])
    assert section["scaling"][0]["protobuf_bytes"] > 0
    assert reports.MISSING in reports.to_text({"payload": section})
    assert "payload_size" not in [item["id"] for item in bench.build_highlights({"payload": section})]


def test_full_benchmark_leaves_the_inventory_untouched(full: FullRun) -> None:
    # update_stock fait partie des appels mesurés : son effet doit être annulé.
    assert full.stock_after == full.stock_before


# --- Sérialisation -----------------------------------------------------------

def test_serialization_compares_both_formats_on_the_same_data(full: FullRun) -> None:
    rows = full.report["serialization"]["rows"]
    product = InventoryService().get_product_details(PRODUCT)

    listed = bench.SERIALIZATION_LIST_SIZE
    assert [(row["items"], row["format"]) for row in rows] == [
        (1, "json"), (1, "protobuf"), (listed, "json"), (listed, "protobuf"),
    ]
    assert [row["message_type"] for row in rows] == ["Product", "Product", "ProductList", "ProductList"]
    assert rows[1]["size_bytes"] == len(converters.product_to_proto(product).SerializeToString())
    assert rows[1]["size_bytes"] < rows[0]["size_bytes"] < rows[3]["size_bytes"] < rows[2]["size_bytes"]
    for row in rows:
        timings = [row[key] for key in ("encode_ns", "decode_ns", "encode_from_dict_ns", "decode_to_dict_ns")]
        assert all(timing > 0 for timing in timings)
        assert row["iterations"] >= 1


def test_protobuf_pays_for_the_dict_conversion(full: FullRun) -> None:
    for row in full.report["serialization"]["rows"]:
        if row["format"] == "json":   # JSON part du dictionnaire : il n'y a rien à convertir
            assert row["encode_from_dict_ns"] == row["encode_ns"]
            assert row["decode_to_dict_ns"] == row["decode_ns"]
        else:
            assert row["encode_from_dict_ns"] > row["encode_ns"]
            assert row["decode_to_dict_ns"] > row["decode_ns"]


# --- Latence -----------------------------------------------------------------

def test_latency_section_describes_the_measure(full: FullRun) -> None:
    latency = full.report["latency"]

    assert set(latency) == {"method", "params", "iterations", "warmup", "concurrency", "via_proxy", "results"}
    assert (latency["method"], latency["params"]) == ("get_product_details", {"product_id": PRODUCT})
    assert (latency["iterations"], latency["warmup"], latency["concurrency"], latency["via_proxy"]) == (40, 5, 1, False)
    assert [result["protocol"] for result in latency["results"]] == list(PROTOCOLS)
    for result in latency["results"]:
        assert set(result) == RESULT_KEYS
        assert result["label"] == PROTOCOL_LABELS[result["protocol"]]
        assert (result["count"], result["errors"], result["aborted"], result["error"]) == (40, 0, False, None)


def test_latency_statistics_are_ordered(full: FullRun) -> None:
    for result in full.report["latency"]["results"]:
        ordered = [result[key] for key in STAT_ORDER]
        assert ordered == sorted(ordered), result["protocol"]
        assert 0 < result["min_ms"] <= result["mean_ms"] <= result["max_ms"]
        assert result["stdev_ms"] >= 0
        assert result["total_s"] > 0
        assert result["rps"] == pytest.approx(result["count"] / result["total_s"], rel=0.01)
        # La boucle dure au moins la somme de ses appels.
        assert result["total_s"] * 1000 >= result["mean_ms"] * result["count"] * 0.99


def test_remote_calls_cost_more_than_the_local_call(full: FullRun) -> None:
    results = by_protocol(full.report["latency"]["results"])
    local = results["local"]

    assert local["overhead_vs_local_x"] == 1.0
    for protocol in REMOTE_PROTOCOLS:
        remote = results[protocol]
        assert remote["mean_ms"] > local["mean_ms"]
        # Tolérance relative : le rapport est calculé avant l'arrondi des moyennes publiées.
        expected = pytest.approx(remote["mean_ms"] / local["mean_ms"], rel=0.001, abs=0.01)
        assert remote["overhead_vs_local_x"] == expected


def test_histograms_share_their_edges_and_count_within_the_clip(full: FullRun) -> None:
    results = full.report["latency"]["results"]
    edges = results[0]["histogram"]["edges_ms"]

    assert len(edges) == bench.HISTOGRAM_BUCKETS + 1
    assert edges == sorted(edges) and edges[0] < edges[-1]
    for result in results:
        histogram = result["histogram"]
        assert histogram["edges_ms"] == edges   # mêmes bornes : les distributions se superposent
        assert len(histogram["counts"]) == bench.HISTOGRAM_BUCKETS
        assert sum(histogram["counts"]) + histogram["clipped"] == result["count"]
        # 40 échantillons tiennent dans samples_ms : on peut recompter à partir des nombres publiés.
        samples = result["samples_ms"]
        assert len(samples) == result["count"]
        assert sum(histogram["counts"]) == sum(1 for sample in samples if sample <= edges[-1])
        assert histogram["clipped"] <= 1       # seule la queue au-delà du 99,5ᵉ centile est écartée
    assert edges[0] == min(result["min_ms"] for result in results)


def test_samples_are_downsampled_in_order(lab: LabRuntime) -> None:
    section = bench.run_latency_benchmark(lab, protocols=["local"], iterations=1000, warmup=0)
    result = section["results"][0]

    assert result["count"] == 1000
    assert len(result["samples_ms"]) == bench.MAX_SAMPLES
    assert result["min_ms"] <= min(result["samples_ms"]) and max(result["samples_ms"]) <= result["max_ms"]
    picked = bench._downsample(list(range(1000)))
    assert len(picked) == bench.MAX_SAMPLES and picked[0] == 0
    assert picked == sorted(set(picked))


def test_concurrent_clients_share_the_iterations(lab: LabRuntime) -> None:
    section = bench.run_latency_benchmark(
        lab, protocols=("custom", "rest"), iterations=31, warmup=2, concurrency=3
    )

    assert section["concurrency"] == 3
    for result in section["results"]:
        assert (result["count"], result["errors"]) == (31, 0)
        assert len(result["samples_ms"]) == 31
        assert result["rps"] > 0
        assert result["overhead_vs_local_x"] is None   # pas d'appel local dans cette mesure : rien à quoi comparer


def test_failing_calls_are_counted_not_raised(lab: LabRuntime) -> None:
    section = bench.run_latency_benchmark(lab, params={"product_id": "SKU-0000"}, iterations=12, warmup=2)

    assert json.dumps(section, allow_nan=False)
    for result in section["results"]:
        assert result["count"] == 0
        assert result["errors"] == bench.MAX_CONSECUTIVE_ERRORS   # inutile d'insister au-delà
        assert result["aborted"] is True
        assert result["error"]["code"] == "NOT_FOUND"
        assert all(result[key] is None for key in (*STAT_ORDER, "mean_ms", "stdev_ms", "overhead_vs_local_x"))
        assert result["histogram"] == {"edges_ms": [], "counts": [], "clipped": 0}
        assert result["samples_ms"] == []


def test_unreachable_protocol_does_not_abort_the_run(lab: LabRuntime) -> None:
    lab.conditions.apply_preset("outage")
    try:
        section = bench.run_latency_benchmark(
            lab, protocols=("local", "custom"), iterations=10, warmup=2, via_proxy=True
        )
    finally:
        lab.conditions.reset()
    results = by_protocol(section["results"])

    assert section["via_proxy"] is True
    assert (results["local"]["count"], results["local"]["errors"]) == (10, 0)
    assert results["custom"]["count"] == 0 and results["custom"]["errors"] > 0
    assert results["custom"]["error"]["code"] == "UNAVAILABLE"
    assert results["custom"]["mean_ms"] is None


def test_measuring_update_stock_leaves_the_stock_untouched(lab: LabRuntime) -> None:
    before = stock(lab)

    section = bench.run_latency_benchmark(
        lab, method="update_stock", protocols=("local", "custom"), iterations=8, warmup=2
    )

    assert section["params"] == {"product_id": PRODUCT, "delta": 1}
    assert [(result["count"], result["errors"]) for result in section["results"]] == [(8, 0), (8, 0)]
    assert stock(lab) == before


def test_latency_loop_runs_with_the_bus_muted(lab: LabRuntime) -> None:
    totals_before = copy.deepcopy(lab.collector.totals)
    bus_states: list[bool] = []

    bench.run_latency_benchmark(
        lab, iterations=10, warmup=2, progress=lambda *call: bus_states.append(lab.bus.enabled)
    )

    assert bus_states and not any(bus_states)
    assert lab.collector.totals == totals_before   # aucun appel mesuré n'a été tracé
    assert lab.bus.enabled


@pytest.mark.parametrize("arguments", [
    {"protocols": ("soap",)},
    {"protocols": ()},
    {"protocols": 3},
    {"method": "delete_everything"},
    {"method": "stream_analytics"},
    {"params": {"colour": "blue"}},
    {"params": ["SKU-1001"]},
    {"iterations": 0},
    {"iterations": True},
    {"warmup": -1},
    {"concurrency": 0},
])
def test_invalid_latency_requests_raise_value_error(lab: LabRuntime, arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        bench.run_latency_benchmark(lab, **arguments)


@pytest.mark.parametrize("settings", [
    {"iteration": 10},
    {"suites": ["latency", "astrology"]},
    {"suites": []},
    {"protocols": ["grpc", "soap"]},
    {"method": "nope"},
    {"via_proxy": "yes"},
    {"sweep_latencies_ms": [10, -5]},
    {"sweep_latencies_ms": []},
    {"scaling_limits": [0]},
    {"wire_calls": 0},
])
def test_invalid_configuration_is_rejected(settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        bench.resolve_config(settings)


def test_measures_need_a_started_lab() -> None:
    runtime = LabRuntime.ephemeral(bus=EventBus())

    for measure in (bench.run_latency_benchmark, bench.run_network_sweep, bench.measure_payload_sizes,
                    bench.run_full_benchmark):
        with pytest.raises(RuntimeError, match="démarré"):
            measure(runtime)


# --- Local vs distant --------------------------------------------------------

def test_sweep_section_has_one_point_per_latency(full: FullRun) -> None:
    network = full.report["network"]

    assert set(network) == {"method", "params", "iterations", "via_proxy", "protocols", "points"}
    assert (network["method"], network["iterations"], network["via_proxy"]) == ("get_product_details", 3, True)
    assert network["protocols"] == list(PROTOCOLS)
    assert [point["latency_ms"] for point in network["points"]] == [0, SWEEP_LATENCY_MS]
    for point in network["points"]:
        assert list(point["results"]) == list(PROTOCOLS)
        assert all(mean > 0 for mean in point["results"].values())
        assert point["errors"] == dict.fromkeys(PROTOCOLS, 0)


def test_latency_slows_remote_calls_but_not_the_local_one(full: FullRun) -> None:
    ideal, slow = (point["results"] for point in full.report["network"]["points"])

    for protocol in REMOTE_PROTOCOLS:
        assert slow[protocol] >= SWEEP_LATENCY_MS * 0.9             # la latence ajoutée se retrouve dans chaque appel
        assert slow[protocol] > ideal[protocol] + SWEEP_LATENCY_MS * 0.75
    assert slow["local"] < SWEEP_LATENCY_MS / 10 and ideal["local"] < SWEEP_LATENCY_MS / 10


def test_sweep_counts_failing_calls_instead_of_raising(lab: LabRuntime) -> None:
    section = bench.run_network_sweep(
        lab, latencies_ms=(0,), iterations=3, params={"product_id": "SKU-0000"}, protocols=("local", "grpc")
    )
    (point,) = section["points"]

    assert json.dumps(section, allow_nan=False)
    assert point["results"] == {"local": None, "grpc": None}   # aucun appel réussi : pas de moyenne
    assert point["errors"] == {"local": 3, "grpc": 3}
    assert bench.build_highlights({"network": section}) == []
    assert lab.conditions.snapshot()["preset"] == "ideal" and lab.bus.enabled


def test_sweep_restores_the_network_conditions(lab: LabRuntime) -> None:
    lab.conditions.apply_preset("wan")
    lab.conditions.update(jitter_ms=3.0)
    before = lab.conditions.snapshot()
    try:
        section = bench.run_network_sweep(lab, latencies_ms=(0, 5), iterations=2, protocols=("local", "custom"))
        after = lab.conditions.snapshot()
    finally:
        lab.conditions.reset()

    assert before["preset"] == "custom" and after == before
    assert [point["latency_ms"] for point in section["points"]] == [0, 5]


def test_sweep_restores_conditions_and_bus_when_interrupted(lab: LabRuntime) -> None:
    lab.conditions.apply_preset("lan")
    before = lab.conditions.snapshot()

    def interrupt(phase: str, fraction: float, message: str, partial: dict[str, Any] | None) -> None:
        if partial is not None:   # premier point mesuré : le réseau est alors réglé par le balayage
            raise KeyboardInterrupt

    try:
        with pytest.raises(KeyboardInterrupt):
            bench.run_network_sweep(lab, latencies_ms=(7, 9), iterations=1, protocols=("custom",), progress=interrupt)
        after = lab.conditions.snapshot()
    finally:
        lab.conditions.reset()

    assert after == before
    assert lab.bus.enabled


@pytest.mark.parametrize("arguments", [
    {"latencies_ms": (10, -1)},
    {"latencies_ms": ()},
    {"latencies_ms": ("fast",)},
    {"protocols": ("carrier-pigeon",)},
    {"method": "check_stock"},
    {"iterations": 0},
])
def test_invalid_sweep_requests_raise_value_error(lab: LabRuntime, arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        bench.run_network_sweep(lab, **arguments)
    assert lab.conditions.snapshot()["preset"] == "ideal"


# --- Progression et bus ------------------------------------------------------

def test_progress_is_monotonic_and_ends_at_one(full: FullRun) -> None:
    fractions = [fraction for _, fraction, _, _ in full.progress]
    phases = list(dict.fromkeys(phase for phase, *_ in full.progress))

    assert fractions == sorted(fractions)
    assert 0.0 <= fractions[0] and fractions[-1] == 1.0
    assert phases == [*bench.SUITES, "done"]
    assert all(isinstance(message, str) and message for _, _, message, _ in full.progress)
    assert full.progress[-1][0] == "done"
    assert full.progress[-1][3] == {"highlights": full.report["highlights"]}


def test_progress_delivers_each_section_as_it_becomes_available(full: FullRun) -> None:
    partials = [partial for *_, partial in full.progress if partial is not None]

    for suite in bench.SUITES:
        delivered = [partial[suite] for partial in partials if suite in partial]
        assert delivered[-1] == full.report[suite], suite
    # Les résultats arrivent protocole par protocole, puis point par point : l'interface dessine au fil de l'eau.
    assert [len(partial["latency"]["results"]) for partial in partials if "latency" in partial] == [1, 2, 3, 4]
    assert [len(partial["network"]["points"]) for partial in partials if "network" in partial] == [1, 2]


def test_latency_loop_notifies_at_most_twenty_times_per_protocol(lab: LabRuntime) -> None:
    calls: list[ProgressCall] = []

    bench.run_latency_benchmark(
        lab, protocols=("local", "custom"), iterations=500, warmup=5, progress=lambda *call: calls.append(call)
    )

    for label in (PROTOCOL_LABELS["local"], PROTOCOL_LABELS["custom"]):
        notifications = [call for call in calls if label in call[2]]
        assert 3 <= len(notifications) <= bench.PROGRESS_TICKS
    assert {phase for phase, *_ in calls} == {"latency"}
    assert calls[-1][1] == 1.0


def test_bus_is_enabled_again_and_still_traces_calls(full: FullRun, lab: LabRuntime) -> None:
    assert lab.bus.enabled
    with lab.client("custom") as client:
        client.calculate_factorial(5)
        trace = lab.collector.get(client.last_call_id)
    assert trace is not None and trace.complete


# --- Faits marquants ---------------------------------------------------------

def test_highlights_are_built_from_the_measurements(full: FullRun) -> None:
    report, items = full.report, full.report["highlights"]

    assert [item["id"] for item in items] == [
        "payload_size", "payload_scaling", "wire_bytes", "http_overhead", "serialization_speed",
        "remote_cost", "fastest_remote", "tail_latency", "throughput", "network_latency",
    ]
    for item in items:
        assert set(item) == {"id", "title", "value", "detail", "tone"}
        assert item["tone"] in {"success", "warning", "info"}
        assert all(isinstance(text, str) and text for text in item.values())

    product = size_row(report, "get_product_details", "response")
    assert highlight(items, "payload_size")["value"] == reports.fr_percent(product["protobuf_vs_json_pct"], signed=True)
    assert reports.fr_bytes(product["protobuf_bytes"]) in highlight(items, "payload_size")["detail"]
    remote = [result for result in report["latency"]["results"] if result["protocol"] != "local"]
    fastest = min(remote, key=lambda result: result["mean_ms"])
    assert highlight(items, "fastest_remote")["value"] == fastest["label"]
    local = by_protocol(report["latency"]["results"])["local"]
    assert highlight(items, "remote_cost")["value"] == reports.fr_ratio(fastest["mean_ms"] / local["mean_ms"])
    lightest = min(report["payload"]["wire"], key=lambda row: row["total_per_call"])
    assert lightest["label"] in highlight(items, "wire_bytes")["value"]
    assert str(SWEEP_LATENCY_MS) in highlight(items, "network_latency")["title"]


@pytest.mark.parametrize("means, winner", [
    ({"local": 0.01, "custom": 0.15, "grpc": 0.30, "rest": 0.20}, "custom"),
    ({"local": 0.01, "custom": 0.40, "grpc": 0.12, "rest": 0.20}, "grpc"),
    ({"local": 0.01, "custom": 0.40, "grpc": 0.30, "rest": 0.09}, "rest"),
])
def test_fastest_protocol_is_the_measured_one(means: dict[str, float], winner: str) -> None:
    items = bench.build_highlights({"latency": fake_latency(means)})

    assert highlight(items, "fastest_remote")["value"] == PROTOCOL_LABELS[winner]
    assert PROTOCOL_LABELS[winner] in highlight(items, "remote_cost")["detail"]
    assert highlight(items, "remote_cost")["value"] == reports.fr_ratio(means[winner] / means["local"])
    assert highlight(items, "throughput")["value"] == f"{reports.fr_number(1000 / means[winner])} appels/s"


def test_highlights_do_not_claim_an_advantage_the_numbers_deny() -> None:
    row = {
        "method": "get_product_details", "title": "", "direction": "response", "json_bytes": 100,
        "json_rpc_bytes": 104, "protobuf_bytes": 120, "grpc_bytes": 125, "rest_bytes": 200, "rest_body_bytes": 90,
        "protobuf_vs_json_pct": 20.0,
    }
    report = {
        "payload": {"rows": [row], "wire": [], "scaling": []},
        "latency": fake_latency({"local": 0.01, "custom": 0.1, "grpc": 0.3}),
    }

    items = bench.build_highlights(report)

    size = highlight(items, "payload_size")
    assert (size["value"], size["tone"]) == (f"+20,0{NBSP}%", "warning")
    ranking = highlight(items, "fastest_remote")["detail"]
    assert "3,0 fois plus lent" in ranking
    assert "plus petits" not in ranking   # Protobuf n'a pas été mesuré plus compact : on ne l'affirme pas


def test_empty_report_has_no_highlight() -> None:
    assert bench.build_highlights({}) == []
    assert bench.build_highlights(dict.fromkeys(("payload", "serialization", "latency", "network"))) == []


# --- Rapports : enregistrement et relecture ----------------------------------

def test_report_round_trip(full: FullRun, reports_dir: Path) -> None:
    path = reports.save_report(full.report)

    assert path == reports_dir / f"{full.report['id']}.json"
    assert reports.load_report(path.name) == full.report
    assert reports.load_report(full.report["id"]) == full.report   # l'extension est facultative
    listing = reports.list_reports()
    assert listing == [{
        "name": path.name,
        "created_at": listing[0]["created_at"],
        "size": path.stat().st_size,
        "kind": "benchmark",
    }]
    assert listing[0]["created_at"][:19] == full.report["created_at"][:19]

    assert reports.save_report(full.report) == path   # même rapport : même fichier, réécrit
    assert [entry.name for entry in reports_dir.iterdir()] == [path.name]


def test_reports_are_listed_newest_first(reports_dir: Path) -> None:
    for identifier in ("benchmark-20260102-080000", "benchmark-20260301-120000", "failures-20260215-093000"):
        reports.save_report({"id": identifier})
    (reports_dir / "notes.txt").write_text("pas un rapport", encoding="utf-8")

    listing = reports.list_reports()

    assert [entry["name"] for entry in listing] == [
        "benchmark-20260301-120000.json", "failures-20260215-093000.json", "benchmark-20260102-080000.json",
    ]
    assert [entry["kind"] for entry in listing] == ["benchmark", "failures", "benchmark"]
    assert listing[0]["created_at"].startswith("2026-03-01T12:00:00")
    assert all(entry["size"] > 0 for entry in listing)


def test_report_without_identifier_is_stamped_at_saving_time(reports_dir: Path) -> None:
    path = reports.save_report({"latency": None})

    assert re.fullmatch(r"benchmark-\d{8}-\d{6}\.json", path.name)
    assert reports.load_report(path.name) == {"latency": None}
    assert reports.list_reports(reports_dir)[0]["name"] == path.name


def test_listing_an_absent_folder_is_empty(reports_dir: Path) -> None:
    assert not reports_dir.exists()
    assert reports.list_reports() == []


@pytest.mark.parametrize("name", [
    "../secret.json",
    "..\\secret.json",
    "../secret",
    "reports/../../secret.json",
    "sub/report.json",
    "sub\\report.json",
    "/etc/passwd",
    "C:\\Windows\\win.ini",
    "C:secret.json",
    "..",
    "",
    ".json",
    "secret.json:stream",
    "x" * 300 + ".json",
    None,
])
def test_report_names_cannot_leave_the_reports_folder(reports_dir: Path, name: Any) -> None:
    reports_dir.mkdir()
    (reports_dir.parent / "secret.json").write_text('{"secret": true}', encoding="utf-8")

    with pytest.raises(ValueError, match="invalide"):
        reports.load_report(name)


def test_missing_or_unreadable_reports_are_reported_clearly(reports_dir: Path) -> None:
    reports_dir.mkdir()
    (reports_dir / "broken.json").write_text("{ pas du JSON", encoding="utf-8")
    (reports_dir / "list.json").write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="introuvable"):
        reports.load_report("benchmark-20260101-000000.json")
    with pytest.raises(ValueError, match="illisible"):
        reports.load_report("broken.json")
    with pytest.raises(ValueError, match="illisible"):
        reports.load_report("list.json")


# --- Rapports : exports ------------------------------------------------------

def test_markdown_export_has_the_comparison_tables(full: FullRun) -> None:
    report = full.report
    markdown = reports.to_markdown(report)
    tables = reports.report_tables(report)

    assert [table.key for table in tables] == [
        "payload", "wire", "scaling", "serialization", "latency", "percentiles", "network",
    ]
    assert markdown.startswith("# RPC Explorer")
    assert report["id"] in markdown
    for table in tables:
        assert f"## {table.title}" in markdown
        assert table.render("github") in markdown
    assert "|---" in markdown   # tableaux au format « github » de tabulate
    assert "## À retenir" in markdown
    assert all(item["title"] in markdown and item["detail"] in markdown for item in report["highlights"])
    # Les deux comparaisons du cahier des charges : tailles JSON vs Protobuf, temps local vs RPC maison vs gRPC.
    product = size_row(report, "get_product_details", "response")
    payload_table = tables[0].render("github")
    assert reports.fr_number(product["json_bytes"]) in payload_table
    assert reports.fr_number(product["protobuf_bytes"]) in payload_table
    latency_table = tables[4].render("github")
    for result in report["latency"]["results"]:
        assert result["label"] in latency_table
        assert reports.fr_number(result["mean_ms"], 4) in latency_table


def test_csv_export_is_tidy_and_numeric(full: FullRun) -> None:
    report = full.report
    rows = list(csv.DictReader(io.StringIO(reports.to_csv(report))))

    assert list(rows[0]) == ["section", "subject", "series", "metric", "value", "unit"]
    assert {row["section"] for row in rows} == {"payload", "wire", "scaling", "serialization", "latency", "network"}
    assert all(float(row["value"]) >= -100 for row in rows)   # nombres bruts, point décimal
    grpc = by_protocol(report["latency"]["results"])["grpc"]
    assert {"section": "latency", "subject": "get_product_details", "series": "grpc", "metric": "mean_ms",
            "value": str(grpc["mean_ms"]), "unit": "ms"} in rows
    product = size_row(report, "get_product_details", "response")
    assert {"section": "payload", "subject": "get_product_details/response", "series": "protobuf",
            "metric": "bytes", "value": str(product["protobuf_bytes"]), "unit": "B"} in rows
    assert sum(row["section"] == "network" for row in rows) == 2 * len(PROTOCOLS)


def test_exports_show_unmeasured_values_as_a_dash(lab: LabRuntime) -> None:
    latency = bench.run_latency_benchmark(lab, params={"product_id": "SKU-0000"}, iterations=6, warmup=0)
    report = {"latency": latency, "payload": None, "network": None, "highlights": bench.build_highlights({})}

    text, markdown = reports.to_text(report), reports.to_markdown(report)
    rows = list(csv.DictReader(io.StringIO(reports.to_csv(report))))

    assert reports.MISSING in text and reports.MISSING in markdown
    assert "À retenir" not in text
    assert {row["value"] for row in rows if row["metric"] == "mean_ms"} == {""}
    assert reports.to_text({}) == "" and reports.to_csv({}).splitlines() == ["section,subject,series,metric,value,unit"]


def test_terminal_export_frames_every_table(full: FullRun) -> None:
    text = reports.to_text(full.report)

    for table in reports.report_tables(full.report):
        assert table.title in text
    assert "╭" in text and "│" in text
    assert "À retenir" in text
    assert max(len(line) for line in text.splitlines() if line.startswith(("│", "╭"))) <= 120


def test_numbers_are_formatted_the_french_way() -> None:
    assert reports.fr_number(1234567) == f"1{NBSP}234{NBSP}567"
    assert reports.fr_number(1234.56, 1) == f"1{NBSP}234,6"
    assert reports.fr_number(-3.25, 1) == "\u22123,2"
    assert reports.fr_number(-0.004, 1) == "0,0"
    assert reports.fr_number(None) == reports.MISSING
    assert (reports.fr_compact(50), reports.fr_compact(12.5)) == ("50", "12,5")
    assert reports.fr_percent(-49.94, signed=True) == f"\u221249,9{NBSP}%"
    assert reports.fr_percent(12, 0, signed=True) == f"+12{NBSP}%"
    assert reports.fr_ratio(9.34) == f"×{NBSP}9,3"
    assert reports.fr_ratio(1234.4) == f"×{NBSP}1{NBSP}234"
    assert reports.fr_duration(0.0005) == f"500{NBSP}ns"
    assert reports.fr_duration(0.0123) == f"12,3{NBSP}µs"
    assert reports.fr_duration(0.25) == f"250{NBSP}µs"
    assert reports.fr_duration(1.234) == f"1,23{NBSP}ms"
    assert reports.fr_duration(2500) == f"2,50{NBSP}s"
    assert reports.fr_bytes(1) == f"1{NBSP}octet"
    assert reports.fr_bytes(268) == f"268{NBSP}octets"
    assert reports.fr_bytes(69.8) == f"69,8{NBSP}octets"
    assert reports.fr_bytes(466_807) == f"466,8{NBSP}ko"
    assert reports.fr_bytes(2_500_000) == f"2,50{NBSP}Mo"


# --- Ligne de commande -------------------------------------------------------

def test_command_line_runs_prints_and_saves_a_report(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bench, "QUICK_CONFIG", {**bench.DEFAULT_CONFIG, **TINY_CONFIG, "scaling_limits": [1, 10]})

    assert bench.main(["--quick", "--iterations", "12"]) == 0

    output = capsys.readouterr().out
    assert "banc d'essai de performance (version courte)" in output
    assert "Taille des messages (octets) — JSON vs Protobuf" in output
    assert "Temps moyen par appel (ms)" in output
    assert "Rapport enregistré" in output
    (saved,) = reports.list_reports()
    report = reports.load_report(saved["name"])
    assert report["latency"]["iterations"] == 12
    assert [row["items"] for row in report["payload"]["scaling"]] == [1, 10]
    assert BUS.enabled


def test_command_line_rejects_a_non_positive_iteration_count(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as outcome:
        bench.main(["--iterations", "0"])

    assert outcome.value.code == 2
    assert "entier compris entre 1" in capsys.readouterr().err
