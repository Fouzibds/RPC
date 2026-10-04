"""Menu interactif : tout le laboratoire au clavier, sur un ``LabRuntime`` qui reste ouvert.

Le menu ne fait qu'enchaîner les fonctions ``run_*`` de ``cli_runner`` et les
appels de ``calls`` : ce qu'on y voit est exactement ce qu'affichent les options
de ``main.py``.

Saisie : un choix invalide est redemandé ; Entrée garde la valeur proposée entre
crochets ; Ctrl+C (ou la fin de l'entrée) abandonne la saisie en cours et ramène
au menu principal — d'où il fait quitter proprement.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Sequence

from rich import box
from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from benchmark_lab.benchmark_perf import DEFAULT_CONFIG, MAX_ITERATIONS, QUICK_CONFIG
from benchmark_lab.contract_evolution import CONTRACT_SCENARIOS
from benchmark_lab.failure_simulation import SCENARIOS
from benchmark_lab.report import fr_compact
from common.catalog import METHODS, MethodSpec, ParamSpec
from common.config import DEFAULT_TIMEOUT_S, PROTOCOL_LABELS, PROTOCOL_TRANSPORTS, PROTOCOLS, REMOTE_PROTOCOLS
from common.errors import RpcError
from netsim import ARM_KINDS, PRESET_INFO, PRESETS

from . import cli_runner
from .calls import (
    STREAM_KINDS,
    describe_call,
    execute_call,
    render_outcome,
    render_parallel,
    run_parallel,
    stream_item_line,
)
from .status_view import network_summary, render_status
from .theme import (
    ACCENT,
    BORDER,
    DANGER,
    MARK_FAIL,
    MARK_OK,
    MARK_WARN,
    MUTED,
    PROMPT,
    SUBTLE,
    SUCCESS,
    WARNING,
    activity,
    compact_json,
    failure_panel,
    header,
    hint,
    note,
    protocol_text,
    section,
)

if TYPE_CHECKING:
    from lab import LabRuntime

RESULT_LINES = 40             # au-delà, un résultat est abrégé : le menu doit rester à l'écran
DEFAULT_PARALLEL_CALLS = 10
MAX_PARALLEL_CALLS = 200
_BACK_ANSWERS = frozenset({"0", "q"})
_YES_ANSWERS = frozenset({"o", "oui", "y", "yes"})
_NO_ANSWERS = frozenset({"n", "non", "no"})

_STRUCTURED_TYPES = frozenset({"updates", "product_ids"})    # paramètres saisis en JSON (listes)

_KIND_LABELS: dict[str, str] = {
    "unary": "appel unaire",
    "server_stream": "flux serveur",
    "client_stream": "flux client",
    "bidi_stream": "flux bidirectionnel",
}
_ARM_LABELS: dict[str, tuple[str, str]] = {
    "reset": ("Coupure", "la prochaine requête coupe la connexion sans atteindre le serveur"),
    "lost_reply": ("Réponse perdue", "le serveur exécute la prochaine requête, mais sa réponse n’arrive jamais"),
}
# Champs de NetworkConditions proposés à la saisie : (nom, libellé, booléen ?).
_NETWORK_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("latency_ms", "Latence aller-retour (ms)", False),
    ("jitter_ms", "Gigue, en plus ou en moins (ms)", False),
    ("spike_probability", "Probabilité qu’une requête subisse un pic (0 à 1)", False),
    ("spike_ms", "Durée d’un pic (ms)", False),
    ("reset_probability", "Probabilité de coupure à l’arrivée d’une requête (0 à 1)", False),
    ("bandwidth_kbps", "Débit maximal (kbit/s, 0 = illimité)", False),
    ("blackhole", "Trou noir : les octets partent, rien ne revient", True),
    ("down", "Serveur injoignable", True),
)


class Cancelled(Exception):
    """Saisie abandonnée : Ctrl+C, ou fin de l'entrée standard."""


@dataclass(frozen=True)
class _Entry:
    """Une ligne du menu principal."""

    title: str
    description: Callable[[], str]
    action: Callable[[], None]


class InteractiveSession:
    """Une séance de menu : la console, la saisie, et les préférences qui durent d'un écran à l'autre."""

    def __init__(self, runtime: "LabRuntime", console: Console, input_func: Callable[[str], str] | None = None) -> None:
        self.runtime = runtime
        self.console = console
        self.protocol = "custom"
        self.under_the_hood = False
        self._read = input_func or input
        # Saisie scriptée ou redirigée : rien ne l'affiche à l'écran, on la recopie pour que la sortie se lise.
        self._echo = input_func is not None or not sys.stdin.isatty()
        self._entries: tuple[_Entry, ...] = (
            _Entry("Appels RPC", lambda: "synchrone · asynchrone × N · flux", self._calls_screen),
            _Entry("Sous le capot", self._under_the_hood_state, self._toggle_under_the_hood),
            _Entry("Visite guidée", lambda: "un appel, trois middlewares, octet par octet", self._tour_screen),
            _Entry("Banc d’essai", lambda: "tailles, latences, local vs distant", self._benchmark_screen),
            _Entry("Laboratoire de pannes", self._network_state, self._failures_screen),
            _Entry("Laboratoire de contrat", lambda: "un client v1 face aux serveurs v2", self._contract_screen),
            _Entry("Transparence de localisation", lambda: "le même appel écrit de quatre façons",
                   self._transparency_screen),
            _Entry("État des serveurs", lambda: "ports, proxys, trafic, inventaire", self._status_screen),
            _Entry("Réinitialiser le laboratoire", lambda: "stocks, réseau, compteurs et traces", self._reset_screen),
            _Entry("Ouvrir le dashboard web", lambda: "le même laboratoire, dans le navigateur",
                   self._dashboard_screen),
        )

    # -- boucle principale ----------------------------------------------------

    def run(self) -> int:
        header(
            self.console,
            "Menu interactif — explorer, mesurer et casser un appel de procédure distant",
            "Entrée valide la valeur entre crochets · Ctrl+C revient au menu",
        )
        while True:
            self._show_menu()
            try:
                choice = self._choose(len(self._entries))
            except Cancelled:
                break
            if choice is None:
                break
            try:
                self._entries[choice].action()
            except Cancelled:
                hint(self.console, "↩ Saisie abandonnée — retour au menu principal.")
            except KeyboardInterrupt:
                self.console.print()
                note(self.console, "Interrompu — retour au menu principal.", mark=MARK_WARN, color=WARNING)
            except (RpcError, RuntimeError, ValueError, OSError) as error:
                # Une action qui échoue (serveur arrêté, port repris) ne doit pas emporter le menu avec elle.
                self.console.print(failure_panel("Cette action a échoué", str(error), "Retour au menu principal."))
        self.console.print()
        note(self.console, "Fermeture du laboratoire. À bientôt.")
        return cli_runner.EXIT_OK

    def _show_menu(self) -> None:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(justify="right", style=f"bold {ACCENT}", no_wrap=True)
        grid.add_column(style="bold", no_wrap=True)
        grid.add_column(style=MUTED)
        for number, entry in enumerate(self._entries, start=1):
            grid.add_row(str(number), entry.title, entry.description())
        grid.add_row("0", Text("Quitter", style=MUTED), "")
        self.console.print()
        self.console.print(Panel(
            grid, title=Text("Menu principal", style=f"bold {ACCENT}"), title_align="left",
            box=box.ROUNDED, border_style=BORDER, padding=(0, 1), expand=False,
        ))

    def _under_the_hood_state(self) -> str:
        state = "ACTIVÉ" if self.under_the_hood else "désactivé"
        return f"{state} — afficher le pipeline et les octets bruts de chaque appel"

    def _network_state(self) -> str:
        return f"scénarios guidés · réseau : {network_summary(self.runtime.conditions.snapshot()).plain}"

    # -- saisie ---------------------------------------------------------------

    def _ask(self, label: str, default: str | None = None, *, detail: str = "") -> str:
        """Une ligne de saisie ; Entrée seule rend ``default``. Lève ``Cancelled`` sur Ctrl+C ou fin d'entrée."""
        prompt = Text.assemble(
            (f"  {label}", "bold"), (f" — {detail}" if detail else "", SUBTLE),
            (f" [{default}]" if default else "", MUTED), (f" {PROMPT} ", ACCENT),
        )
        self.console.print(prompt, end="")
        try:
            answer = self._read("").strip()
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            raise Cancelled from None
        if self._echo:
            self.console.print(Text(answer, style=SUBTLE))
        return answer or (default or "")

    def _retry(self, message: str) -> None:
        self.console.print(Text(f"  {MARK_FAIL} {message}", style=DANGER))

    def _choose(self, count: int, default: int | None = None, label: str = "Votre choix") -> int | None:
        """Numéro d'une option parmi ``count`` → son index, ou ``None`` pour 0 (retour).

        Une saisie invalide est expliquée puis redemandée.
        """
        while True:
            answer = self._ask(label, None if default is None else str(default + 1)).lower()
            if answer in _BACK_ANSWERS:
                return None
            if answer.isdecimal() and 1 <= int(answer) <= count:
                return int(answer) - 1
            self._retry(f"Choix invalide : « {answer} ». Saisissez un numéro de 1 à {count}, ou 0 pour revenir.")

    def _pick(
        self,
        title: str,
        options: Sequence[tuple[RenderableType, str]],
        *,
        default: int | None = None,
        back: str = "Retour",
    ) -> int | None:
        """Affiche une liste numérotée ``(libellé, précision)`` et rend l'index choisi, ou ``None`` pour revenir."""
        grid = Table.grid(padding=(0, 2))
        grid.add_column(justify="right", style=f"bold {ACCENT}", no_wrap=True)
        grid.add_column(no_wrap=True)
        grid.add_column(style=MUTED)
        for number, (label, detail) in enumerate(options, start=1):
            grid.add_row(f"  {number}", label, detail)
        grid.add_row("  0", Text(back, style=MUTED), "")
        self.console.print()
        self.console.print(Text(f"  {title}", style=f"bold {SUBTLE}"))
        self.console.print(grid)
        return self._choose(len(options), default)

    def _ask_number(
        self, label: str, default: float, minimum: float, maximum: float, *, integer: bool = False,
        detail: str = "",
    ) -> float:
        """Un nombre dans ``[minimum, maximum]`` (la virgule décimale est acceptée)."""
        shown = str(int(default)) if integer or float(default).is_integer() else str(default)
        while True:
            answer = self._ask(label, shown, detail=detail).replace(",", ".")
            try:
                value = int(answer) if integer else float(answer)
            except ValueError:
                self._retry(f"« {answer} » n’est pas un nombre{' entier' if integer else ''}.")
                continue
            if minimum <= value <= maximum:
                return value
            self._retry(f"Attendu : une valeur comprise entre {fr_compact(minimum)} et {fr_compact(maximum)}.")

    def _confirm(self, label: str, default: bool = True) -> bool:
        while True:
            answer = self._ask(f"{label} (o/n)", "o" if default else "n").lower()
            if answer in _YES_ANSWERS:
                return True
            if answer in _NO_ANSWERS:
                return False
            self._retry("Répondez par o (oui) ou n (non).")

    def _pick_protocol(self, protocols: Sequence[str]) -> str | None:
        default = protocols.index(self.protocol) if self.protocol in protocols else 0
        index = self._pick(
            "Protocole",
            [(protocol_text(protocol), PROTOCOL_TRANSPORTS[protocol]) for protocol in protocols],
            default=default,
        )
        if index is None:
            return None
        self.protocol = protocols[index]
        return self.protocol

    # -- appels RPC -----------------------------------------------------------

    def _calls_screen(self) -> None:
        self._call_flow(PROTOCOLS, via_proxy=self._network_disturbed())

    def _network_disturbed(self) -> bool:
        return self.runtime.conditions.snapshot()["preset"] != "ideal"

    def _call_flow(self, protocols: Sequence[str], *, via_proxy: bool, timeout: float | None = None) -> None:
        """Choix du protocole, de la procédure et des paramètres, puis l'appel — rejouable à volonté."""
        while True:
            protocol = self._pick_protocol(protocols)
            if protocol is None:
                return
            methods = [spec for spec in METHODS if protocol in spec.protocols]
            index = self._pick(
                "Procédure",
                [(Text(spec.name, style="bold"), f"{spec.title} · {_KIND_LABELS.get(spec.kind, spec.kind)}")
                 for spec in methods],
                default=0,
            )
            if index is None:
                return
            spec = methods[index]
            hint(self.console, spec.description)
            params = {param.name: self._ask_param(param) for param in spec.params}
            count = self._ask_mode(spec)
            if count is None:
                return
            proxied = via_proxy and protocol != "local"
            if proxied:
                note(self.console, "Réseau simulé actif : l’appel traverse le proxy de chaos.",
                     mark=MARK_WARN, color=WARNING)
            while True:
                self._perform(protocol, spec, params, count, via_proxy=proxied, timeout=timeout)
                answer = self._ask("Entrée : rejouer · n : nouvel appel · 0 : retour").lower()
                if answer == "n":
                    break
                if answer in _BACK_ANSWERS:
                    return

    def _ask_param(self, param: ParamSpec) -> Any:
        """Saisie d'un paramètre, guidée par le catalogue : type, bornes et valeur par défaut."""
        if param.type == "int":
            low = param.minimum if param.minimum is not None else -sys.maxsize
            high = param.maximum if param.maximum is not None else sys.maxsize
            return int(self._ask_number(param.name, param.default, low, high, integer=True,
                                        detail=param.description))
        if param.type in _STRUCTURED_TYPES:
            return self._ask_json(param)
        if param.type == "product_id":
            identifiers = self.runtime.service.product_ids()
            hint(self.console, f"Références connues : {identifiers[0]} à {identifiers[-1]}")
        elif param.type == "category":
            hint(self.console, f"Catégories : {', '.join(self.runtime.service.categories())} (vide = toutes)")
        return self._ask(param.name, param.default or None, detail=param.description)

    def _ask_json(self, param: ParamSpec) -> Any:
        """Valeur structurée (liste de mouvements, de références), saisie en JSON ; Entrée garde l'exemple."""
        example = compact_json(param.default, 200)
        hint(self.console, f"Valeur proposée : {example}")
        while True:
            answer = self._ask(param.name, detail=f"{param.description} (JSON, Entrée pour la valeur proposée)")
            if not answer:
                return param.default
            try:
                return json.loads(answer)
            except ValueError:
                self._retry(f"JSON invalide. Exemple attendu : {example}")

    def _ask_mode(self, spec: MethodSpec) -> int | None:
        """Nombre d'appels à lancer : 1 pour un appel synchrone (ou un flux), N pour le mode asynchrone."""
        if spec.kind != "unary":
            return 1
        index = self._pick(
            "Mode d’appel",
            [
                (Text("Synchrone", style="bold"), "un appel, on attend sa réponse"),
                (Text("Asynchrone × N", style="bold"), "N appels lancés ensemble, comparés à N appels successifs"),
            ],
            default=0,
        )
        if index is None:
            return None
        if index == 0:
            return 1
        return int(self._ask_number("Nombre d’appels", DEFAULT_PARALLEL_CALLS, 2, MAX_PARALLEL_CALLS, integer=True))

    def _perform(
        self, protocol: str, spec: MethodSpec, params: dict[str, Any], count: int, *, via_proxy: bool,
        timeout: float | None,
    ) -> None:
        console = self.console
        console.print()
        if count > 1:
            hint(console, f"{count} × {describe_call(spec.name, params)}")
            with activity(console, f"{count} appels successifs, puis {count} appels lancés ensemble…"):
                report = run_parallel(
                    self.runtime, protocol, spec.name, params, count, via_proxy=via_proxy, timeout=timeout
                )
            render_parallel(console, report, protocol)
            if self.under_the_hood:
                # La mesure s'est faite bus de traces coupé : on rejoue UN appel, tracé, pour montrer le fil.
                hint(console, "Sous le capot — les appels ci-dessus sont identiques ; voici l’un d’eux, tracé :")
                outcome = execute_call(
                    self.runtime, protocol, spec.name, params, via_proxy=via_proxy, timeout=timeout
                )
                render_outcome(console, outcome, inspect=True, max_lines=RESULT_LINES)
            return

        def show_item(rank: int, elapsed_ms: float, item: Any) -> None:
            console.print(stream_item_line(rank, elapsed_ms, item, console.width))

        if spec.kind in STREAM_KINDS:
            hint(console, f"{describe_call(spec.name, params)} — les éléments s’affichent à leur arrivée")
        with activity(console, "Appel en cours…"):
            outcome = execute_call(
                self.runtime, protocol, spec.name, params, via_proxy=via_proxy, timeout=timeout, on_item=show_item
            )
        render_outcome(console, outcome, inspect=self.under_the_hood, max_lines=RESULT_LINES)

    # -- écrans simples -------------------------------------------------------

    def _toggle_under_the_hood(self) -> None:
        self.under_the_hood = not self.under_the_hood
        if self.under_the_hood:
            note(self.console, "Mode « Sous le capot » activé : chaque appel affichera son pipeline et ses octets.")
        else:
            note(self.console, "Mode « Sous le capot » désactivé : seuls les résultats sont affichés.",
                 mark="·", color=MUTED)

    def _tour_screen(self) -> None:
        # Les pauses de la visite passent par le lecteur de la séance : elles restent scriptables.
        cli_runner.run_demo(self.runtime, console=self.console, input_func=self._read)

    def _transparency_screen(self) -> None:
        cli_runner.run_transparency(self.runtime, console=self.console, banner=False)

    def _status_screen(self) -> None:
        section(self.console, "État du laboratoire")
        render_status(self.console, self.runtime)

    def _reset_screen(self) -> None:
        if not self._confirm("Remettre stocks, réseau simulé, compteurs et traces dans leur état initial", True):
            return
        self.runtime.reset()
        note(self.console, "Laboratoire réinitialisé : catalogue d’origine, réseau idéal, compteurs à zéro.")

    def _dashboard_screen(self) -> None:
        hint(self.console, "Le dashboard s’ouvre dans le navigateur ; Ctrl+C ici le ferme et ramène au menu.")
        try:
            code = cli_runner.run_dashboard(self.runtime, console=self.console, restart_lab=True)
        except KeyboardInterrupt:
            code = cli_runner.EXIT_OK
        if code == cli_runner.EXIT_OK:
            note(self.console, "Dashboard fermé — le laboratoire a été relancé, sur les mêmes ports.")

    # -- banc d'essai ---------------------------------------------------------

    def _benchmark_screen(self) -> None:
        index = self._pick(
            "Banc d’essai",
            [
                (Text("Rapide", style="bold"),
                 f"{QUICK_CONFIG['iterations']} appels par protocole, balayage réduit — quelques secondes"),
                (Text("Complet", style="bold"),
                 f"{DEFAULT_CONFIG['iterations']} appels par protocole, balayage jusqu’à 200 ms — une demi-minute"),
                (Text("Personnalisé", style="bold"), "choisir le nombre d’appels chronométrés"),
            ],
            default=0,
        )
        if index is None:
            return
        if index == 2:
            iterations = int(self._ask_number("Appels chronométrés par protocole", 500, 1, MAX_ITERATIONS,
                                              integer=True))
            full_sweep = self._confirm("Balayage réseau complet, de 0 à 200 ms (≈ 25 s de plus)", False)
            config = cli_runner.benchmark_config(quick=not full_sweep, iterations=iterations)
        else:
            config = cli_runner.benchmark_config(quick=index == 0)
        save = self._confirm("Enregistrer le rapport dans reports/", True)
        cli_runner.run_benchmark(self.runtime, console=self.console, config=config, save=save, banner=False)

    # -- laboratoire de pannes ------------------------------------------------

    def _failures_screen(self) -> None:
        actions: tuple[tuple[str, str, Callable[[], None]], ...] = (
            ("Jouer un scénario guidé", "latence, échéance, coupure, panne, doublon", self._play_scenario),
            ("Appliquer un préréglage réseau", "LAN, WAN, 3G, satellite, instable, trou noir, panne",
             self._apply_preset),
            ("Régler le réseau à la main", "latence, gigue, pics, coupures, trou noir, panne", self._tune_network),
            ("Armer une panne ponctuelle", "coupure ou réponse perdue sur le prochain appel", self._arm_fault),
            ("Appeler à travers ce réseau", "le même appel, soumis aux conditions ci-dessus", self._call_through),
            ("Rétablir un réseau idéal", "plus aucune perturbation, pannes désarmées", self._restore_network),
        )
        while True:
            section(self.console, "Laboratoire de pannes")
            self.console.print(Text.assemble(
                ("  Réseau simulé : ", MUTED), network_summary(self.runtime.conditions.snapshot())
            ))
            index = self._pick(
                "Que voulez-vous faire ?",
                [(Text(title, style="bold"), detail) for title, detail, _ in actions],
            )
            if index is None:
                return
            actions[index][2]()

    def _play_scenario(self) -> None:
        index = self._pick(
            "Scénario",
            [(Text(scenario["title"], style="bold"), scenario["concept"]) for scenario in SCENARIOS]
            + [(Text("Tous les scénarios", style="bold"), "les cinq, dans l’ordre")],
            default=0,
        )
        if index is None:
            return
        scenario = cli_runner.ALL_SCENARIOS if index == len(SCENARIOS) else SCENARIOS[index]["id"]
        protocol = self._pick_protocol(REMOTE_PROTOCOLS)
        if protocol is None:
            return
        cli_runner.run_failures(self.runtime, console=self.console, scenario=scenario, protocol=protocol,
                                banner=False)

    def _apply_preset(self) -> None:
        names = list(PRESETS)
        index = self._pick(
            "Préréglage réseau",
            [(Text(PRESET_INFO[name]["label"], style="bold"), PRESET_INFO[name]["description"]) for name in names],
        )
        if index is None:
            return
        self.runtime.conditions.apply_preset(names[index])
        note(self.console, f"Réseau simulé : {PRESET_INFO[names[index]]['label']}.")

    def _tune_network(self) -> None:
        current = self.runtime.conditions.snapshot()
        hint(self.console, "Entrée garde la valeur actuelle de chaque réglage.")
        fields: dict[str, Any] = {}
        for name, label, boolean in _NETWORK_FIELDS:
            if boolean:
                fields[name] = self._confirm(label, bool(current[name]))
            else:
                maximum = 1.0 if name.endswith("_probability") else 1_000_000.0
                fields[name] = self._ask_number(label, current[name], 0.0, maximum)
        self.runtime.conditions.update(**fields)
        self.console.print(Text.assemble(
            (f"  {MARK_OK} ", f"bold {SUCCESS}"), "Réseau simulé réglé : ",
            network_summary(self.runtime.conditions.snapshot()),
        ))

    def _arm_fault(self) -> None:
        index = self._pick("Panne à armer", [(Text(_ARM_LABELS[kind][0], style="bold"), _ARM_LABELS[kind][1])
                                             for kind in ARM_KINDS])
        if index is None:
            return
        protocol = self._pick_protocol(REMOTE_PROTOCOLS)
        if protocol is None:
            return
        self.runtime.arm(ARM_KINDS[index], protocol)
        note(self.console, f"Panne armée sur le proxy {PROTOCOL_LABELS[protocol]} : elle frappera le prochain "
                           "appel qui le traverse.", mark=MARK_WARN, color=WARNING)

    def _call_through(self) -> None:
        timeout = self._ask_number("Échéance de l’appel (s)", DEFAULT_TIMEOUT_S, 0.05, 120.0)
        self._call_flow(REMOTE_PROTOCOLS, via_proxy=True, timeout=timeout)

    def _restore_network(self) -> None:
        self.runtime.conditions.reset()
        for proxy in self.runtime.proxies.values():
            proxy.disarm()
        note(self.console, "Réseau idéal rétabli, aucune panne armée.")

    # -- laboratoire de contrat -----------------------------------------------

    def _contract_screen(self) -> None:
        index = self._pick(
            "Laboratoire de contrat",
            [(Text("Tout dérouler", style="bold"), "le diff, les changements, puis tous les scénarios")]
            + [(Text(scenario["title"], style="bold"), scenario["summary"]) for scenario in CONTRACT_SCENARIOS],
            default=0,
        )
        if index is None:
            return
        scenario = None if index == 0 else CONTRACT_SCENARIOS[index - 1]["id"]
        cli_runner.run_contract(self.runtime, console=self.console, scenario=scenario, banner=False)
