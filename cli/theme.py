"""Langage visuel du CLI : palette, glyphes, console et briques d'affichage.

Tous les écrans passent par ce module : une couleur par protocole (la même que
dans le dashboard), les mêmes glyphes d'état, les mêmes tableaux à bordure
arrondie, les mêmes nombres à la française. Un appel gRPC est sarcelle partout,
une erreur est rouge partout : l'œil apprend le code une fois.

Les textes sont toujours des ``Text`` construits à la main, jamais du balisage
Rich : les messages affichés contiennent des crochets (JSON, signatures) que le
balisage interpréterait.
"""
from __future__ import annotations

import json
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence, TextIO

from rich import box
from rich.console import Console, Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from benchmark_lab.report import MISSING, fr_duration
from common.config import APP_NAME, APP_TAGLINE, PROTOCOL_LABELS, VERSION
from common.errors import RpcError

# --- Palette (plan §14 : identique à celle du dashboard) ---------------------

ACCENT = "#8B7CFF"
SUCCESS = "#4ADE80"
WARNING = "#FBBF24"
DANGER = "#F87171"
INFO = "#60A5FA"
ORANGE = "#FB923C"
SUBTLE = "#A9B1BF"     # texte secondaire
MUTED = "grey58"       # légendes, unités
BORDER = "grey35"      # filets et bordures de tableaux

PROTOCOL_COLORS: dict[str, str] = {
    "local": "#8A94A6",
    "custom": "#5AA2FF",
    "grpc": "#2FD9C4",
    "rest": "#F2789F",
}
TONE_COLORS: dict[str, str] = {"success": SUCCESS, "warning": WARNING, "danger": DANGER, "info": INFO}

# --- Glyphes -----------------------------------------------------------------

MARK_OK = "✓"
MARK_FAIL = "✗"
MARK_WARN = "!"
MARK_INFO = "·"
DOT = "●"
PROMPT = "›"

MAX_WIDTH = 118
NARROW_WIDTH = 100     # en dessous, les tableaux ne gardent que leurs colonnes essentielles
_BAR_EIGHTHS = " ▏▎▍▌▋▊▉"
_SPARK_LEVELS = "▁▂▃▄▅▆▇█"


# --- Console -----------------------------------------------------------------

def make_console(file: TextIO | None = None, *, width: int | None = None) -> Console:
    """Console du CLI : largeur bornée, ni balisage ni coloration automatique des textes."""
    stream = sys.stdout if file is None else file
    if width is None:
        width = min(shutil.get_terminal_size((MAX_WIDTH, 40)).columns, MAX_WIDTH)
    is_tty = hasattr(stream, "isatty") and stream.isatty()
    return Console(
        file=stream,
        width=width,
        highlight=False,
        markup=False,
        emoji=False,
        # Hors terminal, Rich croirait à une vieille console Windows et dessinerait des angles droits.
        legacy_windows=None if is_tty else False,
    )


def is_live(console: Console) -> bool:
    """Vrai si l'on peut redessiner l'écran (barres de progression, attente animée)."""
    return console.is_terminal and not console.is_dumb_terminal


@contextmanager
def activity(console: Console, message: str) -> Iterator[None]:
    """Indicateur d'attente animé le temps d'un bloc ; rien du tout si la sortie n'est pas un terminal."""
    if not is_live(console):
        yield
        return
    with console.status(Text(message, style=MUTED), spinner="dots", spinner_style=ACCENT):
        yield


# --- Structure d'un écran ----------------------------------------------------

def header(console: Console, subtitle: str, detail: str = "") -> None:
    """En-tête de marque, compact : nom du produit, version, et l'écran où l'on se trouve."""
    brand = Text.assemble(
        ("◆ ", f"bold {ACCENT}"), (APP_NAME, f"bold {ACCENT}"), (" · ", MUTED), (APP_TAGLINE, "bold"),
    )
    title = Table.grid(expand=True)
    title.add_column(ratio=1)
    title.add_column(justify="right")
    title.add_row(brand, Text(f"v{VERSION}", style=MUTED))
    lines: list[RenderableType] = [title, Text(subtitle, style=SUBTLE)]
    if detail:
        lines.append(Text(detail, style=MUTED))
    console.print()
    console.print(Panel(Group(*lines), box=box.ROUNDED, border_style=ACCENT, padding=(0, 1)))


def section(console: Console, title: str, lead: str = "", *, number: int | None = None) -> None:
    label = title if number is None else f"{number} · {title}"
    console.print()
    console.print(Rule(Text(label, style=f"bold {ACCENT}"), align="left", style=BORDER))
    if lead:
        console.print(Text(lead, style=MUTED))
    console.print()


def make_table(title: str | None = None, *, caption: str | None = None, expand: bool = False) -> Table:
    """Tableau du CLI : bordure arrondie discrète, titre et légende alignés à gauche."""
    return Table(
        title=title,
        title_style="bold",
        title_justify="left",
        caption=caption,
        caption_style=MUTED,
        caption_justify="left",
        box=box.ROUNDED,
        border_style=BORDER,
        header_style=f"bold {SUBTLE}",
        expand=expand,
    )


def add_number_column(table: Table, heading: str, *, style: str | None = None) -> None:
    """Colonne de nombres : alignée à droite, jamais coupée — les chiffres se comparent en colonne."""
    table.add_column(heading, justify="right", no_wrap=True, style=style)


@dataclass(frozen=True)
class Column:
    """Colonne d'un tableau adaptatif (voir ``fill_table``)."""

    heading: str
    numeric: bool = False          # nombres : alignés à droite
    style: str | None = None
    wide_only: bool = False        # colonne de confort : omise quand le terminal est étroit
    no_wrap: bool = True


def is_narrow(console: Console) -> bool:
    """Terminal étroit : mieux vaut moins de colonnes que des nombres tronqués."""
    return console.width < NARROW_WIDTH


def fill_table(
    table: Table,
    columns: Sequence[Column],
    rows: Iterable[Sequence[RenderableType] | None],
    *,
    narrow: bool = False,
) -> Table:
    """Remplit ``table`` ; en mode étroit, les colonnes ``wide_only`` (et leurs cellules) sont omises.

    Chaque ligne donne une cellule par colonne déclarée ; ``None`` sépare deux groupes de lignes.
    """
    kept = [index for index, column in enumerate(columns) if not (narrow and column.wide_only)]
    for index in kept:
        column = columns[index]
        table.add_column(
            column.heading, justify="right" if column.numeric else "left", no_wrap=column.no_wrap, style=column.style
        )
    for row in rows:
        if row is None:
            table.add_section()
        else:
            table.add_row(*(row[index] for index in kept))
    return table


def key_value_grid(rows: Iterable[tuple[str, RenderableType]]) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=MUTED, no_wrap=True)
    grid.add_column(ratio=1)
    for label, value in rows:
        grid.add_row(label, value)
    return grid


def note(console: Console, text: str, *, mark: str = MARK_OK, color: str = SUCCESS) -> None:
    """Constat d'une ligne précédé de son glyphe ; s'il déborde, la suite reste alignée sous le texte."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text(f"  {mark}", style=f"bold {color}"), Text(text))
    console.print(grid)


def hint(console: Console, text: str) -> None:
    console.print(Padding(Text(text, style=MUTED), (0, 0, 0, 2)))


# --- Étiquettes --------------------------------------------------------------

def protocol_color(protocol: str) -> str:
    return PROTOCOL_COLORS.get(protocol, SUBTLE)


def protocol_text(protocol: str, label: str | None = None) -> Text:
    """Nom d'un protocole précédé de sa pastille de couleur."""
    name = PROTOCOL_LABELS.get(protocol, protocol) if label is None else label
    return Text.assemble((f"{DOT} ", protocol_color(protocol)), (name, f"bold {protocol_color(protocol)}"))


def badge(label: str, color: str) -> Text:
    # Le style est porté par un segment, pas par le Text entier : ce qu'on y ajoute ensuite n'en hérite pas.
    return Text.assemble((f" {label} ", f"bold {color} reverse"))


def status_mark(ok: bool) -> Text:
    return Text(MARK_OK, style=f"bold {SUCCESS}") if ok else Text(MARK_FAIL, style=f"bold {DANGER}")


def yes_no(value: bool) -> str:
    return "oui" if value else "non"


# --- Graphiques en caractères ------------------------------------------------

def bar(value: float | None, maximum: float, width: int = 28) -> str:
    """Barre horizontale au huitième de caractère près ; jamais vide pour une valeur non nulle."""
    if not value or value < 0 or maximum <= 0:
        return ""
    eighths = max(round(min(value / maximum, 1.0) * width * 8), 1)
    full, rest = divmod(eighths, 8)
    return "█" * full + (_BAR_EIGHTHS[rest] if rest else "")


def bar_text(value: float | None, maximum: float, color: str, width: int = 28) -> Text:
    return Text(bar(value, maximum, width), style=color)


def sparkline(values: Sequence[float], width: int = 32) -> str:
    """Courbe miniature : ``values`` ramenées à ``width`` points (moyenne par tranche), sur huit niveaux."""
    if not values:
        return ""
    count = min(width, len(values))
    points = []
    for index in range(count):
        chunk = values[index * len(values) // count:(index + 1) * len(values) // count]
        points.append(sum(chunk) / len(chunk))
    low, span = min(points), max(points) - min(points)
    if span <= 0:
        return _SPARK_LEVELS[0] * count
    return "".join(_SPARK_LEVELS[round((point - low) / span * (len(_SPARK_LEVELS) - 1))] for point in points)


# --- Valeurs -----------------------------------------------------------------

def fr_micros(duration_us: float | None) -> str:
    """Durée d'une étape, donnée en microsecondes par le bus de traces."""
    return MISSING if duration_us is None else fr_duration(duration_us / 1000)


def compact_json(value: Any, limit: int = 96) -> str:
    """Valeur sur une ligne, abrégée : pour une cellule de tableau ou un élément de flux."""
    text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "), default=str)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def json_block(value: Any, max_lines: int | None = None) -> RenderableType:
    """JSON indenté et coloré ; au-delà de ``max_lines``, la suite est résumée en une ligne."""
    lines = json.dumps(value, indent=2, ensure_ascii=False, default=str).splitlines()
    hidden = 0
    if max_lines is not None and len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = lines[:max_lines]
    code = Syntax("\n".join(lines), "json", theme="ansi_dark", background_color="default", word_wrap=True)
    if not hidden:
        return code
    return Group(code, Text(f"… {hidden} lignes de plus (résultat abrégé à l’affichage)", style=MUTED))


def error_panel(error: RpcError, footer: str = "") -> Panel:
    """Erreur d'appel distant : code canonique, message, et ce qu'un client peut en faire."""
    if error.retryable:
        advice = "oui — panne de transport : l’appel peut être retenté, s’il est idempotent"
    else:
        advice = "non — rejouer le même appel donnerait la même erreur"
    rows: list[tuple[str, RenderableType]] = [
        ("Code", Text(error.code, style=f"bold {DANGER}")),
        ("Message", Text(error.message)),
        ("Exception", Text(type(error).__name__)),
        ("Rejouable", Text(advice)),
    ]
    if error.detail:
        rows.append(("Détail", Text(compact_json(error.detail, 160), style=SUBTLE)))
    return Panel(
        key_value_grid(rows),
        title=Text(f"{MARK_FAIL} Erreur RPC · {error.code}", style=f"bold {DANGER}"),
        title_align="left",
        subtitle=Text(footer, style=MUTED) if footer else None,
        subtitle_align="right",
        box=box.ROUNDED,
        border_style=DANGER,
        padding=(0, 1),
    )


def failure_panel(title: str, message: str, advice: str = "") -> Panel:
    """Échec du programme lui-même (port occupé, option incohérente), distinct d'une erreur RPC."""
    body: list[RenderableType] = [Text(message)]
    if advice:
        body += [Text(), Text(advice, style=SUBTLE)]
    return Panel(
        Group(*body),
        title=Text(f"{MARK_FAIL} {title}", style=f"bold {DANGER}"),
        title_align="left",
        box=box.ROUNDED,
        border_style=DANGER,
        padding=(0, 1),
    )
