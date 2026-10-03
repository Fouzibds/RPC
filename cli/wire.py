"""« Sous le capot » : ce qu'un appel a réellement fait, d'après sa trace.

Rien n'est reconstitué ici : la chronologie vient des évènements publiés par le
stub et le squelette (plan §5), les octets sont ceux qui ont été écrits sur la
socket, et le découpage coloré suit les ``segments`` que chaque middleware joint
à ses évènements d'envoi et de réception.

Trois lectures des mêmes octets, de la plus brute à la plus lisible :
vue hexadécimale colorée par segment, légende des segments, puis le message
décodé (JSON indenté, en-têtes HTTP, ou champs Protobuf un par un).
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterator, Mapping, Sequence

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from benchmark_lab.report import MISSING, fr_bytes, fr_number
from common.config import PROTOCOL_LABELS
from common.telemetry import PIPELINE, STAGE_INFO, CallTrace, TraceEvent

from .theme import (
    ACCENT,
    DANGER,
    INFO,
    MUTED,
    ORANGE,
    SUBTLE,
    SUCCESS,
    WARNING,
    Column,
    bar_text,
    compact_json,
    fill_table,
    fr_micros,
    hint,
    is_narrow,
    json_block,
    key_value_grid,
    make_table,
    note,
    protocol_color,
)

HEXDUMP_BYTES = 160           # dix lignes : assez pour voir le tramage et le début d'un long message
FULL_DUMP_BYTES = 320         # jusqu'à vingt lignes, un message est montré en entier
MAX_FIELD_ROWS = 28
MAX_BODY_LINES = 24
_BYTES_PER_LINE = 16
_FIELD_BYTES_PREVIEW = 6
_FIELD_VALUE_PREVIEW = 40     # au-delà, la colonne des valeurs écraserait les autres
_NARROW_VALUE_PREVIEW = 24
_HEX_COLUMN_WIDTH = _BYTES_PER_LINE * 3 + 1   # « xx » + espace par octet, et une gouttière au milieu

# Une couleur par nature de segment ; deux segments voisins de même nature alternent (requête / en-têtes).
_SEGMENT_COLORS: dict[str, tuple[str, ...]] = {
    "frame": (ORANGE, WARNING),
    "header": (ACCENT, INFO),
    "tag": (ACCENT,),
    "len": (WARNING,),
    "value": (SUCCESS,),
    "body": (SUBTLE,),
}
_SIDE_STYLES: dict[str, tuple[str, str]] = {
    "client": ("client", ACCENT),
    "server": ("serveur", ORANGE),
    "network": ("réseau", WARNING),
    "resilience": ("résilience", INFO),
}
_STREAM_STAGES: frozenset[str] = frozenset({"client.stream_item", "server.stream_item"})
# Libellé d'un groupe d'éléments de flux : (étape, sens) — le sens n'est précisé que par gRPC.
_STREAM_LABELS: dict[tuple[str, str], str] = {
    ("client.stream_item", "request"): "Éléments envoyés par le client",
    ("client.stream_item", "response"): "Éléments reçus par le client",
    ("server.stream_item", "request"): "Éléments reçus par le serveur",
    ("server.stream_item", "response"): "Éléments émis par le serveur",
}
_STREAM_TEXT = "Chaque élément du flux voyage dans son propre message, sur la même connexion."
_EXTRA_STAGES: dict[str, tuple[str, str]] = {
    "client.error": ("Erreur remontée", "Stub client"),
    "server.error": ("Erreur côté serveur", "Squelette serveur"),
}


# --- Chronologie -------------------------------------------------------------

def pipeline_table(trace: CallTrace, *, explain: bool = False, narrow: bool = False) -> Table:
    """Les étapes de l'appel dans l'ordre où elles ont eu lieu : libellé, rôle, durée, taille.

    Les étapes du pipeline nominal portent leur numéro (1 à 12) ; les autres
    (erreur, éléments de flux) sont marquées « · ». La vue compacte rapporte
    chaque durée à celle de l'appel entier ; avec ``explain``, la barre laisse
    sa place à la phrase qui dit ce que fait l'étape. ``narrow`` ne garde que
    les colonnes essentielles.
    """
    color = protocol_color(trace.protocol)
    terminal = trace.terminal
    total_us = terminal.duration_us if terminal is not None else None
    columns = (
        Column("#", numeric=True, style=MUTED),
        Column("Étape"),
        Column("Ce qui se passe", style=SUBTLE, no_wrap=False) if explain else Column("Côté"),
        Column("Rôle", style=SUBTLE, wide_only=True),
        Column("Durée", numeric=True),
        Column("Octets", numeric=True),
        Column("Part de l'appel", wide_only=True),
    )
    streamed: Counter[tuple[str, str]] = Counter()
    streamed_bytes: Counter[tuple[str, str]] = Counter()
    for event in trace.events:
        if event.stage in _STREAM_STAGES:
            streamed[_stream_group(event)] += 1
            streamed_bytes[_stream_group(event)] += event.size or 0
    shown: set[tuple[str, str]] = set()
    rows: list[Sequence[RenderableType]] = []
    for event in trace.events:
        stage = event.stage
        share: RenderableType = ""
        if stage in _STREAM_STAGES:
            group = _stream_group(event)
            if group in shown:
                continue      # un flux de 500 éléments tient sur une ligne par côté et par sens
            shown.add(group)
            label, role, text = f"{_STREAM_LABELS[group]} × {streamed[group]}", "Flux", _STREAM_TEXT
            duration, size = MISSING, fr_number(streamed_bytes[group])
        else:
            info = STAGE_INFO.get(stage)
            label, role = (info["label"], info["role"]) if info else _EXTRA_STAGES.get(stage, (stage, ""))
            text = info["text"] if info else str(event.detail.get("message", ""))
            duration = fr_micros(event.duration_us)
            size = MISSING if event.size is None else fr_number(event.size)
            if event is terminal:
                share = Text("durée totale", style=MUTED)
            else:
                share = bar_text(event.duration_us, total_us or 0.0, color, width=16)
        side_label, side_color = _SIDE_STYLES.get(event.side, (event.side, SUBTLE))
        number = str(PIPELINE.index(stage) + 1) if stage in PIPELINE else "·"
        title = Text.assemble((label, f"bold {DANGER}" if stage.endswith(".error") else "bold"))
        if explain:
            # Le côté et le rôle passent sous le libellé : la phrase d'explication a besoin de la largeur.
            title.append(f"\n{side_label} · {role}" if role else f"\n{side_label}", style=side_color)
            rows.append((number, title, text, "", duration, size, ""))
        else:
            rows.append((number, title, Text(side_label, style=side_color), role, duration, size, share))
    # La vue expliquée n'a ni colonne « Rôle » ni barre : elle se remplit comme un tableau étroit.
    return fill_table(make_table(expand=explain), columns, rows, narrow=narrow or explain)


def _stream_group(event: TraceEvent) -> tuple[str, str]:
    direction = event.detail.get("direction")
    return event.stage, direction if direction in ("request", "response") else "response"


# --- Octets ------------------------------------------------------------------

def _byte_styles(length: int, segments: Sequence[Mapping[str, Any]]) -> list[str]:
    """Style de chaque octet affiché, d'après le segment qui le couvre."""
    styles = [""] * length
    seen: Counter[str] = Counter()
    for segment in segments:
        palette = _SEGMENT_COLORS.get(segment.get("kind", ""), (SUBTLE,))
        style = palette[seen[segment.get("kind", "")] % len(palette)]
        seen[segment.get("kind", "")] += 1
        # Un payload tronqué à la capture garde des segments qui débordent : on s'arrête à ce qui est affiché.
        for index in range(max(segment["start"], 0), min(segment["end"], length)):
            styles[index] = style
    return styles


def hexdump(payload: bytes, segments: Sequence[Mapping[str, Any]], *, limit: int = HEXDUMP_BYTES) -> Text:
    """Vue hexadécimale : offset, octets, rendu ASCII — chaque octet porte la couleur de son segment."""
    shown = payload[:limit]
    styles = _byte_styles(len(shown), segments)
    text = Text()
    for offset in range(0, len(shown), _BYTES_PER_LINE):
        chunk = shown[offset:offset + _BYTES_PER_LINE]
        text.append(f"{offset:06x}  ", style=MUTED)
        written = 0
        for index, byte in enumerate(chunk):
            gutter = "  " if index == _BYTES_PER_LINE // 2 - 1 else " "
            text.append(f"{byte:02x}", style=styles[offset + index])
            text.append(gutter)
            written += 2 + len(gutter)
        text.append(" " * (_HEX_COLUMN_WIDTH - written + 1))
        for index, byte in enumerate(chunk):
            text.append(chr(byte) if 32 <= byte < 127 else "·", style=styles[offset + index])
        if offset + _BYTES_PER_LINE < len(shown):
            text.append("\n")
    if len(payload) > limit:
        text.append(f"\n… {fr_bytes(len(payload) - limit)} de plus, non affichés", style=MUTED)
    return text


def segments_legend(segments: Sequence[Mapping[str, Any]]) -> Text:
    """Légende de la vue hexadécimale : un carré de couleur par segment (ou par nature, pour Protobuf)."""
    legend = Text()
    if any(segment.get("kind") == "tag" for segment in segments):
        prefix = sum(segment["end"] - segment["start"] for segment in segments if segment.get("kind") == "frame")
        entries = [
            (ORANGE, f"préfixe gRPC ({prefix} octets : drapeau de compression + longueur)"),
            (ACCENT, "tag (numéro de champ + type de fil)"),
            (WARNING, "longueur"),
            (SUCCESS, "valeur"),
        ]
    else:
        seen: Counter[str] = Counter()
        entries = []
        for segment in segments:
            kind = segment.get("kind", "")
            palette = _SEGMENT_COLORS.get(kind, (SUBTLE,))
            color = palette[seen[kind] % len(palette)]
            seen[kind] += 1
            size = segment["end"] - segment["start"]
            entries.append((color, f"{segment['label']} · {fr_bytes(size)}"))
    for index, (color, label) in enumerate(entries):
        legend.append("\n" if index and index % 2 == 0 else ("   " if index else ""))
        legend.append("■ ", style=color)
        legend.append(label, style=MUTED)
    return legend


def protobuf_fields(payload: bytes, segments: Sequence[Mapping[str, Any]], *, narrow: bool = False) -> Table:
    """Décodage Protobuf champ par champ : les octets, puis ce que le contrat permet d'y lire."""
    columns = (
        Column("Octets", style=MUTED, wide_only=True),
        Column("Champ"),
        Column("Type de fil", style=SUBTLE),
        Column("Type", style=SUBTLE),
        Column("Valeur décodée"),
    )
    preview = _NARROW_VALUE_PREVIEW if narrow else _FIELD_VALUE_PREVIEW
    fields = list(_fields(segments))
    rows: list[Sequence[RenderableType]] = []
    for tag, length, value in fields[:MAX_FIELD_ROWS]:
        end = (value or length or tag)["end"]
        raw = payload[tag["start"]:end]
        raw_hex = raw[:_FIELD_BYTES_PREVIEW].hex(" ") + (" …" if len(raw) > _FIELD_BYTES_PREVIEW else "")
        name = Text("  " * tag.get("depth", 0))
        name.append(f"{tag.get('number', '?')} ", style=f"bold {ACCENT}")
        name.append(str(tag.get("field", "")), style=f"bold {DANGER}" if tag.get("mismatch") else "bold")
        if value is not None:
            decoded = Text(compact_json(value.get("value"), preview), style=SUCCESS)
        elif length is not None:
            decoded = Text(f"sous-message · {fr_bytes(length['value'])}", style=MUTED)
        else:
            decoded = Text(MISSING, style=MUTED)
        wire_type = f"{tag.get('wire_type', '?')} · {tag.get('wire_type_name', '')}"
        rows.append((raw_hex, name, wire_type, str(tag.get("type", MISSING)), decoded))
    if len(fields) > MAX_FIELD_ROWS:
        rows.append(("", Text(f"… {len(fields) - MAX_FIELD_ROWS} champs de plus", style=MUTED), "", "", ""))
    return fill_table(make_table(), columns, rows, narrow=narrow)


def _fields(
    segments: Sequence[Mapping[str, Any]],
) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any] | None, Mapping[str, Any] | None]]:
    """Regroupe les segments Protobuf en champs : ``(tag, longueur éventuelle, valeur éventuelle)``.

    Un sous-message n'a pas de segment « valeur » : ce sont ses propres champs,
    d'un niveau plus profond, qui couvrent ses octets.
    """
    current: list[Mapping[str, Any] | None] | None = None
    for segment in segments:
        kind = segment.get("kind")
        if kind == "tag":
            if current is not None:
                yield current[0], current[1], current[2]
            current = [segment, None, None]
        elif current is not None and kind == "len":
            current[1] = segment
        elif current is not None and kind == "value":
            current[2] = segment
    if current is not None:
        yield current[0], current[1], current[2]


def _decoded_view(
    event: TraceEvent, segments: Sequence[Mapping[str, Any]], narrow: bool
) -> list[RenderableType]:
    """Le message tel qu'un humain le lit : selon le protocole, JSON, HTTP + JSON, ou champs Protobuf."""
    payload = event.payload or b""
    parts: list[RenderableType] = []
    headers = event.detail.get("http2_headers")
    if headers:
        merged = {**headers, **(event.detail.get("http2_trailers") or {})}
        parts.append(Text("En-têtes HTTP/2 (compressés par HPACK sur le fil, hors des octets ci-dessus)", style=MUTED))
        parts.append(key_value_grid((name, Text(str(value))) for name, value in merged.items()))
    if any(segment.get("kind") == "tag" for segment in segments):
        parts.append(protobuf_fields(payload, segments, narrow=narrow))
        return parts
    if not segments:
        # Élément de flux JSON : publié sans découpage, il se lit d'un seul tenant.
        try:
            parts.append(json_block(json.loads(payload), MAX_BODY_LINES))
        except ValueError:
            pass
        return parts
    for segment in segments:
        chunk = payload[segment["start"]:segment["end"]]
        if segment.get("kind") == "header":
            parts.append(Text(chunk.decode("utf-8", "replace").rstrip("\r\n"), style=SUBTLE))
        elif segment.get("kind") == "body" and chunk:
            try:
                parts.append(json_block(json.loads(chunk), MAX_BODY_LINES))
            except ValueError:    # corps tronqué à la capture : il n'est plus un document JSON
                parts.append(Text(chunk[:400].decode("utf-8", "replace") + " …", style=SUBTLE))
    return parts


def message_panel(event: TraceEvent, title: str, color: str, *, narrow: bool = False) -> Panel:
    """Un message tel qu'il a circulé : octets colorés, légende, puis sa lecture décodée."""
    payload = event.payload or b""
    segments = event.detail.get("segments") or []
    body: list[RenderableType] = []
    if payload:
        limit = len(payload) if len(payload) <= FULL_DUMP_BYTES else HEXDUMP_BYTES
        body.append(hexdump(payload, segments, limit=limit))
        if segments:
            body += [Text(), segments_legend(segments)]
        decoded = _decoded_view(event, segments, narrow)
        if decoded:
            body.append(Text())
            body += decoded
    else:
        # Flux gRPC : l'évènement agrège tous les messages, sans octets (ils sont dans les éléments du flux).
        messages = event.detail.get("messages")
        body.append(Text(
            f"{messages} messages, {fr_bytes(event.size)} au total — le détail est porté par chaque élément du flux."
            if messages else "Aucun octet capturé pour cette étape.",
            style=MUTED,
        ))
    return Panel(
        Group(*body),
        title=Text(f"{title} · {fr_bytes(event.size)}", style=f"bold {color}"),
        title_align="left",
        box=box.ROUNDED,
        border_style=color,
        padding=(0, 1),
    )


# --- Vue d'ensemble d'un appel -----------------------------------------------

def wire_report(console: Console, trace: CallTrace | None, *, explain: bool = False) -> None:
    """Chronologie puis messages bruts d'un appel (``--inspect``, mode « Sous le capot », visite guidée)."""
    if trace is None or not trace.events:
        note(
            console,
            "Aucune trace pour cet appel : le bus de télémétrie était coupé.",
            mark="!", color=WARNING,
        )
        return
    color = protocol_color(trace.protocol)
    narrow = is_narrow(console)
    stages = {event.stage: event for event in reversed(trace.events)}   # première occurrence de chaque étape
    console.print(pipeline_table(trace, explain=explain, narrow=narrow))
    if trace.protocol == "local":
        hint(console, "Appel local : ni sérialisation ni réseau, donc aucun octet à montrer — c'est le point zéro "
                      "auquel se comparent les trois middlewares.")
        return
    request, response = stages.get("client.send"), stages.get("client.receive")
    label = PROTOCOL_LABELS.get(trace.protocol, trace.protocol)
    if request is None:
        hint(console, "Rien n'a été envoyé : l'appel a échoué avant d'atteindre le réseau.")
        return
    console.print(message_panel(request, f"Requête — {label}", color, narrow=narrow))
    item = stages.get("client.stream_item")
    if item is not None:
        console.print(message_panel(item, "Premier élément du flux", color, narrow=narrow))
    if response is not None:
        console.print(message_panel(response, f"Réponse — {label}", color, narrow=narrow))
    else:
        hint(console, "Aucune réponse n'est parvenue au client : c'est tout ce qu'il sait de cet appel.")
