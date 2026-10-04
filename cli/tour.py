"""Visite guidée « sous le capot » : le même appel à travers les trois middlewares.

Un seul appel métier — sortir trois unités d'un stock — est exécuté tour à tour
par le RPC maison, par gRPC puis par REST. Pour chacun on déroule la trace
réelle : les douze étapes du pipeline (stub, marshalling, transport, squelette,
procédure, et retour), puis les octets de la requête et de la réponse. Un
récapitulatif met enfin les trois protocoles côte à côte, avec l'appel local
comme point zéro.

Chaque protocole part du même stock et d'une connexion déjà ouverte : les
tailles et les durées affichées se comparent donc à conditions égales.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Mapping

from rich import box
from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from benchmark_lab.report import MISSING, fr_bytes, fr_duration, fr_number
from common.config import PROTOCOL_LABELS, PROTOCOL_TRANSPORTS, PROTOCOLS, REMOTE_PROTOCOLS

from .calls import CallOutcome, describe_call, execute_call
from .theme import (
    ACCENT,
    DANGER,
    MARK_FAIL,
    MARK_OK,
    MUTED,
    SUBTLE,
    SUCCESS,
    Column,
    bar_text,
    compact_json,
    fill_table,
    header,
    is_narrow,
    make_table,
    protocol_color,
    protocol_text,
    section,
)
from .wire import wire_report

if TYPE_CHECKING:
    from lab import LabRuntime

TOUR_METHOD = "update_stock"
TOUR_PARAMS: dict[str, Any] = {"product_id": "SKU-1001", "delta": -3, "idempotency_key": ""}
TRACED_RUNS = 5

_SERIALIZATION_STAGES = ("client.marshal", "server.unmarshal", "server.marshal", "client.unmarshal")
_LEADS: dict[str, str] = {
    "custom": "Le stub écrit à la main : un message JSON-RPC 2.0, précédé de sa longueur sur 4 octets, dans une "
              "connexion TCP.",
    "grpc": "Le stub généré par protoc : un message Protobuf binaire, précédé d’un préfixe de 5 octets, dans un "
            "flux HTTP/2.",
    "rest": "Sans stub : une requête HTTP/1.1 dont l’URL désigne la ressource, le verbe l’action, et le corps "
            "JSON les arguments.",
}

Pause = Callable[[str], None]


def _round_trip(outcome: CallOutcome) -> float:
    """Durée de l'appel vue par le stub (étape « retour à l'appelant »), en millisecondes."""
    traced = outcome.trace.summary()["duration_us"] if outcome.trace is not None else None
    return outcome.duration_ms if traced is None else traced / 1000


def _fair_call(runtime: "LabRuntime", protocol: str) -> CallOutcome:
    """L'appel de la visite, à conditions égales pour chaque protocole.

    Un premier appel, non tracé, ouvre la connexion (sinon la durée affichée
    serait celle de la poignée de main TCP). L'appel est ensuite tracé
    ``TRACED_RUNS`` fois et l'on garde l'exécution la plus rapide : client et
    serveur partagent ici un seul interpréteur, et un changement de fil mal
    placé peut ajouter plusieurs millisecondes à une étape. Entre deux
    exécutions, le stock est ramené à sa valeur initiale : tous les protocoles
    font ainsi exactement la même opération.
    """
    service = runtime.service
    product_id = TOUR_PARAMS["product_id"]
    stock = service.check_stock(product_id)["stock"]

    def restore() -> None:
        drift = service.check_stock(product_id)["stock"] - stock
        if drift:
            service.update_stock(product_id, -drift)

    with runtime.client(protocol) as client:
        with runtime.bus.muted():
            execute_call(runtime, protocol, "get_product_details", {"product_id": product_id}, client=client)
        runs = []
        for _ in range(TRACED_RUNS):
            try:
                runs.append(execute_call(runtime, protocol, TOUR_METHOD, TOUR_PARAMS, client=client))
            finally:
                restore()
            if not runs[-1].ok:
                return runs[-1]      # une erreur se montre telle quelle : inutile de la rejouer
    return min(runs, key=_round_trip)


def _stage_durations(outcome: CallOutcome, stages: tuple[str, ...]) -> float | None:
    """Somme, en millisecondes, des durées de certaines étapes de la trace."""
    if outcome.trace is None:
        return None
    durations = [
        event.duration_us for event in outcome.trace.events
        if event.stage in stages and event.duration_us is not None
    ]
    return sum(durations) / 1000 if durations else None


def _recap(console: Console, outcomes: Mapping[str, CallOutcome]) -> None:
    columns = (
        Column("Protocole"),
        Column("Requête", numeric=True, wide_only=True),
        Column("Réponse", numeric=True, wide_only=True),
        Column("Octets", numeric=True),
        Column("Octets échangés", wide_only=True),
        Column("Sérialisation", numeric=True),
        Column("Exécution", numeric=True),
        Column("Aller-retour", numeric=True, style="bold"),
    )
    totals = {
        protocol: (outcome.request_bytes or 0) + (outcome.response_bytes or 0)
        for protocol, outcome in outcomes.items()
    }
    heaviest = max(totals.values(), default=0)
    rows = []
    for protocol, outcome in outcomes.items():
        remote = protocol != "local"
        rows.append((
            protocol_text(protocol),
            fr_number(outcome.request_bytes) if remote else MISSING,
            fr_number(outcome.response_bytes) if remote else MISSING,
            fr_number(totals[protocol]) if remote else MISSING,
            bar_text(totals[protocol], heaviest, protocol_color(protocol), 22),
            fr_duration(_stage_durations(outcome, _SERIALIZATION_STAGES)),
            fr_duration(_stage_durations(outcome, ("server.execute",))),
            fr_duration(_round_trip(outcome)) if outcome.ok else Text(outcome.error.code, style=DANGER),
        ))
    table = make_table(
        "Le même appel, quatre chemins",
        caption="Octets : requête + réponse. Sérialisation : marshalling et démarshalling, des deux côtés. "
                "Exécution : la procédure seule, sur le serveur. Aller-retour : la durée totale vue par le stub. "
                f"Pour chaque protocole, la plus rapide de {TRACED_RUNS} exécutions tracées : ces durées montrent où "
                "passe le temps ; pour comparer les performances, voir --benchmark.",
    )
    console.print(fill_table(table, columns, rows, narrow=is_narrow(console)))


def _takeaway(outcomes: Mapping[str, CallOutcome]) -> Panel:
    points = Table.grid(padding=(0, 2))
    points.add_column(style=f"bold {ACCENT}", no_wrap=True)
    points.add_column(ratio=1)
    points.add_row("Stub", "L’appelant écrit un appel de fonction ; le stub le transforme en octets et rend la "
                           "réponse comme une valeur de retour.")
    points.add_row("Marshalling", "JSON nomme chaque champ en toutes lettres ; Protobuf n’envoie qu’un numéro et "
                                  "un type de fil — le contrat .proto dit le reste.")
    points.add_row("Transport", "Une trame préfixée par sa longueur (maison), un flux HTTP/2 (gRPC) ou une requête "
                                "HTTP/1.1 (REST) : trois façons de délimiter un message.")
    points.add_row("Squelette", "Côté serveur, le chemin inverse : octets → arguments → procédure → résultat → "
                                "octets.")
    body = [points]
    grpc, custom = outcomes.get("grpc"), outcomes.get("custom")
    if grpc and custom and grpc.request_bytes and custom.request_bytes:
        ratio = custom.request_bytes / grpc.request_bytes
        body += [Text(), Text(
            f"Ici, la requête gRPC pèse {fr_bytes(grpc.request_bytes)} contre {fr_bytes(custom.request_bytes)} "
            f"pour JSON-RPC, soit {fr_number(ratio, 1)} fois moins : c’est le prix des noms de champs en clair.",
            style=SUBTLE,
        )]
    body += [Text(), Text("Pour aller plus loin : python main.py --benchmark · --simulate-failures · --contract",
                          style=MUTED)]
    return Panel(Group(*body), title=Text("En résumé", style=f"bold {ACCENT}"), title_align="left",
                 box=box.ROUNDED, border_style=ACCENT, padding=(1, 2))


def run_tour(runtime: "LabRuntime", console: Console, *, pause: Pause | None = None) -> dict[str, CallOutcome]:
    """Déroule la visite ; ``pause(message)`` est appelé entre deux protocoles (``None`` : sans arrêt).

    Renvoie l'issue de l'appel pour chaque protocole, ``local`` compris.
    """
    call = describe_call(TOUR_METHOD, {name: value for name, value in TOUR_PARAMS.items() if value != ""})
    header(
        console,
        "Visite guidée « sous le capot » — un appel, trois middlewares, octet par octet",
        f"L’appel suivi : {call}",
    )
    console.print()
    console.print(Padding(Text.assemble(
        ("Pour l’appelant, c’est une seule ligne, la même quel que soit le protocole : ", SUBTLE),
        (f"client.{call}", "bold"), (". Voici ce qu’elle déclenche réellement.", SUBTLE),
    ), (0, 2)))
    outcomes: dict[str, CallOutcome] = {}
    for number, protocol in enumerate(REMOTE_PROTOCOLS, start=1):
        if number > 1 and pause is not None:
            pause(f"Entrée pour passer à {PROTOCOL_LABELS[protocol]}")
        section(console, f"{PROTOCOL_LABELS[protocol]} — {PROTOCOL_TRANSPORTS[protocol]}", _LEADS[protocol],
                number=number)
        outcome = outcomes[protocol] = _fair_call(runtime, protocol)
        if outcome.ok:
            console.print(Text.assemble(
                (f"  {MARK_OK} ", f"bold {SUCCESS}"), ("résultat  ", MUTED), compact_json(outcome.result, 96),
            ))
        else:
            console.print(Text.assemble(
                (f"  {MARK_FAIL} ", f"bold {DANGER}"), (f"{outcome.error.code} — {outcome.error.message}", DANGER),
            ))
        console.print()
        # La phrase d'explication de chaque étape n'est donnée qu'une fois : le pipeline est le même ensuite.
        wire_report(console, outcome.trace, explain=number == 1)
    if pause is not None:
        pause("Entrée pour le récapitulatif")
    section(console, "Récapitulatif", "Tailles et durées relevées dans les traces des appels ci-dessus.",
            number=len(REMOTE_PROTOCOLS) + 1)
    outcomes = {"local": _fair_call(runtime, "local"), **outcomes}
    _recap(console, {protocol: outcomes[protocol] for protocol in PROTOCOLS if protocol in outcomes})
    console.print()
    console.print(_takeaway(outcomes))
    return outcomes
