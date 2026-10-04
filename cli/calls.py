"""Un appel RPC de bout en bout : arguments saisis → paramètres → appel → affichage.

Le même code sert à ``--call``, au menu interactif et à la visite guidée. Un
appel passe toujours par ``InventoryClient.invoke`` : c'est l'interface commune
aux quatre protocoles, donc l'appel affiché est exactement celui que font le
banc d'essai et le dashboard.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Sequence

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from benchmark_lab.report import fr_bytes, fr_duration, fr_number, fr_ratio
from common.catalog import METHODS, MethodSpec, ParamSpec, method_spec
from common.client_api import InventoryClient
from common.config import PROTOCOL_LABELS
from common.errors import RpcError
from common.telemetry import PIPELINE, CallTrace

from .theme import (
    DANGER,
    MARK_OK,
    MUTED,
    SUBTLE,
    SUCCESS,
    WARNING,
    Column,
    bar_text,
    compact_json,
    error_panel,
    fill_table,
    is_narrow,
    json_block,
    make_table,
    note,
    protocol_color,
)
from .wire import wire_report

if TYPE_CHECKING:
    from lab import LabRuntime

STREAM_KINDS: frozenset[str] = frozenset({"server_stream", "bidi_stream"})
_TEXT_TYPES: frozenset[str] = frozenset({"str", "product_id", "category"})
_TRACE_PATIENCE_S = 0.2     # les derniers évènements du serveur peuvent suivre de peu le retour du client
_TRACE_POLL_S = 0.005

ItemCallback = Callable[[int, float, Any], None]


# --- Arguments de la ligne de commande → paramètres --------------------------

def parse_value(text: str) -> Any:
    """Valeur saisie : du JSON si c'en est (``-3``, ``true``, ``[1, 2]``), sinon la chaîne telle quelle."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def convert_argument(spec: ParamSpec, text: str) -> Any:
    """Convertit un argument saisi selon le type que le catalogue déclare pour ce paramètre.

    Un paramètre textuel garde sa saisie brute — la référence « 1001 » reste une
    chaîne — sauf si elle est écrite comme une chaîne JSON (``"SKU-1001"``).
    """
    value = parse_value(text)
    if spec.type in _TEXT_TYPES and not isinstance(value, str):
        return text
    return value


def require_method(name: str) -> MethodSpec:
    spec = method_spec(name)
    if spec is None:
        known = ", ".join(method.name for method in METHODS)
        raise ValueError(f"Procédure inconnue : « {name} » (procédures disponibles : {known})")
    return spec


def build_params(method: str, arguments: Sequence[str]) -> dict[str, Any]:
    """Paramètres nommés d'un appel, à partir des arguments de ``--call``.

    Les arguments sont positionnels, dans l'ordre des paramètres du catalogue ;
    ``nom=valeur`` désigne un paramètre par son nom. Un paramètre omis prend la
    valeur par défaut du catalogue. Lève ``ValueError`` (procédure inconnue,
    argument en trop, paramètre donné deux fois).
    """
    spec = require_method(method)
    by_name = {param.name: param for param in spec.params}
    supplied: dict[str, Any] = {}
    positional = iter(spec.params)
    for argument in arguments:
        name, separator, raw = argument.partition("=")
        if separator and name in by_name:
            param, text = by_name[name], raw
        else:
            param = next((candidate for candidate in positional if candidate.name not in supplied), None)
            text = argument
            if param is None:
                expected = ", ".join(by_name) or "aucun"
                raise ValueError(
                    f"Trop d’arguments pour {method} : « {argument} » est en trop (paramètres attendus : {expected})"
                )
        if param.name in supplied:
            raise ValueError(f"Le paramètre « {param.name} » de {method} est donné deux fois")
        supplied[param.name] = convert_argument(param, text)
    return {param.name: supplied.get(param.name, param.default) for param in spec.params}


def describe_call(method: str, params: dict[str, Any]) -> str:
    """L'appel tel qu'on l'écrirait en Python."""
    arguments = ", ".join(f"{name}={compact_json(value, 48)}" for name, value in params.items())
    return f"{method}({arguments})"


# --- Exécution ---------------------------------------------------------------

@dataclass
class CallOutcome:
    """Issue d'un appel : son résultat ou son erreur, sa durée, et sa trace si le bus était actif."""

    protocol: str
    method: str
    params: dict[str, Any]
    via_proxy: bool = False
    duration_ms: float = 0.0
    result: Any = None
    error: RpcError | None = None
    items: int | None = None            # nombre d'éléments reçus, pour un flux
    call_id: str = ""
    trace: CallTrace | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def request_bytes(self) -> int | None:
        return self.trace.summary()["request_bytes"] if self.trace is not None else None

    @property
    def response_bytes(self) -> int | None:
        """Octets reçus par le client : la réponse, plus chaque élément quand c'est un flux."""
        if self.trace is None:
            return None
        received = next((event for event in self.trace.events if event.stage == "client.receive"), None)
        if received is not None and received.detail.get("messages"):
            return received.size      # gRPC : cet évènement totalise déjà tous les messages du flux
        items = [
            event.size or 0 for event in self.trace.events
            if event.stage == "client.stream_item" and event.detail.get("direction") != "request"
        ]
        if received is None and not items:
            return None
        return (received.size or 0 if received is not None else 0) + sum(items)


def execute_call(
    runtime: "LabRuntime",
    protocol: str,
    method: str,
    params: dict[str, Any],
    *,
    via_proxy: bool = False,
    timeout: float | None = None,
    on_item: ItemCallback | None = None,
    client: InventoryClient | None = None,
) -> CallOutcome:
    """Exécute un appel et rend son issue ; une ``RpcError`` devient ``outcome.error``.

    L'appel part d'un client neuf, fermé ensuite — sauf si ``client`` est fourni
    (connexion déjà ouverte : la durée mesurée ne comprend alors pas la poignée
    de main). Un flux est consommé jusqu'au bout : ``on_item(rang, millisecondes
    écoulées, élément)`` est appelé à l'arrivée de chaque élément.
    """
    outcome = CallOutcome(protocol, method, dict(params), via_proxy)
    owned = client is None
    if client is None:
        client = runtime.client(protocol, via_proxy=via_proxy, timeout=timeout)
    previous_call_id = client.last_call_id
    started = time.perf_counter()
    try:
        result = client.invoke(method, params)
        if isinstance(result, Iterator):
            outcome.items = 0
            for item in result:
                outcome.items += 1
                if on_item is not None:
                    on_item(outcome.items, (time.perf_counter() - started) * 1000, item)
        else:
            outcome.result = result
    except RpcError as error:
        outcome.error = error
    finally:
        outcome.duration_ms = (time.perf_counter() - started) * 1000
        # Bus coupé, le client ne publie rien et garde l'identifiant de son appel précédent.
        if client.last_call_id != previous_call_id:
            outcome.call_id = client.last_call_id
        if owned:
            client.close()
    outcome.trace = _settled_trace(runtime, outcome)
    return outcome


def _settled_trace(runtime: "LabRuntime", outcome: CallOutcome) -> CallTrace | None:
    """Trace de l'appel, une fois les évènements du serveur arrivés.

    Le serveur publie ses dernières étapes depuis un autre fil : après un appel
    unaire réussi, on leur laisse un instant pour rejoindre la trace.
    """
    if not outcome.call_id:
        return None     # bus coupé : le client n'a rien publié
    deadline = time.perf_counter() + _TRACE_PATIENCE_S
    expects_pipeline = outcome.ok and outcome.items is None and outcome.protocol != "local"
    while True:
        trace = runtime.collector.get(outcome.call_id)
        stages = {event.stage for event in trace.events} if trace is not None else set()
        if not expects_pipeline or stages.issuperset(PIPELINE) or time.perf_counter() >= deadline:
            return trace
        time.sleep(_TRACE_POLL_S)


# --- Affichage ---------------------------------------------------------------

def stream_item_line(rank: int, elapsed_ms: float, item: Any, width: int) -> Text:
    """Une ligne par élément de flux, écrite à son arrivée."""
    prefix = f"  ▸ #{rank:<3} +{fr_duration(elapsed_ms):>9}  "
    return Text.assemble((prefix, MUTED), compact_json(item, max(width - len(prefix) - 1, 24)))


def _measures(outcome: CallOutcome) -> str:
    """Durée et tailles de l'appel, en une ligne."""
    remote = outcome.protocol != "local"
    # Chaque appel part d'un client neuf : dès qu'il a émis quelque chose, sa durée comprend l'ouverture
    # de la connexion.
    connected = remote and outcome.request_bytes is not None
    parts = [fr_duration(outcome.duration_ms) + (", connexion comprise" if connected else "")]
    if outcome.items is not None:
        parts.append(f"{fr_number(outcome.items)} élément{'s' if outcome.items > 1 else ''}")
    if outcome.request_bytes is not None:
        parts.append(f"requête {fr_bytes(outcome.request_bytes)}")
    if outcome.response_bytes is not None:
        parts.append(f"réponse {fr_bytes(outcome.response_bytes)}")
    if not remote:
        parts.append("aucun octet échangé")
    elif outcome.via_proxy:
        parts.append("via le proxy de chaos")
    return " · ".join(parts)


def render_outcome(
    console: Console, outcome: CallOutcome, *, inspect: bool = False, max_lines: int | None = None
) -> None:
    """Résultat (JSON coloré) ou erreur (panneau), avec durée et tailles ; ``inspect`` ajoute le fil."""
    color = protocol_color(outcome.protocol)
    label = PROTOCOL_LABELS.get(outcome.protocol, outcome.protocol)
    measures = _measures(outcome)
    if outcome.error is not None:
        console.print(error_panel(outcome.error, f"{label} · {measures}"))
    elif outcome.items is not None:
        note(console, f"Flux terminé · {measures}")
    else:
        console.print(Panel(
            json_block(outcome.result, max_lines),
            title=Text.assemble(
                (f"{MARK_OK} ", f"bold {SUCCESS}"),
                (describe_call(outcome.method, outcome.params), "bold"),
                (f" · {label}", color),
            ),
            title_align="left",
            subtitle=Text(measures, style=MUTED),
            subtitle_align="right",
            box=box.ROUNDED,
            border_style=color,
            padding=(0, 1),
        ))
    if inspect:
        wire_report(console, outcome.trace)


# --- Appels asynchrones ------------------------------------------------------

def run_parallel(
    runtime: "LabRuntime",
    protocol: str,
    method: str,
    params: dict[str, Any],
    count: int,
    *,
    via_proxy: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Les mêmes ``count`` appels, l'un après l'autre puis lancés ensemble (``submit``).

    Renvoie ``{count, sequential_ms, parallel_ms, speedup, sequential_errors,
    parallel_errors}``. Le bus est coupé : on mesure le middleware, pas ses
    traces. Un premier appel, non compté, ouvre la connexion.
    """
    def attempt(call: Callable[[], Any]) -> bool:
        try:
            call()
        except RpcError:
            return False
        return True

    with runtime.client(protocol, via_proxy=via_proxy, timeout=timeout) as client, runtime.bus.muted():
        attempt(lambda: client.invoke(method, params))
        started = time.perf_counter()
        sequential_errors = sum(not attempt(lambda: client.invoke(method, params)) for _ in range(count))
        sequential_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        futures = [client.submit(method, params) for _ in range(count)]
        parallel_errors = sum(not attempt(future.result) for future in futures)
        parallel_ms = (time.perf_counter() - started) * 1000
    return {
        "count": count,
        "sequential_ms": sequential_ms,
        "parallel_ms": parallel_ms,
        "speedup": sequential_ms / parallel_ms if parallel_ms > 0 else None,
        "sequential_errors": sequential_errors,
        "parallel_errors": parallel_errors,
    }


def render_parallel(console: Console, report: dict[str, Any], protocol: str) -> None:
    """Séquentiel contre simultané : durées, barres à l'échelle, et le facteur d'accélération."""
    color = protocol_color(protocol)
    count = report["count"]
    longest = max(report["sequential_ms"], report["parallel_ms"])
    columns = (
        Column("Mode"),
        Column("Durée totale", numeric=True),
        Column("Par appel", numeric=True),
        Column("Erreurs", numeric=True),
        Column("", wide_only=True),
    )
    rows = [
        (
            label,
            fr_duration(duration),
            fr_duration(duration / count),
            Text(fr_number(errors), style=DANGER if errors else MUTED),
            bar_text(duration, longest, style, width=32),
        )
        for label, duration, errors, style in (
            ("Synchrone — un appel après l’autre", report["sequential_ms"], report["sequential_errors"], SUBTLE),
            ("Asynchrone — tous lancés ensemble", report["parallel_ms"], report["parallel_errors"], color),
        )
    ]
    table = make_table(f"{fr_number(count)} appels · {PROTOCOL_LABELS.get(protocol, protocol)}")
    console.print(fill_table(table, columns, rows, narrow=is_narrow(console)))
    speedup = report["speedup"]
    if speedup is None:
        return
    if speedup >= 1.5:
        note(
            console,
            f"Accélération {fr_ratio(speedup)} : les requêtes partent sans attendre les réponses ; "
            "le temps d’attente du réseau se superpose au lieu de s’additionner.",
        )
    else:
        note(
            console,
            f"Accélération {fr_ratio(speedup)} : sans latence réseau, un appel ne fait presque qu’occuper le "
            "processeur — il n’y a pas d’attente à superposer. Ajoutez de la latence (laboratoire de pannes) "
            "pour voir l’écart se creuser.",
            mark="!", color=WARNING,
        )


__all__ = [
    "STREAM_KINDS",
    "CallOutcome",
    "build_params",
    "convert_argument",
    "describe_call",
    "execute_call",
    "parse_value",
    "render_outcome",
    "render_parallel",
    "require_method",
    "run_parallel",
    "stream_item_line",
]
