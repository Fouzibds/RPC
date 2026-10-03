"""Laboratoire de pannes dans le terminal : chronologie en direct, puis verdict.

Chaque étape d'un scénario est écrite au moment où elle survient (``on_step``) :
on voit l'appel partir, la tentative échouer, l'attente, le disjoncteur s'ouvrir.
Les lignes ne sont jamais réécrites, la sortie reste donc lisible une fois
redirigée vers un fichier.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from benchmark_lab.failure_simulation import METRIC_INFO, SCENARIOS
from benchmark_lab.report import fr_compact, fr_duration, fr_number, fr_ratio
from common.config import PROTOCOL_LABELS
from common.errors import OK

from .theme import (
    ACCENT,
    BORDER,
    DANGER,
    INFO,
    MARK_FAIL,
    MARK_INFO,
    MARK_OK,
    MUTED,
    SUBTLE,
    SUCCESS,
    WARNING,
    add_number_column,
    bar_text,
    make_table,
    protocol_color,
    protocol_text,
    yes_no,
)

ALL_SCENARIOS = "all"

# Glyphe et couleur de chaque statut d'étape (failure_simulation.STEP_STATUSES).
STATUS_MARKS: dict[str, tuple[str, str]] = {
    "ok": (MARK_OK, SUCCESS),
    "slow": ("◷", WARNING),
    "timeout": (MARK_FAIL, DANGER),
    "error": (MARK_FAIL, DANGER),
    "retry": ("↻", WARNING),
    "info": (MARK_INFO, MUTED),
    "open": ("⊘", DANGER),
    "dedup": ("≡", INFO),
}
KIND_LABELS: dict[str, str] = {
    "local_call": "appel local",
    "rpc_call": "appel RPC",
    "attempt": "tentative",
    "backoff": "attente",
    "breaker": "disjoncteur",
    "network": "réseau",
    "state": "état",
    "note": "note",
}
# Étapes produites par la mécanique de résilience : décalées, elles se lisent comme le détail d'un appel.
_NESTED_KINDS: frozenset[str] = frozenset({"attempt", "backoff", "breaker"})
_TIME_WIDTH = 11
_KIND_WIDTH = 11
_DURATION_WIDTH = 10


def failure_scenarios(scenario: str) -> list[dict[str, Any]]:
    """Métadonnées des scénarios désignés par ``--scenario`` ; ``ValueError`` si l'identifiant est inconnu."""
    if scenario == ALL_SCENARIOS:
        return list(SCENARIOS)
    chosen = [meta for meta in SCENARIOS if meta["id"] == scenario]
    if not chosen:
        known = ", ".join(meta["id"] for meta in SCENARIOS)
        raise ValueError(f"Scénario inconnu : « {scenario} » (scénarios disponibles : {known}, ou {ALL_SCENARIOS})")
    return chosen


def scenario_heading(console: Console, rank: int, scenario: Mapping[str, Any], protocol: str) -> None:
    console.print()
    console.print(Rule(
        Text.assemble((f"{rank} · {scenario['title']} ", f"bold {ACCENT}"), ("· ", BORDER), protocol_text(protocol)),
        align="left", style=BORDER,
    ))
    console.print(Text.assemble((f"{scenario['concept']} — ", "bold"), (scenario["summary"], MUTED)))
    console.print()


class TimelinePrinter:
    """Observateur ``on_step`` : une ligne horodatée par étape, écrite dès qu'elle survient."""

    def __init__(self, console: Console) -> None:
        self._console = console

    def __call__(self, step: Mapping[str, Any]) -> None:
        detail = step["detail"]
        if "scenario" in detail:
            return      # étape-repère de début de scénario : l'en-tête vient de l'annoncer
        mark, color = STATUS_MARKS.get(step["status"], (MARK_INFO, MUTED))
        nested = step["kind"] in _NESTED_KINDS
        label = Text.assemble(("└ " if nested else "", BORDER), (step["label"], SUBTLE if nested else ""))
        duration = detail.get("duration_ms", detail.get("backoff_ms"))
        row = Table.grid(padding=(0, 1), expand=True)
        row.add_column(width=_TIME_WIDTH, justify="right", style=MUTED, no_wrap=True)
        row.add_column(width=1)
        row.add_column(width=_KIND_WIDTH, no_wrap=True)
        row.add_column(ratio=1)
        row.add_column(width=_DURATION_WIDTH, justify="right", style=MUTED, no_wrap=True)
        row.add_row(
            f"+{fr_number(step['t_ms'])} ms",
            Text(mark, style=f"bold {color}"),
            Text(KIND_LABELS.get(step["kind"], step["kind"]), style=color),
            label,
            "" if duration is None else fr_duration(duration),
        )
        self._console.print(row)


def _metric_value(value: Any, unit: str) -> Text:
    if isinstance(value, bool):
        return Text(yes_no(value))
    if isinstance(value, str):
        # Une issue d'appel est un code canonique : « OK » rassure, tout autre code est un échec.
        return Text(value or "—", style=f"bold {SUCCESS if value == OK else WARNING}" if value.isupper() else "")
    if unit == "×":
        return Text(fr_ratio(value), style="bold")
    if unit == "ms":
        return Text(fr_duration(value))
    return Text(fr_compact(value))


def verdict_panel(result: Mapping[str, Any], *, narrow: bool = False) -> Panel:
    """Conclusion d'un scénario : la phrase de verdict, ses mesures, et la règle à retenir.

    Les durées sont aussi dessinées en barres, à la même échelle — sauf en mode ``narrow``.
    """
    color = protocol_color(result["protocol"])
    metrics = result["metrics"]
    longest = 0.0 if narrow else max(
        (value for name, value in metrics.items()
         if METRIC_INFO.get(name, {}).get("unit") == "ms" and isinstance(value, (int, float))
         and not isinstance(value, bool)),
        default=0.0,
    )
    table = make_table()
    table.add_column("Mesure")
    add_number_column(table, "Valeur")
    if longest:
        table.add_column("Durées, à la même échelle", style=MUTED)
    for name, value in metrics.items():
        info = METRIC_INFO.get(name, {"label": name, "unit": ""})
        timed = info["unit"] == "ms" and isinstance(value, (int, float)) and not isinstance(value, bool)
        row = [info["label"], _metric_value(value, info["unit"])]
        if longest:
            row.append(bar_text(value, longest, color, 24) if timed else "")
        table.add_row(*row)
    body: list[RenderableType] = [
        Text(result["verdict"], style="bold"),
        Text(),
        table,
        Text(),
        Text.assemble(("À retenir — ", f"bold {ACCENT}"), result["lesson"]),
    ]
    return Panel(
        Group(*body),
        title=Text(f"Verdict · {result['title']}", style=f"bold {ACCENT}"),
        title_align="left",
        subtitle=Text(
            f"{PROTOCOL_LABELS.get(result['protocol'], result['protocol'])} · scénario joué en "
            f"{fr_duration(result.get('duration_ms'))}",
            style=MUTED,
        ),
        subtitle_align="right",
        box=box.ROUNDED,
        border_style=ACCENT,
        padding=(1, 2),
    )


def campaign_summary(console: Console, results: Sequence[Mapping[str, Any]], elapsed_s: float) -> None:
    count = len(results)
    console.print()
    console.print(Text.assemble(
        (f"  {MARK_OK} ", f"bold {SUCCESS}"),
        f"{count} scénario{'s' if count > 1 else ''} en {fr_duration(elapsed_s * 1000)}",
        (" — réseau, stocks et proxys remis dans l'état où ils étaient.", MUTED),
    ))
