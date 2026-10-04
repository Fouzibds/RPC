"""Transparence de localisation : la même opération, écrite de quatre façons, côte à côte.

Les extraits affichés sont les sources réelles des fonctions de
``benchmark_lab.transparency_demo`` (``inspect.getsource``) : ce qui se lit ici
est ce qui s'exécute juste après, dans le tableau de comparaison.
"""
from __future__ import annotations

from typing import Any, Mapping

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from benchmark_lab.report import fr_duration, fr_number

from .theme import (
    DANGER,
    MUTED,
    SUBTLE,
    SUCCESS,
    Column,
    fill_table,
    is_narrow,
    make_table,
    protocol_color,
    protocol_text,
)


def _snippet_panel(snippet: Mapping[str, Any]) -> Panel:
    color = protocol_color(snippet["id"])
    concerns = snippet["concerns"]
    footer = Text()
    if concerns:
        footer.append(f"À la charge de l’appelant : {len(concerns)} préoccupations", style=f"bold {SUBTLE}")
        footer.append("".join(f"\n  · {concern}" for concern in concerns), style=MUTED)
    elif snippet["id"] == "local":
        footer.append("Rien à gérer : un appel de méthode ordinaire, dans le même processus", style=f"bold {SUBTLE}")
    else:
        footer.append("Rien à la charge de l’appelant : le stub s’occupe de tout", style=f"bold {SUBTLE}")
    body: list[RenderableType] = [
        Syntax(snippet["code"], snippet["language"], theme="ansi_dark", background_color="default", word_wrap=True),
        Text(),
        footer,
    ]
    plural = "s" if snippet["lines"] > 1 else ""
    return Panel(
        Group(*body),
        title=Text(snippet["title"], style=f"bold {color}"),
        title_align="left",
        subtitle=Text(f"{snippet['lines']} ligne{plural} utile{plural}", style=f"bold {color}"),
        subtitle_align="right",
        box=box.ROUNDED,
        border_style=color,
        padding=(0, 1),
    )


def render_snippets(console: Console, comparison: Mapping[str, Any]) -> None:
    """Les quatre écritures : les trois courtes à gauche, le REST « à la main » à droite."""
    panels = {snippet["id"]: _snippet_panel(snippet) for snippet in comparison["snippets"]}
    console.print(Text(f"  {comparison['operation']}", style="bold"))
    console.print()
    if is_narrow(console):      # deux colonnes de code y seraient illisibles : on empile
        for panel in panels.values():
            console.print(panel)
        return
    with_stub = Group(*(panel for identifier, panel in panels.items() if identifier != "rest"))
    layout = Table.grid(expand=True, padding=(0, 1))
    layout.add_column(ratio=1)
    layout.add_column(ratio=1)
    layout.add_row(with_stub, panels["rest"])
    console.print(layout)


def render_runs(console: Console, comparison: Mapping[str, Any], outcome: Mapping[str, Any]) -> None:
    """Les quatre écritures exécutées sur le laboratoire : même résultat, effort et durée différents."""
    runs = {run["id"]: run for run in outcome["runs"]}
    columns = (
        Column("Écriture"),
        Column("Lignes", numeric=True),
        Column("À gérer soi-même", numeric=True, wide_only=True),
        Column("Nouveau stock", numeric=True),
        Column("Durée médiane", numeric=True),
        Column("× local", numeric=True, wide_only=True),
    )
    rows = []
    for snippet in comparison["snippets"]:
        run = runs[snippet["id"]]
        result = (
            Text(fr_number(run["result"]), style=f"bold {SUCCESS}") if run["ok"]
            else Text(run["error"]["code"], style=f"bold {DANGER}")
        )
        ratio = run.get("ratio_to_local")
        rows.append((
            protocol_text(snippet["id"], snippet["title"]),
            fr_number(snippet["lines"]),
            fr_number(len(snippet["concerns"])),
            result,
            fr_duration(run["duration_ms"]),
            "—" if ratio is None else f"× {fr_number(ratio)}",
        ))
    table = make_table(
        "Les quatre écritures, exécutées",
        caption=f"Stock de {outcome['product_id']} avant l’opération : {fr_number(outcome['stock_before'])} — "
                f"rétabli après chaque appel. Durée : médiane de {fr_number(outcome['repeats'])} appels.",
    )
    console.print(fill_table(table, columns, rows, narrow=is_narrow(console)))
    console.print()
    console.print(Text(outcome["summary"], style=f"bold {SUCCESS if outcome['equivalent'] else DANGER}"))
    console.print()
    console.print(Text(comparison["takeaway"], style=SUBTLE))
