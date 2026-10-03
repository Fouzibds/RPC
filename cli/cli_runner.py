"""Les modes du programme : une fonction ``run_*`` par option de ``main.py``.

Chaque fonction reçoit un ``LabRuntime`` DÉJÀ démarré, affiche sur une console
Rich et renvoie un code de sortie — 0 si tout s'est passé comme prévu. Aucune
ne démarre ni n'arrête le laboratoire : ``main.py`` s'en charge pour les modes
directs, et le menu interactif enchaîne ces mêmes fonctions sur un laboratoire
qui reste ouvert.

Codes de sortie : 0 succès · 1 échec du programme (scénario interrompu, issue
inattendue) · 2 erreur d'appel RPC ou d'utilisation.
"""
from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from benchmark_lab.benchmark_perf import DEFAULT_CONFIG, QUICK_CONFIG, resolve_config, run_full_benchmark
from benchmark_lab.contract_evolution import CONTRACT_SCENARIOS, contract_overview, run_contract_scenario
from benchmark_lab.failure_simulation import run_scenario
from benchmark_lab.report import fr_duration, fr_number
from benchmark_lab.transparency_demo import code_comparison, run_comparison
from common.config import PROTOCOL_LABELS, PROTOCOLS
from common.errors import RpcError

from .benchmark_view import BenchmarkProgress, render_report, save_report_files
from .calls import (
    build_params,
    describe_call,
    execute_call,
    render_outcome,
    require_method,
    stream_item_line,
)
from .contract_view import render_changes, render_diff, render_rules, render_tally, scenario_panel
from .failures_view import (
    ALL_SCENARIOS,
    TimelinePrinter,
    campaign_summary,
    failure_scenarios,
    scenario_heading,
    verdict_panel,
)
from .status_view import activity_view, render_status, servers_table
from .theme import (
    ACCENT,
    MARK_INFO,
    MUTED,
    PROMPT,
    activity,
    failure_panel,
    header,
    hint,
    is_live,
    is_narrow,
    make_console,
    note,
    section,
)
from .tour import run_tour
from .transparency_view import render_runs, render_snippets

if TYPE_CHECKING:
    from lab import LabRuntime

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_RPC_ERROR = 2

_SERVE_REFRESH_S = 1.0

InputFunc = Callable[[str], str]


def _title(console: Console, banner: bool, title: str, detail: str = "") -> None:
    """Titre d'un écran : l'en-tête de marque en mode direct, un simple filet dans le menu interactif."""
    if banner:
        header(console, title, detail)
    else:
        section(console, title, detail)


# --- Banc d'essai ------------------------------------------------------------

def benchmark_config(*, quick: bool = False, iterations: int | None = None) -> dict[str, Any]:
    """Configuration du banc d'essai pour ``--benchmark [--quick] [--iterations N]``."""
    config = dict(QUICK_CONFIG if quick else DEFAULT_CONFIG)
    if iterations is not None:
        config["iterations"] = iterations
    return config


def run_benchmark(
    runtime: "LabRuntime",
    *,
    console: Console | None = None,
    config: Mapping[str, Any] | None = None,
    save: bool = True,
    banner: bool = True,
) -> int:
    """Banc d'essai complet : progression en direct, tableaux comparatifs, faits marquants, rapport."""
    console = console or make_console()
    try:
        settings = resolve_config(config)
    except ValueError as error:
        console.print(failure_panel("Configuration du banc d'essai refusée", str(error)))
        return EXIT_USAGE
    sweep = ", ".join(fr_number(latency) for latency in settings["sweep_latencies_ms"])
    _title(
        console, banner,
        "Banc d'essai — ce qu'un appel distant coûte, en octets et en temps",
        f"{settings['method']} · {fr_number(settings['iterations'])} appels par protocole · "
        f"balayage réseau à {sweep} ms",
    )
    console.print()
    try:
        with BenchmarkProgress(console) as progress:
            report = run_full_benchmark(runtime, settings, progress)
    except (RuntimeError, ValueError) as error:
        console.print(failure_panel("Banc d'essai interrompu", str(error)))
        return EXIT_FAILURE
    note(console, f"Mesures terminées en {fr_duration(progress.elapsed_s * 1000)}.")
    render_report(console, report)
    console.print()
    if save:
        json_path, markdown_path = save_report_files(report)
        note(console, f"Rapport enregistré : {json_path}")
        hint(console, f"  et sa version Markdown : {markdown_path}")
    else:
        hint(console, f"{MARK_INFO} Rapport non enregistré.")
    return EXIT_OK


# --- Laboratoire de pannes ---------------------------------------------------

def run_failures(
    runtime: "LabRuntime",
    *,
    console: Console | None = None,
    scenario: str = ALL_SCENARIOS,
    protocol: str = "custom",
    banner: bool = True,
) -> int:
    """Joue les scénarios de panne : chronologie en direct, puis verdict, mesures et leçon de chacun."""
    console = console or make_console()
    chosen = failure_scenarios(scenario)
    _title(
        console, banner,
        "Laboratoire de pannes — un appel distant n'est pas un appel local",
        f"Middleware mis à l'épreuve : {PROTOCOL_LABELS[protocol]} · chaque appel traverse le proxy de chaos",
    )
    printer = TimelinePrinter(console)
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        for rank, meta in enumerate(chosen, start=1):
            scenario_heading(console, rank, meta, protocol)
            with activity(console, "Scénario en cours…"):
                result = run_scenario(runtime, meta["id"], protocol=protocol, on_step=printer)
            console.print()
            console.print(verdict_panel(result, narrow=is_narrow(console)))
            results.append(result)
    except (RpcError, RuntimeError) as error:
        console.print()
        console.print(failure_panel(
            "Scénario interrompu", str(error),
            "Réseau, stocks et proxy ont été remis dans l'état où le scénario les a trouvés.",
        ))
        return EXIT_FAILURE
    campaign_summary(console, results, time.perf_counter() - started)
    return EXIT_OK


# --- Laboratoire de contrat --------------------------------------------------

def run_contract(
    runtime: "LabRuntime",
    *,
    console: Console | None = None,
    scenario: str | None = None,
    banner: bool = True,
) -> int:
    """Laboratoire de contrat ; renvoie 1 si l'issue d'un scénario n'est pas celle attendue.

    Sans ``scenario`` : le diff des deux contrats, les changements classés, tous
    les scénarios, puis les règles d'or. Avec ``scenario`` : ce scénario seul.
    """
    console = console or make_console()
    overview = contract_overview()
    stats = overview["stats"]
    _title(
        console, banner,
        "Laboratoire de contrat — le serveur est passé en v2, les clients sont restés en v1",
        f"{stats['breaking']} changements cassants et {stats['compatible']} évolutions compatibles "
        "séparent les deux versions",
    )
    if scenario is not None:
        known = [item["id"] for item in CONTRACT_SCENARIOS]
        if scenario not in known:
            raise ValueError(f"Scénario de contrat inconnu : « {scenario} » (disponibles : {', '.join(known)})")
        console.print()
        return _play_contract_scenarios(console, runtime, [scenario])

    section(console, "Le diff des contrats", "Les deux fichiers .proto, ligne à ligne.", number=1)
    render_diff(console, overview)
    section(console, "Ce qui a changé", "Protobuf (calculé à partir des deux descripteurs), puis JSON-RPC — "
            "qui n'a pas d'IDL : rien n'y annonce une rupture.", number=2)
    render_changes(console, overview["changes"])
    section(console, "Un client v1 face aux serveurs v2",
            "Pour chaque appel : ce qu'un serveur v1 aurait fait, ce qui s'est réellement passé, et le verdict.",
            number=3)
    code = _play_contract_scenarios(console, runtime, [item["id"] for item in CONTRACT_SCENARIOS])
    section(console, "Règles d'or", "Faire évoluer un contrat sans casser les clients déjà déployés.", number=4)
    render_rules(console, overview["rules"])
    return code


def _play_contract_scenarios(console: Console, runtime: "LabRuntime", scenario_ids: Sequence[str]) -> int:
    """Joue les scénarios un à un : un panneau par scénario, puis le bilan par issue."""
    results: list[dict[str, Any]] = []
    try:
        for rank, scenario_id in enumerate(scenario_ids, start=1):
            result = run_contract_scenario(runtime, scenario_id)
            console.print(scenario_panel(result, rank))
            results.append(result)
    except (RpcError, RuntimeError) as error:
        console.print(failure_panel("Scénario de contrat interrompu", str(error)))
        return EXIT_FAILURE
    console.print()
    render_tally(console, results)
    return EXIT_OK if all(result["matches"] for result in results) else EXIT_FAILURE


# --- Visite guidée -----------------------------------------------------------

def run_demo(
    runtime: "LabRuntime",
    *,
    console: Console | None = None,
    pause: bool = True,
    input_func: InputFunc | None = None,
) -> int:
    """Visite guidée « sous le capot ». Sans terminal (ou ``pause=False``), elle se déroule d'une traite."""
    console = console or make_console()
    read = input_func or input
    pausing = pause and (input_func is not None or (is_live(console) and sys.stdin.isatty()))

    def wait(message: str) -> None:
        nonlocal pausing
        if not pausing:
            return
        console.print()
        console.print(Text.assemble((f"  {message} ", MUTED), (f"{PROMPT} ", ACCENT)), end="")
        try:
            read("")
        except EOFError:
            pausing = False     # plus personne au clavier : la visite continue sans s'arrêter
            console.print()

    outcomes = run_tour(runtime, console, pause=wait if pausing else None)
    return EXIT_OK if all(outcome.ok for outcome in outcomes.values()) else EXIT_FAILURE


# --- Appel unitaire ----------------------------------------------------------

def run_call(
    runtime: "LabRuntime",
    method: str,
    arguments: Sequence[str] = (),
    *,
    console: Console | None = None,
    protocol: str = "custom",
    inspect: bool = False,
    via_proxy: bool = False,
    timeout: float | None = None,
) -> int:
    """Un appel : résultat en JSON coloré, durée et tailles ; code 2 si le stub lève une ``RpcError``."""
    console = console or make_console()
    try:
        spec = require_method(method)
        params = build_params(method, arguments)
        if protocol not in PROTOCOLS:
            raise ValueError(f"Protocole inconnu : « {protocol} » (attendu : {', '.join(PROTOCOLS)})")
    except ValueError as error:
        console.print(failure_panel("Appel impossible", str(error)))
        return EXIT_USAGE
    route = " à travers le proxy de chaos" if via_proxy and protocol != "local" else ""
    header(console, f"Appel unitaire — {PROTOCOL_LABELS[protocol]}{route}", describe_call(method, params))
    console.print()

    def show_item(rank: int, elapsed_ms: float, item: Any) -> None:
        console.print(stream_item_line(rank, elapsed_ms, item, console.width))

    outcome = execute_call(
        runtime, protocol, method, params, via_proxy=via_proxy, timeout=timeout, on_item=show_item
    )
    render_outcome(console, outcome, inspect=inspect)
    if not outcome.ok and protocol not in spec.protocols:
        # L'erreur vient du stub, comme pour tout appel ; on dit seulement où cette procédure existe.
        hint(console, f"{method} n'est exposée que par : {', '.join(spec.protocols)} "
                      f"(par exemple --protocol {spec.protocols[-1]}).")
    return EXIT_OK if outcome.ok else EXIT_RPC_ERROR


# --- Transparence de localisation --------------------------------------------

def run_transparency(runtime: "LabRuntime", *, console: Console | None = None, banner: bool = True) -> int:
    """Les quatre écritures du même appel, côte à côte, puis exécutées ; 1 si elles ne sont pas équivalentes."""
    console = console or make_console()
    _title(console, banner, "Transparence de localisation — le même appel, écrit de quatre façons")
    comparison = code_comparison()
    render_snippets(console, comparison)
    console.print()
    outcome = run_comparison(runtime)
    render_runs(console, comparison, outcome)
    return EXIT_OK if outcome["equivalent"] else EXIT_FAILURE


# --- Serveurs au premier plan ------------------------------------------------

def _wait_for_interrupt() -> None:
    """Bloque jusqu'à Ctrl+C. Une attente courte en boucle : sous Windows, un long sommeil retarde le signal."""
    while True:
        time.sleep(0.5)


def run_serve(
    runtime: "LabRuntime",
    *,
    console: Console | None = None,
    wait: Callable[[], None] | None = None,
) -> int:
    """Garde le laboratoire en marche et affiche son état jusqu'à Ctrl+C.

    Dans un terminal, le tableau se rafraîchit chaque seconde (compteurs d'appels,
    octets relayés) ; redirigé, il est écrit une seule fois. ``wait`` remplace
    l'attente (tests) : la fonction rend la main quand elle se termine.
    """
    console = console or make_console()
    header(console, "Serveurs au premier plan — les trois middlewares, leurs proxys de chaos, les serveurs v2")
    footer = Text(f"  Ctrl+C pour arrêter les serveurs {PROMPT}", style=MUTED)
    try:
        if wait is None and is_live(console):
            # Les ports ne changent pas : affichés une fois. Seul le trafic est redessiné sur place.
            narrow = is_narrow(console)
            console.print(servers_table(runtime.status(), narrow=narrow))
            with Live(Group(activity_view(runtime, narrow=narrow), footer), console=console,
                      auto_refresh=False) as live:
                while True:
                    time.sleep(_SERVE_REFRESH_S)
                    live.update(Group(activity_view(runtime, narrow=narrow), footer), refresh=True)
        else:
            render_status(console, runtime)
            console.print(footer)
            (wait or _wait_for_interrupt)()
    except KeyboardInterrupt:
        pass    # Ctrl+C est la façon normale de quitter ce mode
    console.print()
    note(console, "Arrêt demandé : fermeture des serveurs et des proxys.")
    return EXIT_OK


# --- Dashboard web -----------------------------------------------------------

def run_dashboard(
    runtime: "LabRuntime",
    *,
    console: Console | None = None,
    port: int | None = None,
    open_browser: bool = True,
    restart_lab: bool = False,
) -> int:
    """Sert le dashboard web sur le laboratoire en marche, jusqu'à Ctrl+C.

    ``dashboard.server.serve`` affiche sa propre bannière (adresse, serveurs) et
    ARRÊTE le laboratoire en sortant. ``restart_lab`` le relance ensuite : c'est
    ce qu'il faut au menu interactif, qui continue après la fermeture du dashboard.
    ``port=None`` : le port du plan d'adressage (8000 + ``RPCX_PORT_OFFSET``).
    """
    console = console or make_console()
    try:
        # Import tardif : FastAPI et uvicorn ne sont chargés que si l'on ouvre le dashboard.
        from dashboard.server import serve
    except ImportError as error:
        console.print(failure_panel(
            "Dashboard indisponible",
            f"Le module dashboard.server n'a pas pu être chargé : {error}",
            "Vérifiez que les dépendances sont installées (pip install -r requirements.txt).",
        ))
        return EXIT_FAILURE
    try:
        serve(runtime, host=runtime.host, port=port, open_browser=open_browser)
    except KeyboardInterrupt:
        pass    # Ctrl+C est la façon normale de fermer le dashboard
    except (OSError, RuntimeError) as error:
        console.print(failure_panel("Le dashboard n'a pas pu démarrer", str(error)))
        return EXIT_FAILURE
    finally:
        if restart_lab and not runtime.started:
            runtime.start()
    return EXIT_OK


# --- Menu interactif ---------------------------------------------------------

def run_interactive(
    runtime: "LabRuntime",
    *,
    console: Console | None = None,
    input_func: InputFunc | None = None,
) -> int:
    """Menu interactif. ``input_func(invite) -> saisie`` remplace ``input`` (tests, démonstrations scriptées)."""
    from .interactive import InteractiveSession   # import tardif : ce module importe celui-ci

    return InteractiveSession(runtime, console or make_console(), input_func).run()


__all__ = [
    "ALL_SCENARIOS",
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_RPC_ERROR",
    "EXIT_USAGE",
    "benchmark_config",
    "failure_scenarios",
    "run_benchmark",
    "run_call",
    "run_contract",
    "run_dashboard",
    "run_demo",
    "run_failures",
    "run_interactive",
    "run_serve",
    "run_transparency",
]
