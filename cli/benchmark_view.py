"""Banc d'essai dans le terminal : progression en direct, puis les tableaux comparatifs.

Le rapport affiché est le dictionnaire de ``run_full_benchmark`` : ce module ne
calcule rien, il met en page. L'ordre des sections est celui du cahier des
charges — d'abord ce que pèse un appel (JSON contre Protobuf, puis les octets
réellement échangés), ensuite ce qu'il coûte en temps (local contre RPC maison,
gRPC et REST), enfin ce que le réseau en fait.

Dans un terminal étroit, chaque tableau ne garde que ses colonnes essentielles :
mieux vaut moins de colonnes que des nombres tronqués.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Column as ProgressCell
from rich.table import Table
from rich.text import Text

from benchmark_lab.benchmark_perf import SUITE_LABELS
from benchmark_lab.report import (
    MISSING,
    fr_bytes,
    fr_compact,
    fr_duration,
    fr_number,
    fr_percent,
    save_report,
    to_markdown,
)
from common.config import PROTOCOL_LABELS

from .theme import (
    ACCENT,
    BORDER,
    DANGER,
    MARK_INFO,
    MARK_OK,
    MARK_WARN,
    MUTED,
    PROTOCOL_COLORS,
    SUBTLE,
    SUCCESS,
    TONE_COLORS,
    WARNING,
    Column,
    bar_text,
    fill_table,
    is_live,
    is_narrow,
    make_table,
    note,
    protocol_color,
    protocol_text,
    section,
    sparkline,
)

PAYLOAD_TITLE = "Taille du paquet (octets) : JSON vs Protobuf"
WIRE_TITLE = "Octets réels sur le fil par appel"
LATENCY_TITLE = "Temps moyen par appel (ms) : Local vs RPC maison vs gRPC vs REST"

_DIRECTIONS = {"request": "requête", "response": "réponse"}
_FORMATS = {"json": "JSON", "protobuf": "Protobuf"}
_FORMAT_COLORS = {"json": PROTOCOL_COLORS["custom"], "protobuf": PROTOCOL_COLORS["grpc"]}
_TONE_MARKS = {"success": MARK_OK, "warning": MARK_WARN, "info": MARK_INFO}
_SIZE_CAPTION = (
    "JSON, Protobuf : le message sérialisé seul. Trames : le même message derrière son préfixe "
    "(4 octets en JSON-RPC, 5 en gRPC). HTTP : la requête ou la réponse REST entière, en-têtes compris."
)
_WIDE_BAR, _NARROW_BAR = 40, 22

# Colonnes communes aux deux tableaux de tailles ; les tailles tramées sont un détail de confort.
_SIZE_COLUMNS: tuple[Column, ...] = (
    Column("JSON", numeric=True, style=_FORMAT_COLORS["json"]),
    Column("Protobuf", numeric=True, style=_FORMAT_COLORS["protobuf"]),
    Column("Économie", numeric=True),
    Column("Trame JSON-RPC", numeric=True, style=MUTED, wide_only=True),
    Column("Trame gRPC", numeric=True, style=MUTED, wide_only=True),
    Column("HTTP (REST)", numeric=True, style=MUTED, wide_only=True),
)


# --- Progression -------------------------------------------------------------

class BenchmarkProgress:
    """Rappel ``progress`` de ``run_full_benchmark``.

    Dans un terminal : une barre animée avec le temps écoulé et le temps restant
    estimé. Sortie redirigée : une ligne par suite, sans rien redessiner.
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._phase = ""
        self._started = 0.0
        self.elapsed_s = 0.0          # durée du bloc ``with``, connue à sa sortie
        self._progress: Progress | None = None
        self._task: Any = None
        if is_live(console):
            self._progress = Progress(
                SpinnerColumn(style=ACCENT),
                BarColumn(bar_width=30, complete_style=ACCENT, finished_style=SUCCESS, pulse_style=BORDER),
                TaskProgressColumn(text_format="{task.percentage:>3.0f} %", style="bold", markup=False),
                TimeElapsedColumn(),
                TextColumn("reste", style=MUTED, markup=False),
                TimeRemainingColumn(compact=True),
                TextColumn(
                    "{task.description}", style=SUBTLE, markup=False,
                    table_column=ProgressCell(ratio=1, no_wrap=True, overflow="ellipsis"),
                ),
                console=console,
                transient=True,
                expand=True,
            )

    def __enter__(self) -> "BenchmarkProgress":
        self._started = time.perf_counter()
        if self._progress is not None:
            self._progress.start()
            self._task = self._progress.add_task("Préparation…", total=1.0)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.elapsed_s = time.perf_counter() - self._started
        if self._progress is not None:
            self._progress.stop()

    def __call__(self, phase: str, fraction: float, message: str, partial: dict[str, Any] | None = None) -> None:
        if self._progress is not None:
            self._progress.update(self._task, completed=fraction, description=message)
        elif phase != self._phase and phase != "done":
            self._console.print(Text(f"  {MARK_INFO} {SUITE_LABELS.get(phase, message)}…", style=MUTED))
        self._phase = phase


# --- Cellules ----------------------------------------------------------------

def _saving(percent: float | None) -> Text:
    """Écart Protobuf / JSON : vert quand Protobuf est plus petit."""
    if percent is None:
        return Text(MISSING, style=MUTED)
    return Text(fr_percent(percent, signed=True), style=f"bold {SUCCESS if percent < 0 else DANGER}")


def _size_cells(row: Mapping[str, Any]) -> tuple[RenderableType, ...]:
    """Cellules des colonnes ``_SIZE_COLUMNS`` pour une ligne de tailles."""
    return (
        fr_number(row.get("json_bytes")),
        fr_number(row.get("protobuf_bytes")),
        _saving(row.get("protobuf_vs_json_pct")),
        fr_number(row.get("json_rpc_bytes")),
        fr_number(row.get("grpc_bytes")),
        fr_number(row.get("rest_bytes")),
    )


def _size_bar(row: Mapping[str, Any], message_format: str) -> Text:
    """Barre d'une taille de message, à l'échelle de la taille JSON de la même ligne."""
    return bar_text(row.get(f"{message_format}_bytes"), row["json_bytes"], _FORMAT_COLORS[message_format], 24)


def _milliseconds(value: float | None) -> str:
    return fr_number(value, 4)


def _microseconds(nanoseconds: float | None) -> str:
    return fr_number(None if nanoseconds is None else nanoseconds / 1000, 2)


def _grouped(rows: Sequence[Mapping[str, Any]], key: str) -> list[tuple[Mapping[str, Any], bool]]:
    """Chaque ligne avec un drapeau : vrai si elle ouvre un nouveau groupe (valeur de ``key`` différente)."""
    return [(row, index == 0 or rows[index - 1][key] != row[key]) for index, row in enumerate(rows)]


def _chart(rows: Sequence[Sequence[RenderableType]]) -> Table:
    """Diagramme en barres horizontales : une ligne par série, cellules alignées en colonnes."""
    grid = Table.grid(padding=(0, 2))
    for _ in rows[0]:
        grid.add_column(no_wrap=True)
    for row in rows:
        grid.add_row(*row)
    return grid


# --- Sections ----------------------------------------------------------------

def _payload_section(console: Console, payload: Mapping[str, Any]) -> None:
    rows = payload.get("rows") or []
    if not rows:
        return
    narrow = is_narrow(console)
    cells: list[Sequence[RenderableType] | None] = []
    for row, opens_group in _grouped(rows, "method"):
        if opens_group and cells:
            cells.append(None)
        cells.append((
            Text(row["method"], style="bold") if opens_group else "",
            _DIRECTIONS.get(row["direction"], row["direction"]),
            *_size_cells(row),
        ))
    columns = (Column("Procédure"), Column("Sens", style=SUBTLE), *_SIZE_COLUMNS)
    console.print(fill_table(make_table(PAYLOAD_TITLE, caption=_SIZE_CAPTION), columns, cells, narrow=narrow))

    responses = [row for row in rows if row["direction"] == "response" and row.get("json_bytes")]
    if responses and not narrow:
        console.print()
        console.print(Text("Réponses, à la même échelle par procédure", style=MUTED))
        console.print(_chart([
            (
                Text(row["method"], style="bold"),
                Text.assemble(("JSON ", MUTED), _size_bar(row, "json")),
                fr_bytes(row["json_bytes"]),
                Text.assemble(("Protobuf ", MUTED), _size_bar(row, "protobuf")),
                fr_bytes(row.get("protobuf_bytes")),
            )
            for row in responses
        ]))


def _wire_section(console: Console, payload: Mapping[str, Any]) -> None:
    rows = payload.get("wire") or []
    if not rows:
        return
    heaviest = max((row.get("total_per_call") or 0 for row in rows), default=0)
    columns = (
        Column("Protocole"),
        Column("Appels", numeric=True, wide_only=True),
        Column("Client → serveur", numeric=True),
        Column("Serveur → client", numeric=True),
        Column("Total par appel", numeric=True, style="bold"),
        Column("", wide_only=True),
    )
    table = make_table(
        WIRE_TITLE,
        caption=f"Appel {rows[0].get('method', '')} sur une connexion déjà ouverte, compté par les proxys de chaos : "
                "la seule mesure qui voit les trames HTTP/2, les en-têtes compressés et les accusés applicatifs.",
    )
    console.print()
    console.print(fill_table(table, columns, [
        (
            protocol_text(row["protocol"]),
            fr_number(row.get("calls")),
            fr_number(row.get("bytes_up_per_call"), 1),
            fr_number(row.get("bytes_down_per_call"), 1),
            fr_number(row.get("total_per_call"), 1),
            bar_text(row.get("total_per_call"), heaviest, protocol_color(row["protocol"]), 28),
        )
        for row in rows
    ], narrow=is_narrow(console)))


def _scaling_section(console: Console, payload: Mapping[str, Any]) -> None:
    rows = payload.get("scaling") or []
    if not rows:
        return
    table = make_table("Montée en charge de list_products : taille de la réponse (octets)")
    columns = (Column("Produits", numeric=True, style="bold"), *_SIZE_COLUMNS)
    console.print()
    console.print(fill_table(
        table, columns, [(fr_number(row["items"]), *_size_cells(row)) for row in rows], narrow=is_narrow(console)
    ))


def _latency_caption(latency: Mapping[str, Any]) -> str:
    clients = latency.get("concurrency", 1)
    route = "à travers le proxy de chaos" if latency.get("via_proxy") else "accès direct en boucle locale"
    return (
        f"{latency.get('method', '')} · {fr_number(latency.get('iterations'))} appels par protocole · "
        f"{fr_number(clients)} client{'s' if clients > 1 else ''} · {route} · bus de traces coupé · "
        f"{fr_number(latency.get('warmup'))} appels d'échauffement"
    )


def _latency_section(console: Console, latency: Mapping[str, Any]) -> None:
    results = latency.get("results") or []
    if not results:
        return
    narrow = is_narrow(console)
    columns = (
        Column("Protocole"),
        Column("Appels", numeric=True, wide_only=True),
        Column("Erreurs", numeric=True, wide_only=True),
        Column("Moyenne", numeric=True, style="bold"),
        Column("Médiane", numeric=True),
        Column("p95", numeric=True),
        Column("p99", numeric=True),
        Column("Appels/s", numeric=True),
        Column("× local", numeric=True),
    )
    console.print(fill_table(make_table(LATENCY_TITLE, caption=_latency_caption(latency)), columns, [
        (
            protocol_text(result["protocol"]),
            fr_number(result.get("count")),
            Text(fr_number(result.get("errors") or 0), style=DANGER if result.get("errors") else MUTED),
            _milliseconds(result.get("mean_ms")),
            _milliseconds(result.get("median_ms")),
            _milliseconds(result.get("p95_ms")),
            _milliseconds(result.get("p99_ms")),
            fr_number(result.get("rps")),
            fr_number(result.get("overhead_vs_local_x"), 1),
        )
        for result in results
    ], narrow=narrow))

    slowest = max((result.get("mean_ms") or 0.0 for result in results), default=0.0)
    lead = "Temps moyen par appel, à la même échelle"
    if not narrow:
        lead += " ; à droite, la latence au fil de la mesure (échelle propre)"
    chart = []
    for result in results:
        color = protocol_color(result["protocol"])
        row: list[RenderableType] = [
            protocol_text(result["protocol"]),
            bar_text(result.get("mean_ms"), slowest, color, _NARROW_BAR if narrow else _WIDE_BAR),
            Text(fr_duration(result.get("mean_ms")), style="bold"),
        ]
        if not narrow:
            row.append(Text(sparkline(result.get("samples_ms") or [], 28), style=color))
        chart.append(row)
    console.print()
    console.print(Text(lead, style=MUTED))
    console.print(_chart(chart))
    for result in results:
        failure = result.get("error")
        if failure:
            state = "mesure interrompue" if result.get("aborted") else f"{fr_number(result.get('errors'))} échecs"
            note(
                console,
                f"{PROTOCOL_LABELS.get(result['protocol'], result['protocol'])} : {state} — "
                f"{failure.get('code', '')} {failure.get('message', '')}",
                mark=MARK_WARN, color=WARNING,
            )


def _serialization_section(console: Console, serialization: Mapping[str, Any]) -> None:
    rows = serialization.get("rows") or []
    if not rows:
        return
    cells: list[Sequence[RenderableType] | None] = []
    for row, opens_group in _grouped(rows, "message"):
        if opens_group and cells:
            cells.append(None)
        cells.append((
            Text(row["message"], style="bold") if opens_group else "",
            Text(_FORMATS.get(row["format"], row["format"]), style=_FORMAT_COLORS.get(row["format"], SUBTLE)),
            fr_number(row.get("size_bytes")),
            _microseconds(row.get("encode_ns")),
            _microseconds(row.get("decode_ns")),
            _microseconds(row.get("encode_from_dict_ns")),
            _microseconds(row.get("decode_to_dict_ns")),
        ))
    columns = (
        Column("Message"),
        Column("Format"),
        Column("Octets", numeric=True),
        Column("Encodage", numeric=True),
        Column("Décodage", numeric=True),
        Column("Dict → octets", numeric=True, wide_only=True),
        Column("Octets → dict", numeric=True, wide_only=True),
    )
    table = make_table(
        "Sérialisation : JSON vs Protobuf (µs par opération)",
        caption="Encodage, Décodage : le codec seul. Dict → octets, Octets → dict : en partant du dictionnaire "
                "métier, conversion dictionnaire ⇄ message Protobuf comprise — ce que paie réellement ce laboratoire.",
    )
    console.print(fill_table(table, columns, cells, narrow=is_narrow(console)))


def _network_section(console: Console, network: Mapping[str, Any]) -> None:
    points = network.get("points") or []
    if not points:
        return
    narrow = is_narrow(console)
    protocols = list(network.get("protocols") or points[0]["results"])
    columns = (
        Column("Latence" if narrow else "Latence ajoutée", numeric=True, style="bold"),
        *(
            Column(PROTOCOL_LABELS.get(protocol, protocol), numeric=True, style=protocol_color(protocol))
            for protocol in protocols
        ),
        Column("Distant / local", numeric=True, wide_only=True),
    )
    cells = []
    for point in points:
        results = point["results"]
        remote = [value for protocol, value in results.items() if protocol != "local" and value]
        local = results.get("local")
        ratio = sum(remote) / len(remote) / local if remote and local else None
        cells.append((
            f"{fr_compact(point['latency_ms'])} ms",
            *(_milliseconds(results.get(protocol)) for protocol in protocols),
            MISSING if ratio is None else f"× {fr_number(ratio)}",
        ))
    table = make_table(
        "Local vs distant : temps moyen par appel (ms) selon la latence du réseau",
        caption=f"{network.get('method', '')} · {fr_number(network.get('iterations'))} appels par point · les "
                "protocoles distants traversent le proxy de chaos, l'appel local n'emprunte aucun réseau.",
    )
    console.print(fill_table(table, columns, cells, narrow=narrow))

    farthest = max(points, key=lambda point: point["latency_ms"])
    slowest = max((value or 0.0 for value in farthest["results"].values()), default=0.0)
    console.print()
    console.print(Text(
        f"La même boucle avec {fr_compact(farthest['latency_ms'])} ms de latence : le réseau écrase tout le reste",
        style=MUTED,
    ))
    console.print(_chart([
        (
            protocol_text(protocol),
            bar_text(farthest["results"].get(protocol), slowest, protocol_color(protocol),
                     _NARROW_BAR if narrow else _WIDE_BAR),
            Text(fr_duration(farthest["results"].get(protocol)), style="bold"),
        )
        for protocol in protocols
    ]))
    failed = [
        f"{PROTOCOL_LABELS.get(protocol, protocol)} à {fr_compact(point['latency_ms'])} ms ({fr_number(count)})"
        for point in points for protocol, count in (point.get("errors") or {}).items() if count
    ]
    if failed:
        note(console, f"Appels en échec pendant le balayage : {', '.join(failed)}.", mark=MARK_WARN, color=WARNING)


def _highlights_panel(highlights: Sequence[Mapping[str, str]]) -> Panel:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1)
    for index, item in enumerate(highlights):
        color = TONE_COLORS.get(item.get("tone", "info"), SUBTLE)
        heading = Text.assemble((item["title"], "bold"), ("  ", ""), (item["value"], f"bold {color}"))
        body: list[RenderableType] = [heading, Text(item["detail"], style=SUBTLE)]
        if index < len(highlights) - 1:
            body.append(Text())
        grid.add_row(Text(_TONE_MARKS.get(item.get("tone", "info"), MARK_INFO), style=f"bold {color}"), Group(*body))
    return Panel(
        grid,
        title=Text("À retenir — calculé à partir de ces mesures", style=f"bold {ACCENT}"),
        title_align="left",
        box=box.ROUNDED,
        border_style=ACCENT,
        padding=(1, 2),
    )


# --- Rapport complet ---------------------------------------------------------

def render_report(console: Console, report: Mapping[str, Any]) -> None:
    """Affiche le rapport du banc d'essai, section par section ; une suite non lancée est passée."""
    environment = report.get("environment") or {}
    if environment:
        console.print(Text(
            f"  Python {environment.get('python', '?')} · {environment.get('platform', '?')} · "
            f"{environment.get('cpu_count', '?')} cœurs logiques · grpcio {environment.get('grpcio', '?')} · "
            f"protobuf {environment.get('protobuf', '?')}",
            style=MUTED,
        ))
    number = 0
    payload = report.get("payload")
    if payload:
        number += 1
        section(console, "Ce que pèse un appel", "Les mêmes données, sérialisées par chaque middleware.",
                number=number)
        _payload_section(console, payload)
        _wire_section(console, payload)
        _scaling_section(console, payload)
    latency = report.get("latency")
    if latency:
        number += 1
        section(console, "Ce que coûte un appel", "La même boucle d'appels, protocole après protocole.",
                number=number)
        _latency_section(console, latency)
    serialization = report.get("serialization")
    if serialization:
        number += 1
        section(console, "Sérialisation", "Encoder puis décoder les mêmes données, hors de tout réseau.",
                number=number)
        _serialization_section(console, serialization)
    network = report.get("network")
    if network:
        number += 1
        section(console, "Local vs distant", "La même boucle encore, sur un réseau de plus en plus lent.",
                number=number)
        _network_section(console, network)
    highlights = report.get("highlights") or []
    if highlights:
        console.print()
        console.print(_highlights_panel(highlights))


def save_report_files(report: Mapping[str, Any]) -> tuple[Path, Path]:
    """Enregistre le rapport en JSON et, à côté, sa version Markdown ; renvoie les deux chemins."""
    json_path = save_report(report)
    markdown_path = json_path.with_suffix(".md")
    markdown_path.write_text(to_markdown(report), encoding="utf-8", newline="\n")
    return json_path, markdown_path
