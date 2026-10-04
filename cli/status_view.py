"""État du laboratoire : serveurs, proxys de chaos, réseau simulé, trafic et inventaire.

Tout vient de ``LabRuntime.status()`` — le même dictionnaire que sert
``GET /api/status`` au dashboard : le terminal et le navigateur montrent donc
exactement le même état.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

from benchmark_lab.report import fr_bytes, fr_compact, fr_duration, fr_number, fr_percent
from netsim import PRESET_INFO

from .theme import (
    DANGER,
    DOT,
    MARK_FAIL,
    MUTED,
    SUBTLE,
    SUCCESS,
    WARNING,
    Column,
    fill_table,
    hint,
    is_narrow,
    make_table,
    protocol_text,
)

if TYPE_CHECKING:
    from lab import LabRuntime

_V2_SUFFIX = "_v2"


def network_summary(conditions: Mapping[str, Any]) -> Text:
    """Les conditions réseau en une ligne : le préréglage, puis ce qui s'écarte d'un réseau parfait."""
    preset = conditions.get("preset", "custom")
    label = PRESET_INFO.get(preset, {}).get("label", "Réglage manuel")
    disturbed = preset != "ideal"
    text = Text.assemble((label, f"bold {WARNING if disturbed else SUCCESS}"))
    facts = []
    if conditions.get("latency_ms"):
        jitter = f" ± {fr_compact(conditions['jitter_ms'])} ms" if conditions.get("jitter_ms") else ""
        facts.append(f"latence {fr_compact(conditions['latency_ms'])} ms{jitter}")
    if conditions.get("spike_probability"):
        facts.append(
            f"pics de {fr_compact(conditions.get('spike_ms', 0))} ms sur "
            f"{fr_percent(conditions['spike_probability'] * 100, 0)} des requêtes"
        )
    if conditions.get("reset_probability"):
        facts.append(f"coupures sur {fr_percent(conditions['reset_probability'] * 100, 0)} des requêtes")
    if conditions.get("bandwidth_kbps"):
        facts.append(f"débit limité à {fr_compact(conditions['bandwidth_kbps'])} kbit/s")
    if conditions.get("blackhole"):
        facts.append("trou noir : aucune réponse ne revient")
    if conditions.get("down"):
        facts.append("serveur injoignable")
    if facts:
        text.append(f" — {', '.join(facts)}", style=SUBTLE)
    return text


def servers_table(status: Mapping[str, Any], *, narrow: bool = False) -> Table:
    columns = (
        Column("Serveur"),
        Column("Transport", style=SUBTLE, wide_only=True, no_wrap=False),
        Column("Port direct" if narrow else "Adresse directe"),
        Column("Port du proxy" if narrow else "Via le proxy de chaos"),
        Column("État"),
    )
    rows = []
    for server in status["servers"]:
        protocol = server["id"].removesuffix(_V2_SUFFIX)
        host = "" if narrow else server["host"]
        state = (
            Text(f"{DOT} en ligne", style=f"bold {SUCCESS}") if server["up"]
            else Text(f"{MARK_FAIL} arrêté", style=f"bold {DANGER}")
        )
        rows.append((
            protocol_text(protocol, server["label"]),
            server["transport"],
            f"{host}:{server['port']}",
            f"{host}:{server['proxy_port']}" if server.get("proxy_port") else Text("—", style=MUTED),
            state,
        ))
    return fill_table(make_table("Serveurs"), columns, rows, narrow=narrow)


def proxies_table(status: Mapping[str, Any], *, narrow: bool = False) -> Table:
    columns = (
        Column("Proxy"),
        Column("Connexions", numeric=True),
        Column("Octets montants", numeric=True, wide_only=True),
        Column("Octets descendants", numeric=True, wide_only=True),
        Column("Coupures", numeric=True),
        Column("Refus", numeric=True),
        Column("Avalés", numeric=True),
        Column("Perdues", numeric=True),
    )
    rows = []
    for protocol, stats in status["proxies"].items():
        active = stats["connections_active"]
        connections = fr_number(stats["connections_total"])
        if active:
            connections += f" (dont {fr_number(active)} en cours)"
        faults = [stats["resets"], stats["refused"], stats["blackholed"], stats["lost_replies"]]
        rows.append((
            protocol_text(protocol),
            connections,
            fr_number(stats["bytes_up"]),
            fr_number(stats["bytes_down"]),
            *(Text(fr_number(count), style=f"bold {DANGER}" if count else MUTED) for count in faults),
        ))
    table = make_table(
        "Proxys de chaos",
        caption="Pannes injectées — coupures : connexions réinitialisées ; refus : serveur injoignable ; "
                "avalés : paquets d’un trou noir ; perdues : réponses supprimées après exécution.",
    )
    return fill_table(table, columns, rows, narrow=narrow)


def traffic_table(status: Mapping[str, Any], *, narrow: bool = False) -> Table:
    columns = (
        Column("Protocole"),
        Column("Appels", numeric=True),
        Column("Erreurs", numeric=True),
        Column("Octets émis", numeric=True, wide_only=True),
        Column("Octets reçus", numeric=True, wide_only=True),
        Column("Durée moyenne", numeric=True),
    )
    rows = []
    for protocol, totals in status["totals"].items():
        errors = totals["errors"]
        rows.append((
            protocol_text(protocol),
            fr_number(totals["calls"]),
            Text(fr_number(errors), style=f"bold {DANGER}" if errors else MUTED),
            fr_bytes(totals["bytes_out"]),
            fr_bytes(totals["bytes_in"]),
            fr_duration(totals["avg_ms"]) if totals["calls"] else "—",
        ))
    return fill_table(make_table("Appels tracés depuis le démarrage"), columns, rows, narrow=narrow)


def _summary(status: Mapping[str, Any]) -> Text:
    inventory = status["inventory"]
    relayed = sum(stats["connections_total"] for stats in status["proxies"].values())
    faults = sum(
        stats["resets"] + stats["refused"] + stats["blackholed"] + stats["lost_replies"]
        for stats in status["proxies"].values()
    )
    return Text.assemble(
        ("  Réseau simulé  ", MUTED), network_summary(status["network"]), "\n",
        ("  Proxys         ", MUTED),
        f"{fr_number(relayed)} connexions relayées · {fr_number(faults)} pannes injectées", "\n",
        ("  Inventaire     ", MUTED),
        f"{fr_number(inventory['products'])} produits · {fr_number(inventory['total_units'])} unités · "
        f"valeur {fr_number(inventory['inventory_value'], 2)} € · "
        f"{fr_number(inventory['low_stock_count'])} en stock bas · "
        f"{fr_number(inventory['operations'])} mouvements",
        "\n",
        ("  En service     ", MUTED), f"depuis {fr_duration(status['app']['uptime_s'] * 1000)}",
    )


def status_view(runtime: "LabRuntime", *, narrow: bool = False) -> RenderableType:
    """L'état complet : serveurs et ports, proxys de chaos, trafic, réseau simulé, inventaire."""
    status = runtime.status()
    return Group(
        servers_table(status, narrow=narrow),
        proxies_table(status, narrow=narrow),
        traffic_table(status, narrow=narrow),
        _summary(status),
    )


def activity_view(runtime: "LabRuntime", *, narrow: bool = False) -> RenderableType:
    """Ce qui bouge pendant que les serveurs tournent : trafic et résumé — assez court pour être redessiné
    sur place dans n'importe quel terminal (les ports, eux, ne changent pas : ``servers_table``)."""
    status = runtime.status()
    return Group(traffic_table(status, narrow=narrow), _summary(status))


def render_status(console: Console, runtime: "LabRuntime") -> None:
    console.print(status_view(runtime, narrow=is_narrow(console)))
    hint(console, "Chaque client peut viser le port direct (boucle locale idéale) ou celui du proxy, où le réseau "
                  "simulé s’applique.")
