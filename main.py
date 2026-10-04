"""RPC Explorer & Benchmark Lab — point d'entrée.

    python main.py                         menu interactif
    python main.py --benchmark             banc d'essai complet          [--iterations N] [--quick] [--no-save]
    python main.py --simulate-failures     laboratoire de pannes         [--scenario ID|all] [--protocol P]
    python main.py --contract              ruptures de contrat v1 → v2
    python main.py --demo                  visite guidée « sous le capot » [--no-pause]
    python main.py --dashboard             dashboard web                 [--port N] [--no-browser]
    python main.py --serve                 tous les serveurs au premier plan
    python main.py --call METHOD [ARG…]    appel unitaire  [--protocol P] [--inspect] [--via-proxy] [--timeout S]

Chaque mode démarre le laboratoire (``LabRuntime``), s'exécute, puis l'arrête.
Codes de sortie : 0 succès · 1 échec (port occupé, scénario interrompu) ·
2 erreur d'appel RPC ou d'utilisation · 130 interruption au clavier.

Les ports se décalent en bloc avec la variable d'environnement ``RPCX_PORT_OFFSET``.
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import TYPE_CHECKING, Callable, NoReturn, Sequence

from cli.stdio import prepare_stdio
from common.config import APP_NAME, APP_TAGLINE, PORT_OFFSET, PROTOCOLS, REMOTE_PROTOCOLS, VERSION

if TYPE_CHECKING:
    from rich.console import Console

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

MAX_BENCHMARK_ITERATIONS = 1_000_000
MAX_PORT = 65_535

# Modes exclusifs (attributs de l'espace de noms d'argparse) ; sans aucun d'eux : menu interactif.
MODES: tuple[str, ...] = ("benchmark", "simulate_failures", "contract", "demo", "dashboard", "serve", "call")
INTERACTIVE = "interactive"

# Chaque option complémentaire n'a de sens qu'avec certains modes : (attribut, option, modes acceptés).
_MODE_OPTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("iterations", "--iterations", ("benchmark",)),
    ("quick", "--quick", ("benchmark",)),
    ("no_save", "--no-save", ("benchmark",)),
    ("scenario", "--scenario", ("simulate_failures",)),
    ("protocol", "--protocol", ("simulate_failures", "call")),
    ("no_pause", "--no-pause", ("demo",)),
    ("port", "--port", ("dashboard",)),
    ("no_browser", "--no-browser", ("dashboard",)),
    ("inspect", "--inspect", ("call",)),
    ("via_proxy", "--via-proxy", ("call",)),
    ("timeout", "--timeout", ("call",)),
)
_MODE_FLAGS: dict[str, str] = {mode: "--" + mode.replace("_", "-") for mode in MODES}

# argparse ne parle qu'anglais : ses messages d'erreur les plus courants sont reformulés en français.
_ARGPARSE_MESSAGES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^unrecognized arguments: (?P<rest>.+)$"), r"option ou argument inconnu : \g<rest>"),
    (re.compile(r"^argument (?P<option>\S+): expected one argument$"), r"l’option \g<option> attend une valeur"),
    (re.compile(r"^argument (?P<option>\S+): expected at least one argument$"),
     r"l’option \g<option> attend au moins une valeur"),
    (re.compile(r"^argument (?P<option>\S+): not allowed with argument (?P<other>\S+)$"),
     r"les options \g<option> et \g<other> ne peuvent pas être utilisées ensemble"),
    (re.compile(r"^argument (?P<option>\S+): invalid choice: (?P<value>.+) \(choose from (?P<choices>.+)\)$"),
     r"l’option \g<option> n’accepte pas \g<value> (valeurs possibles : \g<choices>)"),
    (re.compile(r"^argument (?P<option>\S+): (?P<rest>.+)$"), r"option \g<option> : \g<rest>"),
)

_EPILOG = """\
Exemples :
  python main.py --call update_stock SKU-1001 -3 --protocol grpc --inspect
  python main.py --call list_products limit=3 category=Audio --protocol rest
  python main.py --call stream_analytics 5 100
  python main.py --benchmark --quick
  python main.py --simulate-failures --scenario duplicate_execution --protocol rest

Les ports du laboratoire se décalent en bloc avec la variable d’environnement RPCX_PORT_OFFSET.
"""


# --- Ligne de commande -------------------------------------------------------

class _FrenchHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None) -> None:   # type: ignore[no-untyped-def]
        super().add_usage(usage, actions, groups, "Utilisation : " if prefix is None else prefix)


class _FrenchArgumentParser(argparse.ArgumentParser):
    """Analyseur dont les erreurs d'utilisation sont dites en français (code de sortie 2, comme argparse)."""

    def error(self, message: str) -> NoReturn:
        for pattern, replacement in _ARGPARSE_MESSAGES:
            if pattern.match(message):
                message = pattern.sub(replacement, message)
                break
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"Erreur : {message}\nAide complète : python main.py --help\n")


def _bounded_int(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"« {text} » n’est pas un nombre entier") from None
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(f"attendu : un entier compris entre {minimum} et {maximum}")
        return value

    return parse


def _positive_seconds(text: str) -> float:
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        value = 0.0
    if not 0 < value <= 3600:
        raise argparse.ArgumentTypeError("attendu : une durée en secondes, strictement positive (par exemple 2.5)")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Analyseur de la ligne de commande (plan §12)."""
    parser = _FrenchArgumentParser(
        prog="python main.py",
        usage="python main.py [MODE] [OPTIONS]",
        description=f"{APP_NAME} · {APP_TAGLINE} — démonstrateur pédagogique du Remote Procedure Call.\n"
                    "Sans option : menu interactif.",
        epilog=_EPILOG,
        formatter_class=_FrenchHelpFormatter,
        add_help=False,
        allow_abbrev=False,
    )
    modes = parser.add_argument_group("Modes (un seul à la fois) ").add_mutually_exclusive_group()
    modes.add_argument("--benchmark", action="store_true",
                       help="banc d’essai complet : tailles JSON vs Protobuf, temps par appel, local vs distant")
    modes.add_argument("--simulate-failures", action="store_true",
                       help="laboratoire de pannes : latence, échéance, coupure, panne du serveur, doublon")
    modes.add_argument("--contract", action="store_true",
                       help="ruptures de contrat : un client v1 face aux serveurs v2")
    modes.add_argument("--demo", action="store_true",
                       help="visite guidée « sous le capot » : le même appel en JSON-RPC maison, gRPC puis REST")
    modes.add_argument("--dashboard", action="store_true", help="dashboard web (FastAPI + WebSocket)")
    modes.add_argument("--serve", action="store_true",
                       help="lance tous les serveurs au premier plan, jusqu’à Ctrl+C")
    modes.add_argument("--call", nargs="+", metavar=("METHOD", "ARG"),
                       help="appel unitaire : la procédure, puis ses arguments dans l’ordre du catalogue "
                            "(JSON accepté, nom=valeur possible ; un argument omis prend sa valeur par défaut)")

    benchmark = parser.add_argument_group("Options de --benchmark ")
    benchmark.add_argument("--iterations", type=_bounded_int(1, MAX_BENCHMARK_ITERATIONS), metavar="N",
                           help="nombre d’appels chronométrés par protocole")
    benchmark.add_argument("--quick", action="store_true", help="version courte : moins d’appels, balayage réduit")
    benchmark.add_argument("--no-save", action="store_true", help="n’enregistre pas le rapport dans reports/")

    failures = parser.add_argument_group("Options de --simulate-failures ")
    failures.add_argument("--scenario", metavar="ID",
                          help="scénario à jouer : latency_trap, timeout_spike, connection_cut, server_outage, "
                               "duplicate_execution, ou all (par défaut)")

    shared = parser.add_argument_group("Option de --simulate-failures et de --call ")
    shared.add_argument("--protocol", choices=PROTOCOLS, metavar="P",
                        help=f"protocole : {', '.join(PROTOCOLS)} (par défaut : custom ; local est réservé à --call)")

    demo = parser.add_argument_group("Option de --demo ")
    demo.add_argument("--no-pause", action="store_true", help="déroule la visite sans s’arrêter entre les étapes")

    dashboard = parser.add_argument_group("Options de --dashboard ")
    dashboard.add_argument("--port", type=_bounded_int(1, MAX_PORT), metavar="N",
                           help="port HTTP du dashboard (par défaut : 8000 + RPCX_PORT_OFFSET)")
    dashboard.add_argument("--no-browser", action="store_true", help="n’ouvre pas le navigateur")

    call = parser.add_argument_group("Options de --call ")
    call.add_argument("--inspect", action="store_true",
                      help="ajoute le pipeline de l’appel et les octets échangés (« sous le capot »)")
    call.add_argument("--via-proxy", action="store_true",
                      help="fait passer l’appel par le proxy de chaos plutôt qu’en direct")
    call.add_argument("--timeout", type=_positive_seconds, metavar="S", help="échéance de l’appel, en secondes")

    general = parser.add_argument_group("Général ")
    general.add_argument("--version", action="version", version=f"{APP_NAME} · {APP_TAGLINE} {VERSION}",
                         help="affiche la version et quitte")
    general.add_argument("-h", "--help", action="help", help="affiche cette aide et quitte")
    return parser


def resolve_mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """Mode demandé ; refuse (code 2) une option complémentaire donnée sans le mode auquel elle appartient."""
    mode = next((name for name in MODES if getattr(args, name)), INTERACTIVE)
    for attribute, flag, accepted in _MODE_OPTIONS:
        if getattr(args, attribute) in (None, False) or mode in accepted:
            continue
        owners = " ou ".join(_MODE_FLAGS[name] for name in accepted)
        parser.error(f"l’option {flag} ne s’utilise qu’avec {owners}")
    if mode == "simulate_failures" and args.protocol == "local":
        parser.error(
            f"un scénario de panne a besoin d’un réseau à dérégler : --protocol {', '.join(REMOTE_PROTOCOLS)}"
        )
    return mode


# --- Exécution ---------------------------------------------------------------

def _run(mode: str, args: argparse.Namespace, console: "Console") -> int:
    """Démarre le laboratoire, exécute le mode demandé, puis arrête tout — quoi qu'il arrive."""
    from cli.theme import activity, failure_panel

    try:
        # Régénère les stubs gRPC si un .proto a changé, avant que ``lab`` n'importe le code généré.
        from rpc_grpc.generate import ensure_generated

        ensure_generated()
    except RuntimeError as error:
        console.print(failure_panel("Les contrats .proto n’ont pas pu être compilés", str(error)))
        return EXIT_FAILURE

    from cli import cli_runner
    from lab import PORT_OFFSET_STEP, LabRuntime

    runtime = LabRuntime()
    try:
        with activity(console, "Démarrage du laboratoire…"):
            runtime.start()
    except RuntimeError as error:
        console.print(failure_panel(
            "Le laboratoire n’a pas pu démarrer",
            str(error),
            f"Décalage actuel : RPCX_PORT_OFFSET={PORT_OFFSET}. Sous PowerShell : "
            f"$env:RPCX_PORT_OFFSET = \"{PORT_OFFSET + PORT_OFFSET_STEP}\" ; puis relancez la commande.",
        ))
        return EXIT_FAILURE
    try:
        if mode == "benchmark":
            return cli_runner.run_benchmark(
                runtime, console=console, save=not args.no_save,
                config=cli_runner.benchmark_config(quick=args.quick, iterations=args.iterations),
            )
        if mode == "simulate_failures":
            return cli_runner.run_failures(
                runtime, console=console, scenario=args.scenario or cli_runner.ALL_SCENARIOS,
                protocol=args.protocol or "custom",
            )
        if mode == "contract":
            return cli_runner.run_contract(runtime, console=console)
        if mode == "demo":
            return cli_runner.run_demo(runtime, console=console, pause=not args.no_pause)
        if mode == "dashboard":
            return cli_runner.run_dashboard(
                runtime, console=console, port=args.port, open_browser=not args.no_browser
            )
        if mode == "serve":
            return cli_runner.run_serve(runtime, console=console)
        if mode == "call":
            method, *arguments = args.call
            return cli_runner.run_call(
                runtime, method, arguments, console=console, protocol=args.protocol or "custom",
                inspect=args.inspect, via_proxy=args.via_proxy, timeout=args.timeout,
            )
        return cli_runner.run_interactive(runtime, console=console)
    finally:
        runtime.stop()


def main(argv: Sequence[str] | None = None) -> int:
    prepare_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = resolve_mode(parser, args)

    # Imports tardifs : ``--version`` et ``--help`` répondent sans charger Rich ni les middlewares.
    from rich.text import Text

    from cli.calls import build_params
    from cli.failures_view import ALL_SCENARIOS, failure_scenarios
    from cli.theme import MUTED, make_console

    # Ce qui peut être refusé sans démarrer le laboratoire l'est tout de suite.
    try:
        if mode == "call":
            build_params(args.call[0], args.call[1:])
        if mode == "simulate_failures":
            failure_scenarios(args.scenario or ALL_SCENARIOS)
    except ValueError as error:
        parser.error(str(error))

    console = make_console()
    try:
        return _run(mode, args, console)
    except KeyboardInterrupt:
        console.print()
        console.print(Text("  Interrompu au clavier (Ctrl+C) — le laboratoire est arrêté.", style=MUTED))
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
