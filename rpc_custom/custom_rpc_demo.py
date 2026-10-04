"""Démonstration autonome du RPC maison.

    python -m rpc_custom.custom_rpc_demo

Démarre un squelette serveur sur un port éphémère, puis déroule ce qu'un
middleware RPC sait faire — et ce qu'il fait passer sur le fil : découverte,
appel synchrone, trames brutes, appels asynchrones multiplexés, lot,
notification, flux serveur, et enfin les erreurs propres à un appel distant.

La démonstration utilise son propre bus de traces : c'est lui qui fournit les
octets réellement émis et reçus, affichés à l'étape « sous le capot ».
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from typing import Any, Callable

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from common.config import APP_NAME, HOST
from common.errors import RpcError
from common.inventory import InventoryService
from common.telemetry import STAGE_INFO, EventBus, TraceCollector, TraceEvent, hexdump

from .client_stub import RpcClientStub
from .inventory_binding import build_inventory_skeleton
from .protocol import HEADER_SIZE
from .server_skeleton import RpcServerSkeleton

ACCENT = "#8B7CFF"
CLIENT = "#5AA2FF"
SERVER = "#2FD9C4"
SUCCESS = "#4ADE80"
WARNING = "#FBBF24"
DANGER = "#F87171"
MUTED = "grey58"

MAX_WIDTH = 118
PIPELINED_CALLS = 20
WORK_MS = 40
HEXDUMP_BYTES = 96
# Mise en page de common.telemetry.hexdump : « oooooo  xx xx … xx  ascii ».
_HEX_COLUMN = 8
_ASCII_COLUMN = _HEX_COLUMN + 16 * 3 - 1 + 2


class DemoProcedures:
    """Procédures ajoutées pour la démonstration, à côté du service d'inventaire."""

    def __init__(self) -> None:
        self.journal: list[str] = []
        self.logged = threading.Event()

    def simulate_work(self, duration_ms: int = WORK_MS) -> dict[str, Any]:
        """Simule un traitement lent (entrée/sortie, calcul)."""
        time.sleep(duration_ms / 1000)
        return {"worked_ms": duration_ms}

    def log_event(self, message: str) -> None:
        """Consigne un message dans le journal du serveur."""
        self.journal.append(message)
        self.logged.set()


class Demo:
    """Le scénario, étape par étape ; chaque méthode ``show_*`` est une section affichée."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.bus = EventBus()
        self.collector = TraceCollector(self.bus).start()
        self.procedures = DemoProcedures()
        self.skeleton: RpcServerSkeleton = build_inventory_skeleton(InventoryService(), port=0, bus=self.bus)
        self.skeleton.register_instance(self.procedures)
        self.skeleton.start()
        self.stub = RpcClientStub(HOST, self.skeleton.port, bus=self.bus)
        self._section = 0

    def run(self) -> None:
        try:
            self.show_banner()
            self.show_discovery()
            self.show_sync_call()
            self.show_wire()
            self.show_pipelining()
            self.show_batch()
            self.show_notification()
            self.show_stream()
            self.show_errors()
            self.show_summary()
        finally:
            self.stub.close()
            self.skeleton.stop()
            self.collector.stop()

    # -- sections -------------------------------------------------------------------

    def show_banner(self) -> None:
        title = Text.assemble((APP_NAME, f"bold {ACCENT}"), ("  ·  RPC fait maison", "bold"))
        subtitle = Text(
            "JSON-RPC 2.0 sur TCP — un stub, un squelette, une trame préfixée par sa longueur.\n"
            f"Squelette serveur à l’écoute sur {HOST}:{self.skeleton.port}, "
            f"{len(self.skeleton.methods())} procédures enregistrées.",
            style=MUTED,
        )
        self.console.print()
        self.console.print(Panel(Group(title, subtitle), border_style=ACCENT, box=box.ROUNDED, padding=(1, 2)))

    def show_discovery(self) -> None:
        self.section(
            "Découverte",
            "Sans IDL, le seul « contrat » est ce que le serveur veut bien décrire : rpc.discover.",
        )
        self.code("stub.discover()")
        table = self.table("Procédure", "Paramètres", "Flux", "Description")
        for method in self.stub.discover()["methods"]:
            params = ", ".join(_format_param(param) for param in method["params"])
            summary = method["doc"].splitlines()[0].replace("``", "") if method["doc"] else ""
            table.add_row(
                Text(method["name"], style=f"bold {CLIENT}"),
                Text(params or "—"),
                Text("oui", style=SERVER) if method["streaming"] else Text("non", style=MUTED),
                Text(summary, style=MUTED),
            )
        self.console.print(table)

    def show_sync_call(self) -> None:
        self.section(
            "Appel synchrone",
            "Pour l’appelant, une fonction comme une autre : c’est la transparence de localisation.",
        )
        self.code('result = stub.update_stock("SKU-1001", -3)')
        started = time.perf_counter()
        result = self.stub.update_stock("SKU-1001", -3)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.console.print(_json_block(result))
        self.note(
            f"Aller-retour complet en {_fr(elapsed_ms, 2)} ms : "
            "marshalling, réseau, exécution distante, démarshalling."
        )

    def show_wire(self) -> None:
        self.section(
            "Sous le capot",
            "Le même appel, vu du réseau : ce que le stub a réellement écrit, et ce qu’il a lu.",
        )
        trace = self.collector.get(self.stub.last_call_id)
        if trace is None:
            return
        events = {event.stage: event for event in trace.events}
        self.console.print(_frame_panel("Requête — écrite par le stub", events["client.send"], CLIENT))
        self.console.print(_frame_panel("Réponse — écrite par le squelette", events["server.send"], SERVER))

        table = self.table("#", "Côté", "Étape", "Durée", "Octets", "Rôle", title="Chronologie de l’appel")
        for index, event in enumerate(trace.events, start=1):
            info = STAGE_INFO.get(event.stage, {"label": event.stage, "role": ""})
            table.add_row(
                str(index),
                Text("client", style=CLIENT) if event.side == "client" else Text("serveur", style=SERVER),
                Text(info["label"], style="bold"),
                Text(_format_duration(event.duration_us), justify="right"),
                Text("" if event.size is None else str(event.size), justify="right"),
                Text(info["role"], style=MUTED),
            )
        self.console.print(table)

    def show_pipelining(self) -> None:
        self.section(
            "Asynchrone et multiplexage",
            f"{PIPELINED_CALLS} appels à une procédure qui prend {WORK_MS} ms, sur UNE seule connexion TCP.",
        )
        self.code(f'futures = [stub.call_async("simulate_work", {WORK_MS}) for _ in range({PIPELINED_CALLS})]')
        with self.bus.muted():      # on mesure le middleware, pas son instrumentation
            started = time.perf_counter()
            for _ in range(PIPELINED_CALLS):
                self.stub.simulate_work(WORK_MS)
            sequential_ms = (time.perf_counter() - started) * 1000

            started = time.perf_counter()
            futures = [self.stub.call_async("simulate_work", WORK_MS) for _ in range(PIPELINED_CALLS)]
            for future in futures:
                future.result()
            pipelined_ms = (time.perf_counter() - started) * 1000

        table = self.table("Mode", "Durée totale", "Par appel", "")
        longest = max(sequential_ms, pipelined_ms)
        for label, duration, color in (
            ("Séquentiel — stub.simulate_work(…)", sequential_ms, WARNING),
            ("Pipeliné — stub.call_async(…)", pipelined_ms, SUCCESS),
        ):
            bar = "█" * max(1, round(40 * duration / longest))
            table.add_row(
                label,
                Text(f"{_fr(duration, 0)} ms", justify="right"),
                Text(f"{_fr(duration / PIPELINED_CALLS)} ms", justify="right"),
                Text(bar, style=color),
            )
        self.console.print(table)
        self.note(
            f"×{_fr(sequential_ms / pipelined_ms)} : les requêtes partent sans attendre les réponses, "
            "le serveur les exécute en parallèle,\n"
            "    et le thread lecteur du stub rend chaque réponse au bon appel grâce à son « id »."
        )

    def show_batch(self) -> None:
        self.section(
            "Lot (batch)",
            "Plusieurs appels dans une seule trame ; un échec n’efface pas les autres résultats.",
        )
        calls: list[tuple[str, Any]] = [
            ("calculate_factorial", [12]),
            ("get_product_details", {"product_id": "SKU-1005"}),
            ("get_product_details", {"product_id": "SKU-9999"}),
            ("update_stock", {"product_id": "SKU-1005", "delta": 10}),
        ]
        self.code('results = stub.batch([("calculate_factorial", [12]), ("get_product_details", {…}), …])')
        mark = self.mark()
        results = self.stub.batch(calls)
        table = self.table("#", "Appel", "Résultat")
        for index, ((method, params), result) in enumerate(zip(calls, results), start=1):
            if isinstance(result, RpcError):
                outcome = Text.assemble((type(result).__name__, f"bold {DANGER}"), (f" — {result.message}", MUTED))
            else:
                outcome = Text(_compact(result, 52), style=MUTED)
            table.add_row(str(index), Text(_format_call(method, params), style=CLIENT), outcome)
        self.console.print(table)
        sent, received = self.first_since(mark, "client.send"), self.first_since(mark, "client.receive")
        if sent is not None and received is not None:
            self.note(
                f"{len(calls)} appels, mais une seule trame émise ({sent.size} octets) "
                f"et une seule trame reçue ({received.size} octets)."
            )

    def show_notification(self) -> None:
        self.section("Notification", "Une requête sans « id » : le serveur l’exécute, mais ne répond jamais.")
        self.code('stub.notify("log_event", "inventaire vérifié")')
        mark = self.mark()
        self.stub.notify("log_event", "inventaire vérifié")
        executed = self.procedures.logged.wait(2.0)
        sent = self.first_since(mark, "client.send")
        answered = self.first_since(mark, "server.send") is not None
        if sent is not None and sent.payload is not None:
            self.console.print(_json_block(json.loads(sent.payload[HEADER_SIZE:])))

        table = self.table("Observation", "Constat")
        table.add_row("Trame émise par le stub", f"{sent.size if sent is not None else 0} octets")
        table.add_row(
            "Procédure exécutée côté serveur",
            Text(f"oui — journal du serveur : « {' ; '.join(self.procedures.journal)} »", style=SUCCESS)
            if executed else Text("non", style=DANGER),
        )
        table.add_row(
            "Trame de réponse",
            Text("une réponse est partie", style=DANGER) if answered
            else Text("aucune : ni résultat, ni erreur, ni accusé de réception", style=WARNING),
        )
        self.console.print(table)

    def show_stream(self) -> None:
        self.section(
            "Flux serveur",
            "Une requête, plusieurs trames : chaque élément arrive dans une notification rpc.stream.item.",
        )
        self.code('for snapshot in stub.stream("stream_analytics", samples=5, interval_ms=120): ...')
        table = self.table("Reçu à", "seq", "Unités en stock", "Valeur du stock", "Commandes / min")
        started = time.perf_counter()
        for snapshot in self.stub.stream("stream_analytics", samples=5, interval_ms=120):
            elapsed_ms = (time.perf_counter() - started) * 1000
            table.add_row(
                Text(f"+{_fr(elapsed_ms, 0)} ms", justify="right"),
                str(snapshot["seq"]),
                Text(_fr(snapshot["total_units"], 0), justify="right"),
                Text(f"{_fr(snapshot['inventory_value'], 2)} €", justify="right"),
                Text(_fr(snapshot["orders_per_min"]), justify="right"),
            )
        self.console.print(table)
        trace = self.collector.get(self.stub.last_call_id)
        if trace is not None:
            frames = sum(event.stage == "client.stream_item" for event in trace.events)
            final = json.dumps({"stream": "end", "count": frames}, separators=(",", ":"))
            self.note(f"{frames} trames d’éléments, puis la réponse finale {final} clôt le flux.")

    def show_errors(self) -> None:
        self.section(
            "Erreurs",
            "Ce qu’un appel local ne connaît pas : le serveur peut refuser, se taire, ou disparaître.",
        )
        cases: list[tuple[str, str, Callable[[], Any]]] = [
            ("Procédure inconnue", 'stub.get_product("SKU-1001")',
             lambda: self.stub.get_product("SKU-1001")),
            ("Paramètres invalides", 'stub.update_stock("SKU-1001")',
             lambda: self.stub.update_stock("SKU-1001")),
            ("Erreur métier : introuvable", 'stub.get_product_details("SKU-9999")',
             lambda: self.stub.get_product_details("SKU-9999")),
            ("Erreur métier : précondition", 'stub.update_stock("SKU-1001", -10000)',
             lambda: self.stub.update_stock("SKU-1001", -10_000)),
            ("Pas de réponse à temps", 'stub.call("simulate_work", 600, timeout=0.15)',
             lambda: self.stub.call("simulate_work", 600, timeout=0.15)),
            ("Serveur arrêté en plein appel", "stub.simulate_work(600)  # puis skeleton.stop()",
             self._call_then_stop_server),
        ]
        table = self.table("Cas", "Ce que lève le stub", leading=1)
        for label, source, action in cases:
            try:
                action()
            except RpcError as error:
                detail = error.detail if isinstance(error.detail, dict) else {}
                wire_code = detail.get("jsonrpc_code")
                outcome = Text.assemble(
                    (type(error).__name__, f"bold {DANGER}"),
                    (f"   {error.code}", "bold"),
                    (f"   JSON-RPC {wire_code}" if wire_code is not None else "   aucune réponse reçue", WARNING),
                    (f"\n{error.message}", MUTED),
                )
            else:
                outcome = Text("aucune erreur", style=SUCCESS)
            table.add_row(Text.assemble((f"{label}\n", "bold"), (source, CLIENT)), outcome)
        self.console.print(table)
        self.note(
            "Après un délai dépassé, le client ignore si le serveur a exécuté l’appel : c’est le piège de la\n"
            "    transparence, et la raison d’être des retries, de l’idempotence et des disjoncteurs.",
            mark="!", color=WARNING,
        )

    def _call_then_stop_server(self) -> None:
        pending = self.stub.call_async("simulate_work", 600)
        self.skeleton.stop()
        pending.result()

    def show_summary(self) -> None:
        lines = Text()
        for term, text in (
            ("stub", "appel Python → requête JSON-RPC → trame ; réponse → valeur ou exception"),
            ("trame", "4 octets de longueur (big-endian) + JSON UTF-8 compact, un seul sendall"),
            ("squelette", "trame → requête → procédure enregistrée → réponse, en parallèle par connexion"),
            ("« id »", "la clé de voûte : il rattache chaque réponse à son appel, même dans le désordre"),
        ):
            lines.append(f"  {term:<12}", style=f"bold {ACCENT}")
            lines.append(f"{text}\n")
        lines.append("\n  À lire : rpc_custom/protocol.py, client_stub.py, server_skeleton.py", style=MUTED)
        self.console.print()
        self.console.print(
            Panel(lines, title="En résumé", border_style=ACCENT, box=box.ROUNDED, padding=(1, 1))
        )
        self.console.print()

    # -- outils d'affichage -----------------------------------------------------------

    def section(self, title: str, lead: str) -> None:
        self._section += 1
        self.console.print()
        heading = Text(f" {self._section} · {title} ", style=f"bold {ACCENT}")
        self.console.print(Rule(heading, style=ACCENT, align="left"))
        self.console.print(Text(lead, style=MUTED))
        self.console.print()

    def code(self, source: str) -> None:
        self.console.print(Text.assemble(("  >>> ", MUTED), (source, f"bold {CLIENT}")))

    def note(self, text: str, *, mark: str = "✓", color: str = SUCCESS) -> None:
        self.console.print(Text.assemble((f"  {mark} ", f"bold {color}"), text))

    def table(self, *columns: str, title: str | None = None, leading: int = 0) -> Table:
        return Table(
            *columns, title=title, title_style=f"bold {ACCENT}", title_justify="left", leading=leading,
            box=box.SIMPLE_HEAD, header_style="bold", border_style=MUTED, pad_edge=False,
        )

    def mark(self) -> int:
        """Numéro du dernier évènement publié : repère pour ne lire que ce qui suit."""
        recent = self.bus.recent(1)
        return recent[-1].seq if recent else 0

    def first_since(self, mark: int, stage: str) -> TraceEvent | None:
        return next((event for event in self.bus.recent() if event.seq > mark and event.stage == stage), None)


def _frame_panel(title: str, event: TraceEvent, color: str) -> Panel:
    """Une trame telle qu'elle a circulé : vue hexadécimale, puis le message JSON qu'elle transporte."""
    data = event.payload or b""
    dump = Text(hexdump(data, limit=HEXDUMP_BYTES), style=MUTED)
    # L'en-tête de longueur tient sur la première ligne : on le surligne côté hexadécimal et côté ASCII.
    dump.stylize(f"bold {WARNING}", _HEX_COLUMN, _HEX_COLUMN + 3 * HEADER_SIZE - 1)
    dump.stylize(f"bold {WARNING}", _ASCII_COLUMN, _ASCII_COLUMN + HEADER_SIZE)
    legend = Text.assemble(
        ("■ ", WARNING), (f"en-tête : longueur du message = {len(data) - HEADER_SIZE} octets", MUTED),
        ("   ■ ", MUTED), ("corps : message JSON-RPC 2.0", MUTED),
    )
    truncated = len(data) > HEXDUMP_BYTES
    return Panel(
        Group(dump, legend, Text(), _json_block(json.loads(data[HEADER_SIZE:]))),
        title=f"{title} · {len(data)} octets", title_align="left",
        subtitle=f"vue hexadécimale limitée aux {HEXDUMP_BYTES} premiers octets" if truncated else None,
        subtitle_align="right", border_style=color, box=box.ROUNDED,
    )


def _json_block(value: Any) -> Syntax:
    text = json.dumps(value, indent=2, ensure_ascii=False)
    return Syntax(text, "json", theme="ansi_dark", background_color="default", word_wrap=True)


def _format_param(param: dict[str, Any]) -> str:
    annotation = f": {param['annotation']}" if param["annotation"] else ""
    default = "" if param["required"] else f" = {json.dumps(param['default'], ensure_ascii=False)}"
    return f"{param['name']}{annotation}{default}"


def _format_call(method: str, params: Any) -> str:
    """Écrit un appel de lot comme on l'écrirait en Python."""
    if isinstance(params, dict):
        arguments = [f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in params.items()]
    else:
        arguments = [json.dumps(value, ensure_ascii=False) for value in params or ()]
    return f"{method}({', '.join(arguments)})"


def _format_duration(duration_us: float | None) -> str:
    if duration_us is None:
        return ""
    return f"{_fr(duration_us / 1000, 2)} ms" if duration_us >= 1000 else f"{_fr(duration_us, 0)} µs"


def _compact(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _fr(value: float, digits: int = 1) -> str:
    """Nombre au format français : espace pour les milliers, virgule décimale."""
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def main() -> int:
    # La console Windows n'est pas en UTF-8 par défaut : accents et filets seraient illisibles.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    width = min(shutil.get_terminal_size((MAX_WIDTH, 40)).columns, MAX_WIDTH)
    # markup=False : les textes affichés contiennent des crochets (JSON, annotations de type).
    Demo(Console(width=width, highlight=False, markup=False)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
