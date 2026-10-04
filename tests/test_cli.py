"""Tests du CLI (plan §12).

Trois niveaux :

* ``main.py`` lancé comme un vrai programme (sous-processus, sortie décodée en
  UTF-8 strict) — c'est ce que tape l'utilisateur, codes de sortie compris ;
* l'analyseur d'arguments et la conversion des arguments de ``--call`` ;
* les fonctions ``run_*`` et le menu interactif, pilotés dans le processus sur
  un laboratoire à ports éphémères, avec une console en mémoire et une saisie scriptée.

Les sous-processus utilisent les ports fixes décalés de ``RPCX_PORT_OFFSET=400`` :
ils ne doivent jamais tourner à deux en même temps. Un verrou de fichier le
garantit, même entre deux exécutions de pytest simultanées.
"""
from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest

import main
from cli import cli_runner
from cli.benchmark_view import LATENCY_TITLE, PAYLOAD_TITLE, WIRE_TITLE, save_report_files
from cli.calls import build_params, convert_argument, execute_call, parse_value, render_parallel, run_parallel
from cli.theme import Column, bar, fill_table, make_console, make_table, sparkline
from cli.wire import hexdump, wire_report
from common import config
from common.catalog import method_spec
from common.config import APP_NAME, HOST, PROTOCOL_LABELS, PROTOCOLS, REMOTE_PROTOCOLS, VERSION, Ports
from common.telemetry import PIPELINE, EventBus
from lab import LabRuntime

ROOT = Path(__file__).resolve().parent.parent
PORT_OFFSET = 400
CLI_TIMEOUT_S = 120
LOCK_TIMEOUT_S = 300
CONSOLE_WIDTH = 118
NBSP = "\u00a0"


def readable(text: str) -> str:
    """Les nombres affichés utilisent des espaces insécables (« 1 234 octets ») : on les compare comme des espaces."""
    return text.replace(NBSP, " ")


# --- Lancement de main.py ----------------------------------------------------

@contextmanager
def exclusive_ports() -> Iterator[None]:
    """Verrou inter-processus : un seul laboratoire à la fois sur les ports décalés de ``PORT_OFFSET``."""
    path = Path(tempfile.gettempdir()) / f"rpcx-cli-tests-{PORT_OFFSET}.lock"
    with open(path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def spawn_cli(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Lance ``python main.py`` ; la sortie est décodée en UTF-8 strict (une erreur d'encodage fait échouer)."""
    environment = {**os.environ, "RPCX_PORT_OFFSET": str(PORT_OFFSET), "COLUMNS": str(CONSOLE_WIDTH), **env}
    # Variables qui feraient croire à Rich qu'il écrit dans un terminal.
    for name in ("FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        environment.pop(name, None)
    result = subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=CLI_TIMEOUT_S,
    )
    result.stdout, result.stderr = readable(result.stdout), readable(result.stderr)
    return result


def run_cli(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    with exclusive_ports():
        return spawn_cli(*args, **env)


def assert_clean(result: subprocess.CompletedProcess[str], expected_code: int = 0) -> None:
    assert result.returncode == expected_code, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Traceback" not in result.stdout + result.stderr


# --- Aide, version, erreurs d'utilisation ------------------------------------

def test_version_prints_name_and_version() -> None:
    result = spawn_cli("--version")
    assert_clean(result)
    assert APP_NAME in result.stdout
    assert VERSION in result.stdout


def test_help_is_french_and_utf8_whatever_the_console_encoding() -> None:
    # Même quand l'environnement impose cp1252, main.py repasse ses sorties en UTF-8.
    result = spawn_cli("--help", PYTHONIOENCODING="cp1252")
    assert_clean(result)
    assert "Utilisation : python main.py" in result.stdout
    assert "démonstrateur pédagogique" in result.stdout
    for option in ("--benchmark", "--simulate-failures", "--contract", "--demo", "--dashboard", "--serve",
                   "--call", "--iterations", "--quick", "--no-save", "--scenario", "--protocol", "--no-pause",
                   "--port", "--no-browser", "--inspect", "--via-proxy", "--timeout", "--version"):
        assert option in result.stdout


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--call", "no_such_method", "1"), "Procédure inconnue"),
        (("--call", "calculate_factorial", "1", "2"), "Trop d'arguments"),
        (("--quick",), "--quick ne s'utilise qu'avec --benchmark"),
        (("--benchmark", "--contract"), "ne peuvent pas être utilisées ensemble"),
        (("--benchmark", "--iterations", "0"), "un entier compris entre 1 et"),
        (("--simulate-failures", "--protocol", "local"), "un réseau à dérégler"),
        (("--simulate-failures", "--scenario", "nope"), "Scénario inconnu : « nope »"),
        (("--call", "calculate_factorial", "--protocol", "soap"), "n'accepte pas 'soap'"),
        (("--frobnicate",), "option ou argument inconnu"),
    ],
)
def test_usage_errors_exit_2_with_a_french_message(arguments: tuple[str, ...], message: str) -> None:
    result = spawn_cli(*arguments)      # refusé avant tout démarrage : aucun port n'est ouvert
    assert_clean(result, expected_code=2)
    assert message in result.stderr
    assert "Erreur :" in result.stderr
    assert result.stdout == ""


def test_unknown_method_lists_the_available_procedures() -> None:
    result = spawn_cli("--call", "get_product")
    assert_clean(result, expected_code=2)
    assert "Procédure inconnue : « get_product »" in result.stderr
    assert "get_product_details" in result.stderr


# --- --call ------------------------------------------------------------------

@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_call_on_each_protocol(protocol: str) -> None:
    result = run_cli("--call", "calculate_factorial", "5", "--protocol", protocol)
    assert_clean(result)
    assert '"result": "120"' in result.stdout
    assert "calculate_factorial(n=5)" in result.stdout
    assert PROTOCOL_LABELS[protocol] in result.stdout
    if protocol == "local":
        assert "aucun octet échangé" in result.stdout
    else:
        assert "requête" in result.stdout and "réponse" in result.stdout and "octets" in result.stdout


def test_call_accepts_named_arguments_and_catalog_defaults() -> None:
    result = run_cli("--call", "list_products", "limit=2", "--protocol", "rest")
    assert_clean(result)
    assert 'list_products(limit=2, category="")' in result.stdout
    assert '"total": 24' in result.stdout
    assert '"id": "SKU-1002"' in result.stdout and '"id": "SKU-1003"' not in result.stdout


def test_call_with_inspect_shows_pipeline_and_raw_bytes() -> None:
    result = run_cli("--call", "update_stock", "SKU-1001", "-3", "--protocol", "grpc", "--inspect")
    assert_clean(result)
    out = result.stdout
    assert '"new_stock": 81' in out
    for stage in ("Appel du stub", "Marshalling", "Dispatch", "Exécution", "Retour à l'appelant"):
        assert stage in out
    assert "Requête — gRPC / Protobuf · 17 octets" in out
    assert "000000  00 00 00 00 0c 0a 08 53  4b 55 2d 31 30 30 31 10" in out     # préfixe gRPC puis « SKU-1001 »
    assert "préfixe gRPC (5 octets" in out
    assert "/rpcexplorer.v1.InventoryService/UpdateStock" in out
    assert "Type de fil" in out and "sint32" in out and "VARINT" in out


def test_call_through_the_proxy_with_inspect_on_the_home_made_rpc() -> None:
    result = run_cli("--call", "get_product_details", "SKU-1005", "--via-proxy", "--inspect", "--timeout", "3")
    assert_clean(result)
    assert "via le proxy de chaos" in result.stdout
    assert "Longueur (uint32 big-endian) · 4 octets" in result.stdout
    assert '"method": "get_product_details"' in result.stdout


def test_call_streams_items_as_they_arrive() -> None:
    result = run_cli("--call", "stream_analytics", "3", "10", "--protocol", "rest")
    assert_clean(result)
    assert result.stdout.count("▸ #") == 3
    assert "Flux terminé" in result.stdout and "3 éléments" in result.stdout


def test_rpc_error_prints_a_panel_and_exits_2() -> None:
    result = run_cli("--call", "get_product_details", "SKU-9999", "--protocol", "grpc")
    assert_clean(result, expected_code=2)
    assert "Erreur RPC · NOT_FOUND" in result.stdout
    assert "Produit inconnu : SKU-9999" in result.stdout
    assert "Rejouable" in result.stdout and "non" in result.stdout


def test_method_unavailable_on_a_protocol_is_an_rpc_error() -> None:
    result = run_cli("--call", "check_stock", "--protocol", "custom")
    assert_clean(result, expected_code=2)
    assert "METHOD_NOT_FOUND" in result.stdout


def test_busy_port_is_explained_and_exits_1() -> None:
    with exclusive_ports(), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        if os.name == "nt":
            squatter.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        squatter.bind((HOST, Ports().custom + PORT_OFFSET))
        squatter.listen()
        result = spawn_cli("--call", "calculate_factorial", "5")
    assert_clean(result, expected_code=1)
    assert "Le laboratoire n'a pas pu démarrer" in result.stdout
    assert str(Ports().custom + PORT_OFFSET) in result.stdout
    assert "RPCX_PORT_OFFSET" in result.stdout


# --- Modes complets ----------------------------------------------------------

def test_benchmark_prints_the_required_tables_in_order() -> None:
    reports_before = sorted(path.name for path in config.REPORTS_DIR.glob("*")) if config.REPORTS_DIR.exists() else []
    result = run_cli("--benchmark", "--quick", "--iterations", "30", "--no-save")
    assert_clean(result)
    out = result.stdout
    assert "Taille du paquet (octets) : JSON vs Protobuf" in out
    assert "Temps moyen par appel (ms) : Local vs RPC maison vs gRPC vs REST" in out
    landmarks = [
        PAYLOAD_TITLE,
        WIRE_TITLE,
        LATENCY_TITLE,
        "Sérialisation : JSON vs Protobuf",
        "Local vs distant : temps moyen par appel",
        "À retenir",
        "Rapport non enregistré",
    ]
    positions = [out.index(landmark) for landmark in landmarks]
    assert positions == sorted(positions)
    for column in ("Économie", "Moyenne", "Médiane", "p95", "p99", "Appels/s", "× local"):
        assert column in out
    for protocol in PROTOCOLS:
        assert PROTOCOL_LABELS[protocol] in out
    assert "█" in out and "▁" in out      # barres et courbes miniatures
    reports_after = sorted(path.name for path in config.REPORTS_DIR.glob("*")) if config.REPORTS_DIR.exists() else []
    assert reports_after == reports_before


def test_simulate_failures_plays_one_scenario_with_timeline_and_verdict() -> None:
    result = run_cli("--simulate-failures", "--scenario", "duplicate_execution")
    assert_clean(result)
    out = result.stdout
    assert "La réponse perdue et la double exécution" in out
    assert PROTOCOL_LABELS["custom"] in out
    for word in ("tentative", "attente", "appel RPC", "réseau", "état"):
        assert word in out
    assert "Tentative n° 1 : échec (UNAVAILABLE)" in out
    assert "Verdict" in out and "À retenir" in out
    assert "Exécutions sans clé d'idempotence" in out
    assert "Le piège de la boucle innocente" not in out      # un seul scénario demandé


def test_contract_shows_diff_changes_and_scenarios() -> None:
    result = run_cli("--contract")
    assert_clean(result)
    out = result.stdout
    assert "rpc_grpc/protos/service.proto" in out and "rpc_grpc/protos/service_v2.proto" in out
    assert "-   rpc GetProductDetails (ProductRequest) returns (Product);" in out
    assert "+   rpc GetProduct (ProductRequest) returns (Product);" in out
    for tag in ("BREAKING", "COMPATIBLE", "REJET", "PLANTAGE", "CORRUPTION SILENCIEUSE"):
        assert tag in out
    assert "Attendu — ce qu'un serveur v1 aurait fait" in out and "Observé — face au serveur v2" in out
    assert "UNIMPLEMENTED" in out and "-32602" in out
    assert "INATTENDUE" not in out
    assert "Règles d'or" in out and "Bilan" in out


def test_demo_runs_without_pause_through_the_three_middlewares() -> None:
    result = run_cli("--demo", "--no-pause")
    assert_clean(result)
    out = result.stdout
    for protocol in REMOTE_PROTOCOLS:
        assert f"Requête — {PROTOCOL_LABELS[protocol]}" in out
        assert f"Réponse — {PROTOCOL_LABELS[protocol]}" in out
    assert "Ce qui se passe" in out and "Le stub sérialise" in out          # pipeline expliqué
    assert "Longueur (uint32 big-endian)" in out                             # trame JSON-RPC
    assert "Type de fil" in out and "previous_stock" in out                  # décodage Protobuf
    assert "Ligne de requête" in out and "POST /api/stock/SKU-1001 HTTP/1.1" in out
    assert "Le même appel, quatre chemins" in out and "En résumé" in out
    assert "Entrée pour" not in out                                           # aucune pause hors terminal


# --- Analyseur d'arguments ---------------------------------------------------

def parse(*arguments: str) -> tuple[str, object]:
    parser = main.build_parser()
    args = parser.parse_args(list(arguments))
    return main.resolve_mode(parser, args), args


def test_parser_defaults_to_the_interactive_menu() -> None:
    mode, args = parse()
    assert mode == main.INTERACTIVE
    assert args.call is None and args.protocol is None and args.scenario is None
    assert not any((args.benchmark, args.simulate_failures, args.contract, args.demo, args.dashboard, args.serve))


def test_parser_reads_every_mode_and_its_options() -> None:
    mode, args = parse("--benchmark", "--iterations", "250", "--quick", "--no-save")
    assert (mode, args.iterations, args.quick, args.no_save) == ("benchmark", 250, True, True)

    mode, args = parse("--simulate-failures", "--scenario", "all", "--protocol", "grpc")
    assert (mode, args.scenario, args.protocol) == ("simulate_failures", "all", "grpc")

    assert parse("--contract")[0] == "contract"
    assert parse("--serve")[0] == "serve"

    mode, args = parse("--demo", "--no-pause")
    assert (mode, args.no_pause) == ("demo", True)

    mode, args = parse("--dashboard", "--port", "9000", "--no-browser")
    assert (mode, args.port, args.no_browser) == ("dashboard", 9000, True)

    mode, args = parse("--call", "update_stock", "SKU-1001", "-3", "--protocol", "rest", "--inspect",
                       "--via-proxy", "--timeout", "2.5")
    assert mode == "call"
    assert args.call == ["update_stock", "SKU-1001", "-3"]      # « -3 » est un argument, pas une option
    assert (args.protocol, args.inspect, args.via_proxy, args.timeout) == ("rest", True, True, 2.5)


@pytest.mark.parametrize(
    "arguments",
    [
        ("--benchmark", "--serve"),
        ("--iterations", "10"),
        ("--call",),
        ("--call", "calculate_factorial", "--scenario", "all"),
        ("--dashboard", "--port", "70000"),
        ("--call", "calculate_factorial", "--timeout", "0"),
        ("--demo", "--inspect"),
    ],
)
def test_parser_rejects_inconsistent_command_lines(arguments: tuple[str, ...], capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as raised:
        parse(*arguments)
    assert raised.value.code == 2
    assert "Erreur :" in capsys.readouterr().err


def test_benchmark_config_follows_the_options() -> None:
    quick = cli_runner.benchmark_config(quick=True, iterations=30)
    full = cli_runner.benchmark_config()
    assert quick["iterations"] == 30
    assert len(quick["sweep_latencies_ms"]) < len(full["sweep_latencies_ms"])
    assert full["iterations"] > quick["iterations"]


# --- Arguments de --call → paramètres ----------------------------------------

def test_parse_value_reads_json_and_falls_back_on_text() -> None:
    assert parse_value("-3") == -3
    assert parse_value("2.5") == 2.5
    assert parse_value("true") is True
    assert parse_value('["SKU-1001", "SKU-1002"]') == ["SKU-1001", "SKU-1002"]
    assert parse_value("SKU-1001") == "SKU-1001"
    assert parse_value("") == ""


def test_build_params_maps_positional_arguments_in_catalog_order() -> None:
    assert build_params("update_stock", ["SKU-1007", "-4", "cle-1"]) == {
        "product_id": "SKU-1007", "delta": -4, "idempotency_key": "cle-1",
    }
    assert build_params("stream_analytics", ["3", "50"]) == {"samples": 3, "interval_ms": 50}


def test_build_params_fills_missing_arguments_with_catalog_defaults() -> None:
    assert build_params("calculate_factorial", []) == {"n": 20}
    assert build_params("update_stock", ["SKU-1002"]) == {"product_id": "SKU-1002", "delta": -1, "idempotency_key": ""}
    assert build_params("check_stock", []) == {"product_ids": ["SKU-1001", "SKU-1012", "SKU-1022"]}


def test_build_params_accepts_named_and_structured_arguments() -> None:
    assert build_params("list_products", ["category=Audio"]) == {"limit": 20, "category": "Audio"}
    assert build_params("list_products", ["category=Audio", "5"]) == {"limit": 5, "category": "Audio"}
    assert build_params("check_stock", ['["SKU-1003"]']) == {"product_ids": ["SKU-1003"]}
    updates = '[{"product_id": "SKU-1001", "delta": 2}]'
    assert build_params("bulk_update_stock", [updates]) == {"updates": [{"product_id": "SKU-1001", "delta": 2}]}


def test_text_parameters_keep_their_raw_text() -> None:
    product_id = method_spec("get_product_details").params[0]
    assert convert_argument(product_id, "1001") == "1001"             # pas l'entier 1001
    assert convert_argument(product_id, '"SKU-1001"') == "SKU-1001"   # chaîne JSON : guillemets retirés
    assert build_params("update_stock", ["SKU-1001", "abc"])["delta"] == "abc"   # au serveur de le refuser
    assert build_params("update_stock", ["x=1"])["product_id"] == "x=1"          # « x » n'est pas un paramètre


@pytest.mark.parametrize(
    ("method", "arguments", "message"),
    [
        ("teleport", [], "Procédure inconnue"),
        ("calculate_factorial", ["5", "6"], "Trop d'arguments"),
        ("update_stock", ["SKU-1001", "product_id=SKU-1002"], "donné deux fois"),
    ],
)
def test_build_params_rejects_bad_arguments(method: str, arguments: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_params(method, arguments)


# --- Briques d'affichage -----------------------------------------------------

def test_bars_and_sparklines_are_proportional() -> None:
    assert bar(10, 10, width=8) == "█" * 8
    assert bar(5, 10, width=8) == "█" * 4
    assert bar(0.001, 10, width=8) == "▏"          # une valeur non nulle reste visible
    assert bar(0, 10) == "" and bar(None, 10) == "" and bar(5, 0) == ""
    assert sparkline([1, 2, 3, 4, 5, 6, 7, 8], width=8) == "▁▂▃▄▅▆▇█"
    assert sparkline([3, 3, 3], width=3) == "▁▁▁"
    assert len(sparkline(list(range(400)), width=28)) == 28
    assert sparkline([]) == ""


def test_hexdump_lays_out_offsets_bytes_and_ascii() -> None:
    payload = b"\x00\x00\x00\x12" + b'{"jsonrpc":"2.0"}!'
    segments = [
        {"label": "Longueur", "start": 0, "end": 4, "kind": "frame"},
        {"label": "Message", "start": 4, "end": len(payload), "kind": "body"},
    ]
    lines = hexdump(payload, segments).plain.splitlines()
    assert lines[0] == '000000  00 00 00 12 7b 22 6a 73  6f 6e 72 70 63 22 3a 22  ····{"jsonrpc":"'
    assert lines[1].startswith("000010  32 2e 30 22 7d 21")
    assert lines[1].endswith('2.0"}!')
    truncated = readable(hexdump(bytes(range(64)), [], limit=32).plain)
    assert "000010" in truncated and "000020" not in truncated
    assert "32 octets de plus" in truncated


def test_fill_table_drops_comfort_columns_in_a_narrow_terminal() -> None:
    columns = (Column("Protocole"), Column("Appels", numeric=True, wide_only=True), Column("Moyenne", numeric=True))
    rows = [("gRPC", "20", "0,40"), None, ("REST", "20", "0,25")]      # None : séparateur de groupe
    wide = fill_table(make_table(), columns, rows)
    narrow = fill_table(make_table(), columns, rows, narrow=True)
    assert [column.header for column in wide.columns] == ["Protocole", "Appels", "Moyenne"]
    assert [column.header for column in narrow.columns] == ["Protocole", "Moyenne"]
    assert wide.row_count == narrow.row_count == 2
    assert list(narrow.columns[1].cells) == ["0,40", "0,25"]
    assert narrow.columns[1].justify == "right"


def test_save_report_files_writes_json_and_markdown_side_by_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)
    report = {"id": "benchmark-20260102-030405", "created_at": "2026-01-02T03:04:05+00:00", "highlights": []}
    json_path, markdown_path = save_report_files(report)
    assert json_path == tmp_path / "benchmark-20260102-030405.json"
    assert markdown_path == tmp_path / "benchmark-20260102-030405.md"
    assert markdown_path.read_text(encoding="utf-8").startswith("# RPC Explorer")


# --- Fonctions run_* et menu interactif, dans le processus ------------------

@pytest.fixture(scope="module")
def lab() -> Iterator[LabRuntime]:
    with LabRuntime.ephemeral(bus=EventBus()) as runtime:
        yield runtime


class Screen:
    """Console Rich en mémoire : ce que l'utilisateur verrait, sous forme de texte."""

    def __init__(self, width: int = CONSOLE_WIDTH) -> None:
        self._buffer = io.StringIO()
        self.console = make_console(self._buffer, width=width)

    @property
    def text(self) -> str:
        return readable(self._buffer.getvalue())


def scripted(*answers: str | type[BaseException]) -> Callable[[str], str]:
    """Saisie scriptée : rend les réponses une à une (une classe d'exception est levée), puis signale la fin."""
    remaining = iter(answers)

    def read(prompt: str) -> str:
        answer = next(remaining, EOFError)
        if isinstance(answer, str):
            return answer
        raise answer

    return read


def test_execute_call_returns_result_sizes_and_the_full_pipeline(lab: LabRuntime) -> None:
    outcome = execute_call(lab, "custom", "calculate_factorial", {"n": 5})
    assert outcome.ok and outcome.result["result"] == "120"
    assert [event.stage for event in outcome.trace.events] == list(PIPELINE)
    assert outcome.request_bytes == outcome.trace.events[2].size > 4
    assert outcome.response_bytes == outcome.trace.events[9].size > 4
    assert outcome.duration_ms > 0 and outcome.call_id.startswith("custom-")


def test_execute_call_consumes_a_stream_and_reports_each_item(lab: LabRuntime) -> None:
    seen: list[tuple[int, int]] = []
    outcome = execute_call(
        lab, "rest", "stream_analytics", {"samples": 3, "interval_ms": 0},
        on_item=lambda rank, elapsed_ms, item: seen.append((rank, item["seq"])),
    )
    assert outcome.ok and outcome.items == 3 and outcome.result is None
    assert seen == [(1, 1), (2, 2), (3, 3)]
    items = [event.size for event in outcome.trace.events if event.stage == "client.stream_item"]
    assert len(items) == 3 and outcome.response_bytes > sum(items)


def test_execute_call_turns_an_rpc_error_into_an_outcome(lab: LabRuntime) -> None:
    outcome = execute_call(lab, "grpc", "get_product_details", {"product_id": "SKU-0000"})
    assert not outcome.ok and outcome.result is None
    assert outcome.error.code == "NOT_FOUND" and not outcome.error.retryable
    with lab.bus.muted():      # bus coupé : l'appel aboutit, mais sans trace ni tailles
        untraced = execute_call(lab, "custom", "calculate_factorial", {"n": 3})
    assert untraced.ok and untraced.trace is None and untraced.request_bytes is None


@pytest.mark.parametrize("width", [80, CONSOLE_WIDTH])
def test_wire_report_fits_the_terminal_width(lab: LabRuntime, width: int) -> None:
    outcome = execute_call(lab, "grpc", "get_product_details", {"product_id": "SKU-1001"})
    screen = Screen(width)
    wire_report(screen.console, outcome.trace)
    assert max(len(line) for line in screen.text.splitlines()) <= width
    for expected in ("Retour à l'appelant", "Requête — gRPC / Protobuf · 15 octets", "Valeur décodée",
                     '"SKU-1001"', "sous-message · 36 octets", "width_cm"):
        assert expected in screen.text
    # Dans un terminal étroit, les colonnes de confort disparaissent plutôt que de tronquer les nombres.
    assert ("Part de l'appel" in screen.text) == (width >= 100)


def test_run_parallel_compares_sequential_and_simultaneous_calls(lab: LabRuntime) -> None:
    report = run_parallel(lab, "custom", "calculate_factorial", {"n": 5}, 6)
    assert report["count"] == 6
    assert report["sequential_errors"] == report["parallel_errors"] == 0
    assert report["sequential_ms"] > 0 and report["parallel_ms"] > 0 and report["speedup"] > 0
    screen = Screen()
    render_parallel(screen.console, report, "custom")
    assert "Synchrone" in screen.text and "Asynchrone" in screen.text and "Accélération" in screen.text


def test_interactive_menu_runs_a_call_and_quits(lab: LabRuntime) -> None:
    screen = Screen()
    # Appels RPC → JSON-RPC maison → calculate_factorial → n = 5 → synchrone → retour → quitter.
    answers = scripted("1", "2", "1", "5", "1", "0", "0")
    assert cli_runner.run_interactive(lab, console=screen.console, input_func=answers) == 0
    assert "Menu principal" in screen.text
    assert "calculate_factorial(n=5)" in screen.text
    assert '"result": "120"' in screen.text
    assert PROTOCOL_LABELS["custom"] in screen.text
    assert "Fermeture du laboratoire" in screen.text


def test_interactive_menu_reprompts_on_invalid_input_and_survives_end_of_input(lab: LabRuntime) -> None:
    screen = Screen()
    code = cli_runner.run_interactive(lab, console=screen.console, input_func=scripted("zzz", "42"))
    assert code == 0
    assert screen.text.count("Choix invalide") == 2
    assert "Fermeture du laboratoire" in screen.text


def test_interactive_under_the_hood_mode_prints_wire_messages(lab: LabRuntime) -> None:
    screen = Screen()
    # Sous le capot → appels RPC → REST → update_stock (valeurs par défaut) → synchrone → retour → quitter.
    answers = scripted("2", "1", "4", "3", "", "", "", "1", "0", "0")
    assert cli_runner.run_interactive(lab, console=screen.console, input_func=answers) == 0
    assert "Mode « Sous le capot » activé" in screen.text
    assert "Marshalling" in screen.text and "Ligne de requête" in screen.text
    assert "POST /api/stock/SKU-1001 HTTP/1.1" in screen.text


def test_interactive_ctrl_c_in_a_prompt_returns_to_the_menu(lab: LabRuntime) -> None:
    screen = Screen()
    # Ctrl+C pendant le choix du protocole : retour au menu, d'où « 0 » quitte.
    answers = scripted("1", KeyboardInterrupt, "0")
    assert cli_runner.run_interactive(lab, console=screen.console, input_func=answers) == 0
    assert "Saisie abandonnée" in screen.text
    assert screen.text.count("Menu principal") == 2


def test_interactive_network_preset_then_status_and_reset(lab: LabRuntime) -> None:
    screen = Screen()
    # Pannes → préréglage → WAN → retour ; état des serveurs ; réinitialiser (oui) ; quitter.
    answers = scripted("5", "2", "3", "0", "8", "9", "o", "0")
    assert cli_runner.run_interactive(lab, console=screen.console, input_func=answers) == 0
    assert "Internet (WAN) — latence 40 ms ± 8 ms" in screen.text
    assert f"{HOST}:{lab.ports.grpc_proxy}" in screen.text
    assert "Laboratoire réinitialisé" in screen.text
    assert lab.conditions.snapshot()["preset"] == "ideal"


def test_run_call_returns_2_on_rpc_error_and_0_on_success(lab: LabRuntime) -> None:
    screen = Screen()
    assert cli_runner.run_call(lab, "update_stock", ["SKU-1001", "-100000"], console=screen.console,
                               protocol="custom") == cli_runner.EXIT_RPC_ERROR
    assert "FAILED_PRECONDITION" in screen.text and "Stock insuffisant" in screen.text
    assert cli_runner.run_call(lab, "check_stock", ['["SKU-1001"]'], console=screen.console, protocol="grpc") == 0
    assert screen.text.count("▸ #") == 1


def test_run_serve_shows_servers_ports_and_proxies(lab: LabRuntime) -> None:
    screen = Screen()
    waited: list[bool] = []
    assert cli_runner.run_serve(lab, console=screen.console, wait=lambda: waited.append(True)) == 0
    assert waited == [True]
    for name in ("custom", "grpc", "rest", "custom_proxy", "grpc_proxy", "rest_proxy", "custom_v2", "grpc_v2"):
        assert f"{HOST}:{getattr(lab.ports, name)}" in screen.text
    assert "Proxys de chaos" in screen.text and "en ligne" in screen.text
    assert "Ctrl+C" in screen.text


def fake_dashboard(monkeypatch: pytest.MonkeyPatch, serve: Callable[..., None]) -> None:
    """Remplace ``dashboard.server`` : le vrai ``serve`` bloquerait jusqu'à Ctrl+C."""
    server = types.ModuleType("dashboard.server")
    server.serve = serve
    monkeypatch.setitem(sys.modules, "dashboard", types.ModuleType("dashboard"))
    monkeypatch.setitem(sys.modules, "dashboard.server", server)


def test_run_dashboard_serves_the_running_lab(lab: LabRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    fake_dashboard(monkeypatch, lambda runtime, **options: calls.append((runtime, options)))
    assert cli_runner.run_dashboard(lab, console=Screen().console, port=8765, open_browser=False) == 0
    assert calls == [(lab, {"host": lab.host, "port": 8765, "open_browser": False})]


def test_run_dashboard_restarts_the_lab_that_serve_stopped(lab: LabRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    ports = lab.ports

    def serve_then_stop(runtime: LabRuntime, **options: object) -> None:
        runtime.stop()      # comme le vrai serve() quand on le quitte par Ctrl+C
        raise KeyboardInterrupt

    fake_dashboard(monkeypatch, serve_then_stop)
    assert cli_runner.run_dashboard(lab, console=Screen().console, restart_lab=True) == 0
    assert lab.started and lab.ports == ports
    with lab.client("custom") as client:
        assert client.calculate_factorial(4)["result"] == "24"


def test_run_dashboard_reports_a_busy_port(lab: LabRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(runtime: LabRuntime, **options: object) -> None:
        raise RuntimeError("Le dashboard ne peut pas écouter sur 127.0.0.1:8000 : le port est déjà utilisé")

    fake_dashboard(monkeypatch, refuse)
    screen = Screen()
    assert cli_runner.run_dashboard(lab, console=screen.console) == cli_runner.EXIT_FAILURE
    assert "Le dashboard n'a pas pu démarrer" in screen.text and "déjà utilisé" in screen.text


def test_run_transparency_shows_the_four_snippets_with_line_counts(lab: LabRuntime) -> None:
    screen = Screen()
    assert cli_runner.run_transparency(lab, console=screen.console) == 0
    for title in ("Appel local", "RPC maison — stub JSON-RPC", "gRPC — stub généré", "REST à la main — http.client"):
        assert title in screen.text
    assert "3 lignes utiles" in screen.text and "19 lignes utiles" in screen.text
    assert "def update_stock_rest_by_hand" in screen.text


def test_run_failures_rejects_an_unknown_scenario(lab: LabRuntime) -> None:
    with pytest.raises(ValueError, match="Scénario inconnu"):
        cli_runner.run_failures(lab, console=Screen().console, scenario="nope")
