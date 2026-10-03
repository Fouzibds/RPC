"""Rapports du banc d'essai : enregistrement JSON, relecture, exports Markdown et CSV.

Un rapport est le dictionnaire renvoyé par ``benchmark_perf.run_full_benchmark``.
Ce module ne mesure rien : il range les rapports dans ``reports/`` et les met en
forme. Les mêmes tableaux servent au terminal (``to_text``) et au Markdown
(``to_markdown``) ; le CSV est « long » — une mesure par ligne — pour se prêter
aux tableaux croisés d'un tableur.

Toutes les sections d'un rapport sont facultatives (``None`` si la suite n'a pas
été lancée) et une valeur non mesurée vaut ``None`` : elle s'affiche « — ».
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from tabulate import tabulate

from common import config
from common.config import APP_NAME, APP_TAGLINE, PROTOCOL_LABELS

DEFAULT_KIND = "benchmark"
MISSING = "—"

_NBSP = "\u00a0"    # espace insécable : séparateur de milliers, et avant « % » ou une unité
_MINUS = "\u2212"   # vrai signe moins, de la largeur d'un chiffre
_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_REPORT_ID = re.compile(r"(?P<kind>[a-z][a-z0-9_]*)-(?P<stamp>\d{8}-\d{6})")
_SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.json")
_DIRECTIONS = {"request": "requête", "response": "réponse"}
_FORMATS = {"json": "JSON", "protobuf": "Protobuf"}
_CSV_HEADER = ("section", "subject", "series", "metric", "value", "unit")
_SIZE_NOTE = (
    "JSON, Protobuf : message sérialisé seul. JSON-RPC, gRPC : le même message précédé de son préfixe de "
    "tramage (4 et 5 octets). REST : message HTTP complet, ligne de départ et en-têtes compris."
)


# --- Nombres à la française --------------------------------------------------

def fr_number(value: float | None, decimals: int = 0, *, signed: bool = False) -> str:
    """``1234.5`` → « 1 234,5 » ; ``None`` → « — ». ``signed`` affiche aussi le « + »."""
    if value is None:
        return MISSING
    rounded = round(float(value), decimals)
    text = f"{abs(rounded):,.{decimals}f}".replace(",", _NBSP).replace(".", ",")
    if rounded < 0:
        return _MINUS + text
    return "+" + text if signed and rounded > 0 else text


def fr_compact(value: float | None) -> str:
    """Nombre sans décimale s'il est entier, avec une seule sinon : « 50 », « 12,5 »."""
    return MISSING if value is None else fr_number(value, 0 if float(value).is_integer() else 1)


def fr_percent(value: float | None, decimals: int = 1, *, signed: bool = False) -> str:
    """``-49.9`` → « −49,9 % »."""
    return MISSING if value is None else f"{fr_number(value, decimals, signed=signed)}{_NBSP}%"


def fr_ratio(value: float | None) -> str:
    """Facteur multiplicatif : « × 9,3 », sans décimale à partir de 100."""
    return MISSING if value is None else f"×{_NBSP}{fr_number(value, 0 if abs(value) >= 100 else 1)}"


def fr_duration(milliseconds: float | None) -> str:
    """Durée dans l'unité qui la rend lisible : ns, µs, ms ou s."""
    if milliseconds is None:
        return MISSING
    if milliseconds >= 1000:
        return f"{fr_number(milliseconds / 1000, 2)}{_NBSP}s"
    if milliseconds >= 1:
        return f"{fr_number(milliseconds, 1 if milliseconds >= 100 else 2)}{_NBSP}ms"
    microseconds = milliseconds * 1000
    if microseconds >= 1:
        return f"{fr_number(microseconds, 0 if microseconds >= 100 else 1)}{_NBSP}µs"
    return f"{fr_number(microseconds * 1000)}{_NBSP}ns"


def fr_bytes(count: float | None) -> str:
    """Taille en octets, ko ou Mo (multiples de 1000, comme sur une fiche réseau)."""
    if count is None:
        return MISSING
    if count >= 1_000_000:
        return f"{fr_number(count / 1_000_000, 2)}{_NBSP}Mo"
    if count >= 10_000:
        return f"{fr_number(count / 1000, 1)}{_NBSP}ko"
    return f"{fr_compact(count)}{_NBSP}{'octet' if count < 2 else 'octets'}"


# --- Enregistrement et relecture ---------------------------------------------

def save_report(report: Mapping[str, Any], directory: Path | None = None) -> Path:
    """Écrit le rapport en JSON dans le dossier des rapports et renvoie son chemin.

    Le fichier porte l'identifiant du rapport (``benchmark-AAAAmmjj-HHMMSS.json``) :
    enregistrer deux fois le même rapport réécrit le même fichier. Un rapport sans
    identifiant exploitable est daté de l'instant de l'enregistrement.
    """
    folder = _folder(directory)
    folder.mkdir(parents=True, exist_ok=True)
    identifier = str(report.get("id", ""))
    if not _REPORT_ID.fullmatch(identifier):
        identifier = f"{DEFAULT_KIND}-{datetime.now().strftime(_STAMP_FORMAT)}"
    path = folder / f"{identifier}.json"
    # allow_nan=False : un NaN ferait un fichier que les navigateurs refusent de lire.
    document = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    # Brouillon puis renommage : un lecteur concurrent ne voit jamais un rapport à moitié écrit.
    handle, draft = tempfile.mkstemp(dir=folder, prefix=f"{identifier}-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
        os.replace(draft, path)
    except BaseException:
        Path(draft).unlink(missing_ok=True)
        raise
    return path


def list_reports(directory: Path | None = None) -> list[dict[str, Any]]:
    """Rapports enregistrés, du plus récent au plus ancien : ``[{name, created_at, size, kind}]``.

    ``kind`` et ``created_at`` se lisent dans le nom du fichier, sans l'ouvrir ; un
    fichier nommé autrement est daté de sa dernière modification, de type ``unknown``.
    """
    folder = _folder(directory)
    if not folder.is_dir():
        return []
    entries: list[tuple[datetime, dict[str, Any]]] = []
    for path in folder.glob("*.json"):
        if not path.is_file():
            continue
        details = path.stat()
        match = _REPORT_ID.fullmatch(path.stem)
        created = _parse_stamp(match["stamp"]) if match else None
        if created is None:
            created = datetime.fromtimestamp(details.st_mtime)
        created = created.astimezone()
        entries.append((created, {
            "name": path.name,
            "created_at": created.isoformat(timespec="seconds"),
            "size": details.st_size,
            "kind": match["kind"] if match else "unknown",
        }))
    entries.sort(key=lambda entry: (entry[0], entry[1]["name"]), reverse=True)
    return [entry for _, entry in entries]


def load_report(name: str, directory: Path | None = None) -> dict[str, Any]:
    """Relit un rapport d'après son nom de fichier (l'extension ``.json`` est facultative).

    ``name`` vient souvent d'une URL : seul un nom de fichier simple, situé dans le
    dossier des rapports, est accepté. Tout chemin (``../``, ``C:\\…``) lève
    ``ValueError`` ; un rapport absent lève ``FileNotFoundError``.
    """
    path = _report_path(name, directory)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"Rapport introuvable : {path.name}") from None
    except ValueError as exc:
        raise ValueError(f"Rapport illisible : {path.name} n'est pas un document JSON valide") from exc
    if not isinstance(document, dict):
        raise ValueError(f"Rapport illisible : {path.name} ne contient pas un objet JSON")
    return document


def _folder(directory: Path | None) -> Path:
    # Lu à chaque appel : les tests redirigent ``common.config.REPORTS_DIR`` vers un dossier temporaire.
    return Path(config.REPORTS_DIR if directory is None else directory)


def _parse_stamp(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, _STAMP_FORMAT)
    except ValueError:   # « 20261340-250000 » a la forme d'une date sans en être une
        return None


def _report_path(name: str, directory: Path | None) -> Path:
    file_name = name if isinstance(name, str) and name.endswith(".json") else f"{name}.json"
    # Lettres, chiffres, « . », « _ » et « - » seulement, longueur bornée : ni séparateur de chemin,
    # ni lettre de lecteur, ni « .. » en tête.
    if not isinstance(name, str) or not _SAFE_FILE_NAME.fullmatch(file_name):
        raise ValueError(
            f"Nom de rapport invalide : {str(name)[:80]!r} (attendu : un nom de fichier simple, sans chemin)"
        )
    folder = _folder(directory).resolve()
    path = (folder / file_name).resolve()
    # Dernier rempart : un lien symbolique ou un nom de périphérique qui, une fois résolu, sortirait du dossier.
    if path.parent != folder:
        raise ValueError(f"Nom de rapport invalide : {name!r} ne désigne pas un fichier du dossier des rapports")
    return path


# --- Tableaux ----------------------------------------------------------------

@dataclass(frozen=True)
class ReportTable:
    """Un tableau du rapport, cellules déjà mises en forme (nombres à la française)."""

    key: str                           # identifiant stable : payload, wire, scaling, serialization, latency…
    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    label_columns: int = 1             # colonnes de libellés, alignées à gauche ; les suivantes sont des nombres
    note: str = ""                     # légende : ce que mesure chaque colonne, quand l'en-tête ne suffit pas

    def render(self, tablefmt: str) -> str:
        align = ("left",) * self.label_columns + ("right",) * (len(self.headers) - self.label_columns)
        # disable_numparse : tabulate ne doit pas reformater « 1 234,5 » à sa façon.
        return tabulate(self.rows, self.headers, tablefmt=tablefmt, colalign=align, disable_numparse=True)


def report_tables(report: Mapping[str, Any]) -> list[ReportTable]:
    """Les tableaux du rapport, dans l'ordre de lecture ; une section absente n'en produit aucun."""
    builders = (
        _payload_table, _wire_table, _scaling_table, _serialization_table,
        _latency_table, _percentiles_table, _network_table,
    )
    return [table for build in builders if (table := build(report)) is not None]


def _payload_table(report: Mapping[str, Any]) -> ReportTable | None:
    rows = (report.get("payload") or {}).get("rows")
    if not rows:
        return None
    return ReportTable(
        "payload",
        "Taille des messages (octets) — JSON vs Protobuf",
        ("Procédure", "Sens", "JSON", "JSON-RPC", "Protobuf", "gRPC", "REST", "Protobuf vs JSON"),
        tuple((
            row["method"],
            _DIRECTIONS.get(row["direction"], row["direction"]),
            fr_number(row.get("json_bytes")),
            fr_number(row.get("json_rpc_bytes")),
            fr_number(row.get("protobuf_bytes")),
            fr_number(row.get("grpc_bytes")),
            fr_number(row.get("rest_bytes")),
            fr_percent(row.get("protobuf_vs_json_pct"), signed=True),
        ) for row in rows),
        label_columns=2,
        note=_SIZE_NOTE,
    )


def _wire_table(report: Mapping[str, Any]) -> ReportTable | None:
    rows = (report.get("payload") or {}).get("wire")
    if not rows:
        return None
    return ReportTable(
        "wire",
        f"Octets réels sur TCP par appel{_named(rows[0].get('method'))} (compteurs des proxys)",
        ("Protocole", "Appels", "Client → serveur", "Serveur → client", "Total"),
        tuple((
            row.get("label") or _label(row["protocol"]),
            fr_number(row.get("calls")),
            fr_number(row.get("bytes_up_per_call"), 1),
            fr_number(row.get("bytes_down_per_call"), 1),
            fr_number(row.get("total_per_call"), 1),
        ) for row in rows),
    )


def _scaling_table(report: Mapping[str, Any]) -> ReportTable | None:
    rows = (report.get("payload") or {}).get("scaling")
    if not rows:
        return None
    return ReportTable(
        "scaling",
        "Montée en charge de list_products — taille de la réponse (octets)",
        ("Produits", "JSON", "JSON-RPC", "Protobuf", "gRPC", "REST", "Protobuf vs JSON"),
        tuple((
            fr_number(row["items"]),
            fr_number(row.get("json_bytes")),
            fr_number(row.get("json_rpc_bytes")),
            fr_number(row.get("protobuf_bytes")),
            fr_number(row.get("grpc_bytes")),
            fr_number(row.get("rest_bytes")),
            fr_percent(row.get("protobuf_vs_json_pct"), signed=True),
        ) for row in rows),
        label_columns=0,
        note=_SIZE_NOTE,
    )


def _serialization_table(report: Mapping[str, Any]) -> ReportTable | None:
    rows = (report.get("serialization") or {}).get("rows")
    if not rows:
        return None
    return ReportTable(
        "serialization",
        "Sérialisation — JSON vs Protobuf (µs par opération)",
        ("Message", "Format", "Octets", "Encodage", "Décodage", "Dict → octets", "Octets → dict"),
        tuple((
            row["message"],
            _FORMATS.get(row["format"], row["format"]),
            fr_number(row.get("size_bytes")),
            _microseconds(row.get("encode_ns")),
            _microseconds(row.get("decode_ns")),
            _microseconds(row.get("encode_from_dict_ns")),
            _microseconds(row.get("decode_to_dict_ns")),
        ) for row in rows),
        label_columns=2,
        note="Encodage, Décodage : le codec seul (message ⇄ octets pour Protobuf). Dict → octets, Octets → dict : "
             "en partant du dictionnaire métier, conversion dictionnaire ⇄ message comprise.",
    )


def _latency_table(report: Mapping[str, Any]) -> ReportTable | None:
    latency = report.get("latency") or {}
    results = latency.get("results")
    if not results:
        return None
    return ReportTable(
        "latency",
        f"Temps moyen par appel (ms) — {_latency_context(latency)}",
        ("Protocole", "Appels", "Erreurs", "Moyenne (ms)", "Médiane (ms)", "Appels/s", "× appel local"),
        tuple((
            result.get("label") or _label(result["protocol"]),
            fr_number(result.get("count")),
            fr_number(result.get("errors")),
            fr_number(result.get("mean_ms"), 4),
            fr_number(result.get("median_ms"), 4),
            fr_number(result.get("rps")),
            fr_number(result.get("overhead_vs_local_x"), 1),
        ) for result in results),
    )


def _percentiles_table(report: Mapping[str, Any]) -> ReportTable | None:
    results = (report.get("latency") or {}).get("results")
    if not results:
        return None
    keys = ("min_ms", "median_ms", "p90_ms", "p95_ms", "p99_ms", "max_ms", "stdev_ms")
    return ReportTable(
        "percentiles",
        "Distribution des latences (ms)",
        ("Protocole", "Min", "p50", "p90", "p95", "p99", "Max", "Écart-type"),
        tuple((
            result.get("label") or _label(result["protocol"]),
            *(fr_number(result.get(key), 4) for key in keys),
        ) for result in results),
    )


def _network_table(report: Mapping[str, Any]) -> ReportTable | None:
    network = report.get("network") or {}
    points = network.get("points")
    if not points:
        return None
    protocols = list(network.get("protocols") or points[0]["results"])
    return ReportTable(
        "network",
        f"Local vs distant — temps moyen par appel{_named(network.get('method'))} selon la latence ajoutée (ms)",
        ("Latence ajoutée (ms)", *(_label(protocol) for protocol in protocols)),
        tuple((
            fr_compact(point["latency_ms"]),
            *(fr_number(point["results"].get(protocol), 4) for protocol in protocols),
        ) for point in points),
        label_columns=0,
    )


def _label(protocol: str) -> str:
    return PROTOCOL_LABELS.get(protocol, protocol)


def _named(method: str | None) -> str:
    """Nom de la procédure à insérer dans un titre ; rien si le rapport ne dit pas laquelle a été mesurée."""
    return f" {method}" if method else ""


def _microseconds(nanoseconds: float | None) -> str:
    return fr_number(None if nanoseconds is None else nanoseconds / 1000, 2)


def _latency_context(latency: Mapping[str, Any]) -> str:
    """Conditions de la mesure de latence, en une ligne."""
    clients = latency.get("concurrency", 1)
    return (
        f"{latency.get('method', '')}, {fr_number(latency.get('iterations'))} appels par protocole, "
        f"{fr_number(clients)} client{'s' if clients and clients > 1 else ''}, "
        f"{'à travers le proxy de chaos' if latency.get('via_proxy') else 'accès direct'}"
    )


# --- Exports -----------------------------------------------------------------

def to_text(report: Mapping[str, Any], tablefmt: str = "rounded_outline") -> str:
    """Rapport pour le terminal : tableaux encadrés puis faits marquants."""
    blocks = [
        f"{table.title}\n{table.render(tablefmt)}" + (f"\n{table.note}" if table.note else "")
        for table in report_tables(report)
    ]
    highlights = report.get("highlights") or []
    if highlights:
        lines = ["À retenir"]
        lines += [f"  • {item['title']} : {item['value']}\n    {item['detail']}" for item in highlights]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def to_markdown(report: Mapping[str, Any]) -> str:
    """Rapport complet en Markdown (tableaux au format « github » de tabulate)."""
    lines = [f"# {APP_NAME} · {APP_TAGLINE} — rapport du banc d'essai", ""]
    lines += [f"- **{name}** : {value}" for name, value in _summary(report)]
    for table in report_tables(report):
        lines += ["", f"## {table.title}", "", table.render("github")]
        if table.note:
            lines += ["", f"_{table.note}_"]
    highlights = report.get("highlights") or []
    if highlights:
        lines += ["", "## À retenir", ""]
        lines += [f"- **{item['title']} : {item['value']}** — {item['detail']}" for item in highlights]
    return "\n".join(lines) + "\n"


def to_csv(report: Mapping[str, Any]) -> str:
    """Toutes les mesures au format long : ``section, subject, series, metric, value, unit``.

    Une ligne par mesure, nombres bruts (point décimal) et en-têtes ASCII : le
    fichier se filtre et se croise tel quel dans un tableur ou avec pandas.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_HEADER)
    for section, subject, series, metric, value, unit in _measures(report):
        writer.writerow((section, subject, series, metric, "" if value is None else value, unit))
    return buffer.getvalue()


def _summary(report: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """Lignes d'en-tête du rapport Markdown : identité, machine, conditions de mesure."""
    if report.get("id"):
        yield "Identifiant", str(report["id"])
    if report.get("created_at"):
        yield "Date", str(report["created_at"])
    environment = report.get("environment") or {}
    if environment:
        yield "Environnement", (
            f"Python {environment.get('python', '?')} · {environment.get('platform', '?')} · "
            f"{environment.get('processor') or 'processeur inconnu'} "
            f"({environment.get('cpu_count', '?')} cœurs logiques) · "
            f"grpcio {environment.get('grpcio', '?')} · protobuf {environment.get('protobuf', '?')}"
        )
    latency = report.get("latency")
    if latency:
        yield "Mesure de latence", (
            f"{_latency_context(latency)}, après {fr_number(latency.get('warmup'))} appels d'échauffement, "
            "bus de traces coupé"
        )


def _measures(report: Mapping[str, Any]) -> Iterator[tuple[str, str, str, str, Any, str]]:
    payload = report.get("payload") or {}
    for row in payload.get("rows") or ():
        subject = f"{row['method']}/{row['direction']}"
        for series in ("json", "json_rpc", "protobuf", "grpc", "rest", "rest_body"):
            yield "payload", subject, series, "bytes", row.get(f"{series}_bytes"), "B"
        yield "payload", subject, "protobuf", "protobuf_vs_json_pct", row.get("protobuf_vs_json_pct"), "%"
    for row in payload.get("wire") or ():
        subject = row.get("method", "")
        yield "wire", subject, row["protocol"], "calls", row.get("calls"), "calls"
        for metric in ("bytes_up_per_call", "bytes_down_per_call", "total_per_call"):
            yield "wire", subject, row["protocol"], metric, row.get(metric), "B"
    for row in payload.get("scaling") or ():
        subject = f"list_products/{row['items']}"
        for series in ("json", "json_rpc", "protobuf", "grpc", "rest"):
            yield "scaling", subject, series, "bytes", row.get(f"{series}_bytes"), "B"
        yield "scaling", subject, "protobuf", "protobuf_vs_json_pct", row.get("protobuf_vs_json_pct"), "%"
    for row in (report.get("serialization") or {}).get("rows") or ():
        subject = f"{row.get('message_type', row['message'])}[{row.get('items', 1)}]"
        yield "serialization", subject, row["format"], "size_bytes", row.get("size_bytes"), "B"
        for metric in ("encode_ns", "decode_ns", "encode_from_dict_ns", "decode_to_dict_ns"):
            yield "serialization", subject, row["format"], metric, row.get(metric), "ns"
    latency = report.get("latency") or {}
    units = {"count": "calls", "errors": "calls", "total_s": "s", "rps": "calls/s", "overhead_vs_local_x": "x"}
    metrics = ("count", "errors", "mean_ms", "median_ms", "p90_ms", "p95_ms", "p99_ms", "min_ms", "max_ms",
               "stdev_ms", "total_s", "rps", "overhead_vs_local_x")
    for result in latency.get("results") or ():
        for metric in metrics:
            yield ("latency", latency.get("method", ""), result["protocol"], metric, result.get(metric),
                   units.get(metric, "ms"))
    network = report.get("network") or {}
    for point in network.get("points") or ():
        subject = f"{network.get('method', '')}@{point['latency_ms']:g}ms"
        for protocol, mean_ms in point["results"].items():
            yield "network", subject, protocol, "mean_ms", mean_ms, "ms"
