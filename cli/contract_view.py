"""Laboratoire « Contrat » dans le terminal : ce qui a changé entre v1 et v2, et ce que cela casse.

Trois temps : le diff des deux fichiers ``.proto`` (comme un ``git diff``), la
liste des changements classés BREAKING ou COMPATIBLE, puis chaque scénario —
un client resté en v1 face au serveur v2 — avec ce qu'un serveur v1 aurait
répondu, ce qui s'est réellement passé, et le verdict.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from common.errors import OK

from .theme import (
    ACCENT,
    BORDER,
    DANGER,
    MARK_FAIL,
    MARK_OK,
    MUTED,
    ORANGE,
    SUBTLE,
    SUCCESS,
    WARNING,
    badge,
    compact_json,
    key_value_grid,
    make_table,
    protocol_text,
)

# Issue d'un scénario → étiquette et couleur, de la plus bénigne à la plus dangereuse.
OUTCOME_BADGES: dict[str, tuple[str, str]] = {
    "compatible": ("COMPATIBLE", SUCCESS),
    "rejected": ("REJET", WARNING),
    "crash": ("CRASH", ORANGE),
    "silent_corruption": ("CORRUPTION SILENCIEUSE", DANGER),
}
_KIND_BADGES: dict[str, tuple[str, str]] = {"breaking": ("BREAKING", DANGER), "compatible": ("COMPATIBLE", SUCCESS)}
_SEVERITIES: dict[str, tuple[str, str]] = {
    "critical": ("critique", DANGER),
    "major": ("majeure", ORANGE),
    "minor": ("mineure", WARNING),
    "info": ("nulle", MUTED),
}
_DIFF_SIGNS: dict[str, tuple[str, str]] = {"same": (" ", MUTED), "removed": ("-", DANGER), "added": ("+", SUCCESS)}
_DIFF_CONTEXT = 2
_MAX_DIFFERENCES = 4


def render_diff(console: Console, overview: Mapping[str, Any]) -> None:
    """Diff ligne à ligne des deux contrats : seuls les blocs modifiés, avec un peu de contexte."""
    diff, stats = overview["diff"], overview["stats"]
    console.print(Text.assemble(
        (overview["proto_v1"]["path"], "bold"), ("  →  ", MUTED), (overview["proto_v2"]["path"], "bold"),
        ("    ", ""), (f"−{stats['lines_removed']}", f"bold {DANGER}"), (" ", ""),
        (f"+{stats['lines_added']}", f"bold {SUCCESS}"),
    ))
    changed = [index for index, row in enumerate(diff) if row["kind"] != "same"]
    visible: set[int] = set()
    for index in changed:
        visible.update(range(max(index - _DIFF_CONTEXT, 0), min(index + _DIFF_CONTEXT + 1, len(diff))))
    previous = -1
    for index in sorted(visible):
        if index != previous + 1:
            console.print(Text("        ⋯", style=BORDER))
        row = diff[index]
        sign, style = _DIFF_SIGNS[row["kind"]]
        left = "" if row["left_no"] is None else str(row["left_no"])
        right = "" if row["right_no"] is None else str(row["right_no"])
        line = Text.assemble((f"{left:>4} {right:>4} ", BORDER), (f"{sign} {row['text']}", style))
        console.print(line, soft_wrap=True)
        previous = index


def render_changes(console: Console, changes: Sequence[Mapping[str, Any]]) -> None:
    """Tous les changements du contrat, étiquetés BREAKING ou COMPATIBLE."""
    table = make_table(expand=True)
    table.show_lines = True
    table.add_column("Nature", no_wrap=True)
    table.add_column("Changement", ratio=4)
    table.add_column("Contrat v1", ratio=3, overflow="fold")
    table.add_column("Contrat v2", ratio=3, overflow="fold")
    for change in changes:
        label, color = _KIND_BADGES.get(change["kind"], (str(change["kind"]).upper(), SUBTLE))
        severity, severity_color = _SEVERITIES.get(change["severity"], (change["severity"], SUBTLE))
        table.add_row(
            Text.assemble(badge(label, color), "\n", (f"gravité {severity}", severity_color)),
            Text.assemble((change["title"], "bold"), "\n", (change["element"], MUTED)),
            Text(change["v1"] or "—", style="" if change["v1"] else MUTED),
            Text(change["v2"] or "—", style="" if change["v2"] else MUTED),
        )
    console.print(table)


def _status_text(status: str) -> Text:
    return Text(status, style=f"bold {SUCCESS if status == OK else DANGER}")


def scenario_panel(result: Mapping[str, Any], rank: int) -> Panel:
    """Un scénario : l'appel du client v1, attendu contre observé, le verdict et son explication."""
    label, color = OUTCOME_BADGES.get(result["outcome"], (str(result["outcome"]).upper(), SUBTLE))
    expected, observed = result["expected"], result["observed"]
    conformity = (
        Text("issue conforme au scénario", style=MUTED) if result["matches"]
        else Text(f"{MARK_FAIL} issue INATTENDUE pour ce scénario", style=f"bold {DANGER}")
    )
    verdict = Text.assemble(badge(label, color), ("  ", ""), (f"statut {result['status']}", "bold"), ("  ·  ", MUTED))
    verdict.append_text(conformity)

    comparison = Table(box=box.SIMPLE_HEAD, border_style=BORDER, header_style=f"bold {SUBTLE}", expand=True,
                       pad_edge=False)
    comparison.add_column("")
    comparison.add_column("Attendu — ce qu'un serveur v1 aurait fait", ratio=1)
    comparison.add_column("Observé — face au serveur v2", ratio=1)
    comparison.add_row(Text("Statut", style=MUTED), _status_text(expected["status"]), _status_text(observed["status"]))
    comparison.add_row(Text("Effet", style=MUTED), Text(expected["summary"]), Text(observed["summary"], style=color))

    body: list[RenderableType] = [
        verdict,
        Text(),
        key_value_grid([
            ("Appel du client v1", Text(result["sent"]["call"], style="bold")),
            ("Cible sur le fil", Text(str(result["sent"]["target"]), style=SUBTLE)),
        ]),
        comparison,
    ]
    differences = observed.get("differences") or []
    if differences:
        lines = Text()
        for index, difference in enumerate(differences[:_MAX_DIFFERENCES]):
            side = "vu par le client" if difference["where"] == "client" else "état du serveur"
            lines.append("\n" if index else "")
            lines.append(f"{difference['path']} ", style="bold")
            lines.append(f"({side}) : attendu {compact_json(difference['expected'], 32)}, "
                         f"observé {compact_json(difference['observed'], 32)}")
        if len(differences) > _MAX_DIFFERENCES:
            lines.append(f"\n… {len(differences) - _MAX_DIFFERENCES} écarts de plus", style=MUTED)
        body += [key_value_grid([("Écarts", lines)]), Text()]
    body.append(Text(result["explanation"], style=SUBTLE))
    return Panel(
        Group(*body),
        title=Text.assemble((f"{rank} · ", "bold"), protocol_text(result["protocol"]),
                            (f" · {result['title']}", "bold")),
        title_align="left",
        box=box.ROUNDED,
        border_style=color,
        padding=(0, 1),
    )


def render_rules(console: Console, rules: Sequence[Mapping[str, Any]]) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1)
    for rule in rules:
        mark, label, color = (MARK_OK, "à faire", SUCCESS) if rule["good"] else (MARK_FAIL, "jamais", DANGER)
        grid.add_row(
            Text(f"{mark} {label}", style=f"bold {color}"),
            Text.assemble((rule["title"], "bold"), "\n", (rule["text"], MUTED), "\n"),
        )
    console.print(grid)


def render_tally(console: Console, results: Sequence[Mapping[str, Any]]) -> None:
    """Bilan : combien de scénarios par issue, de la plus bénigne à la plus dangereuse."""
    tally = Text.assemble(("  Bilan  ", f"bold {ACCENT}"))
    for outcome, (label, color) in OUTCOME_BADGES.items():
        count = sum(result["outcome"] == outcome for result in results)
        if count:
            tally.append(f"{count} × ", style="bold")
            tally.append_text(badge(label, color))
            tally.append("   ")
    console.print(tally)
