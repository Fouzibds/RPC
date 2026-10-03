"""Transparence de localisation : la même opération métier, écrite de quatre façons.

    python -m benchmark_lab.transparency_demo

L'opération : sortir trois unités du stock d'un produit et obtenir le nouveau
stock. Les quatre fonctions ci-dessous la réalisent RÉELLEMENT — ``run_comparison``
et les tests les exécutent — et sont écrites pour être lues côte à côte, chacune
aussi courte qu'elle peut honnêtement l'être :

* ``update_stock_local``         un appel de méthode sur un objet en mémoire ;
* ``update_stock_custom_rpc``    la MÊME ligne, sur le stub du RPC maison ;
* ``update_stock_grpc``          le stub généré par protoc et ses messages typés ;
* ``update_stock_rest_by_hand``  HTTP + JSON sans stub : tout revient à l'appelant.

C'est le premier avantage du RPC — l'appel distant a la forme d'un appel local —
et, en creux, son piège : rien dans les trois premières écritures ne dit
laquelle traverse un réseau, coûte mille fois plus cher et peut échouer.

``code_comparison`` rend ces sources affichables (code, lignes utiles, ce que
l'appelant doit gérer lui-même) ; ``run_comparison`` les exécute sur un
laboratoire et vérifie qu'elles sont équivalentes.
"""
from __future__ import annotations

import ast
import http.client
import inspect
import json
import shutil
import statistics
import sys
import textwrap
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import grpc
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from common.config import APP_NAME, DEFAULT_TIMEOUT_S
from common.errors import (
    GRPC_TO_CANONICAL,
    INTERNAL,
    DomainError,
    RpcError,
    RpcTimeoutError,
    RpcTransportError,
    error_from_code,
)
from common.inventory import InventoryService
from common.telemetry import EventBus
from lab import LabRuntime
from rpc_custom import RpcClientStub
from rpc_grpc.generated.service_pb2 import UpdateStockRequest
from rpc_grpc.generated.service_pb2_grpc import InventoryServiceStub
from rpc_grpc.interceptors import rpc_status

PRODUCT_ID = "SKU-1001"
QUANTITY = 3


def _operation(product_id: str, quantity: int) -> str:
    return f"Sortir {quantity} unités du stock de {product_id} et obtenir le nouveau stock"


OPERATION = _operation(PRODUCT_ID, QUANTITY)


# --- La même opération, quatre écritures ---------------------------------------------

def update_stock_local(service: InventoryService, product_id: str, quantity: int = 3) -> int:
    """Appel local : une méthode d'un objet qui vit dans le même processus."""
    result = service.update_stock(product_id, -quantity)
    return result["new_stock"]


def update_stock_custom_rpc(stub: RpcClientStub, product_id: str, quantity: int = 3) -> int:
    """RPC maison : la même ligne — mais « stub » est un mandataire, la procédure s'exécute ailleurs."""
    result = stub.update_stock(product_id, -quantity)
    return result["new_stock"]


def update_stock_grpc(grpc_stub: InventoryServiceStub, product_id: str, quantity: int = 3) -> int:
    """gRPC : stub généré depuis le contrat ; la requête et la réponse sont des messages typés."""
    request = UpdateStockRequest(product_id=product_id, delta=-quantity)
    reply = grpc_stub.UpdateStock(request, timeout=DEFAULT_TIMEOUT_S)
    return reply.new_stock


def update_stock_rest_by_hand(host: str, port: int, product_id: str, quantity: int = 3) -> int:
    """REST « à la main » : sans stub, chaque geste du protocole revient à l'appelant."""
    # 1. Construire l'URL de la ressource, sérialiser les arguments, poser les en-têtes.
    path = f"/api/stock/{quote(product_id, safe='')}"
    body = json.dumps({"delta": -quantity}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    connection = http.client.HTTPConnection(host, port, timeout=DEFAULT_TIMEOUT_S)
    try:
        # 2. Choisir le verbe, envoyer, attendre, puis désérialiser la réponse.
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        document = json.loads(response.read())
    except TimeoutError as exc:
        # 3. Délai dépassé : impossible de savoir si le stock a été modifié.
        raise RpcTimeoutError(f"Aucune réponse de {host}:{port}", protocol="rest") from exc
    except (OSError, http.client.HTTPException) as exc:
        # 4. Panne réseau : connexion refusée, ou coupée en plein appel.
        raise RpcTransportError(f"Appel REST impossible : {exc}", protocol="rest") from exc
    finally:
        connection.close()
    if response.status != http.client.OK:
        # 5. Interpréter le statut HTTP et traduire l'erreur métier en exception.
        error = document["error"]
        raise error_from_code(error["code"], error["message"], protocol="rest")
    return document["new_stock"]


# --- L'équivalent côté navigateur ------------------------------------------------------

_JAVASCRIPT_FETCH = """\
async function updateStock(productId, quantity = 3) {
  // Aucun stub : URL, verbe, en-têtes, JSON et statut sont écrits à la main, comme en Python.
  const response = await fetch(`/api/stock/${encodeURIComponent(productId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ delta: -quantity }),
    signal: AbortSignal.timeout(%d),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(`${payload.error.code} : ${payload.error.message}`);
  }
  return payload.new_stock;
}""" % (DEFAULT_TIMEOUT_S * 1000)

_JAVASCRIPT_CONCERNS = (
    "Construire l'URL et échapper ses segments",
    "Choisir le verbe HTTP",
    "Poser les en-têtes (Content-Type, Accept)",
    "Sérialiser les arguments en JSON",
    "Fixer le délai d'attente (AbortSignal)",
    "Désérialiser le corps de la réponse",
    "Tester response.ok et traduire l'erreur",
)

_BUSINESS_FAILURES = "Erreur métier : produit inconnu, stock insuffisant"
_NETWORK_FAILURES = "Délai dépassé, sans savoir si le stock a été modifié"
_OUTAGE_FAILURES = "Serveur injoignable ou connexion coupée en plein appel"


@dataclass(frozen=True)
class _Variant:
    """Une écriture de l'opération, et ce qu'il faut en dire quand on l'affiche."""

    id: str                         # même identifiant que le protocole (local, custom, grpc, rest)
    title: str
    function: Callable[..., int]
    setup: str                      # comment obtenir l'objet passé en premier argument
    concerns: tuple[str, ...]       # ce que l'appelant doit gérer lui-même, à chaque appel
    failures: tuple[str, ...]       # ce qui peut faire échouer l'appel
    note: str

    @property
    def call(self) -> str:
        """L'appel tel qu'on l'écrirait : les arguments propres à l'écriture, puis la référence du produit."""
        own = list(inspect.signature(self.function).parameters)[:-2]
        return f"{self.function.__name__}({', '.join(own)}, {json.dumps(PRODUCT_ID)})"


_VARIANTS: tuple[_Variant, ...] = (
    _Variant(
        id="local",
        title="Appel local",
        function=update_stock_local,
        setup="service = InventoryService()",
        concerns=(),
        failures=(_BUSINESS_FAILURES,),
        note=(
            "La référence : aucun réseau, aucune sérialisation. L'appel rend toujours la main, et ne peut "
            "échouer que pour une raison métier."
        ),
    ),
    _Variant(
        id="custom",
        title="RPC maison — stub JSON-RPC",
        function=update_stock_custom_rpc,
        setup="stub = RpcClientStub(host, port)",
        concerns=(),
        failures=(_BUSINESS_FAILURES, _NETWORK_FAILURES, _OUTAGE_FAILURES),
        note=(
            "Exactement la ligne de l'appel local : c'est la transparence de localisation. Le stub sérialise, "
            "envoie, attend et désérialise — mais l'appel peut désormais lever ce qu'un appel local ne lève "
            "jamais : RpcTimeoutError, RpcTransportError."
        ),
    ),
    _Variant(
        id="grpc",
        title="gRPC — stub généré",
        function=update_stock_grpc,
        setup='grpc_stub = InventoryServiceStub(grpc.insecure_channel(f"{host}:{port}"))',
        concerns=(
            "Construire le message de requête typé (UpdateStockRequest)",
            "Fixer l'échéance de l'appel (timeout)",
        ),
        failures=(_BUSINESS_FAILURES, _NETWORK_FAILURES, _OUTAGE_FAILURES),
        note=(
            "Le contrat impose un message typé : une ligne de plus, en échange d'arguments vérifiés avant "
            "tout envoi et d'un stub généré dans n'importe quel langage. Les échecs arrivent sous la forme "
            "d'une grpc.RpcError porteuse d'un statut."
        ),
    ),
    _Variant(
        id="rest",
        title="REST à la main — http.client",
        function=update_stock_rest_by_hand,
        setup="# aucun objet à créer : l'hôte et le port sont passés à chaque appel",
        concerns=(
            "Construire l'URL et échapper ses segments",
            "Choisir le verbe HTTP",
            "Sérialiser les arguments en JSON",
            "Poser les en-têtes (Content-Type, Accept)",
            "Ouvrir, puis fermer la connexion",
            "Fixer le délai d'attente",
            "Désérialiser le corps de la réponse",
            "Interpréter le code de statut HTTP",
            "Traduire pannes réseau et erreurs métier en exceptions",
        ),
        failures=(_BUSINESS_FAILURES, _NETWORK_FAILURES, _OUTAGE_FAILURES),
        note=(
            "Rien n'est caché, donc tout est à écrire — et à réécrire pour chaque route. Cette version ouvre "
            "même une connexion par appel : la réutiliser (keep-alive) serait une préoccupation de plus."
        ),
    ),
)


# --- Sources affichables ----------------------------------------------------------------

def count_code_lines(code: str, language: str = "python") -> int:
    """Lignes utiles d'un extrait : ni vides, ni commentaires, ni docstring de fonction."""
    skipped: set[int] = set()
    if language == "python":
        first = ast.parse(code).body[0]
        docstring = first.body[0] if isinstance(first, ast.FunctionDef) and ast.get_docstring(first) else None
        if docstring is not None:
            skipped.update(range(docstring.lineno, docstring.end_lineno + 1))
    comment = "#" if language == "python" else "//"
    return sum(
        1
        for number, line in enumerate(code.splitlines(), start=1)
        if number not in skipped and line.strip() and not line.strip().startswith(comment)
    )


def _snippet(variant: _Variant) -> dict[str, Any]:
    code = textwrap.dedent(inspect.getsource(variant.function)).rstrip()
    return {
        "id": variant.id,
        "title": variant.title,
        "language": "python",
        "function": variant.function.__name__,
        "setup": variant.setup,
        "call": variant.call,
        "code": code,
        "lines": count_code_lines(code),
        "concerns": list(variant.concerns),
        "failures": list(variant.failures),
        "note": variant.note,
    }


def code_comparison() -> dict[str, Any]:
    """Les quatre écritures, prêtes à être affichées côte à côte (``GET /api/code-compare``).

    ``{operation, method, product_id, quantity, snippets: [{id, title, language,
    function, setup, call, code, lines, concerns, failures, note}],
    javascript_fetch: {title, language, code, lines, concerns}, takeaway}``.
    ``code`` est la source réelle de la fonction (``inspect.getsource``) :
    ce qui est affiché est ce qui s'exécute.
    """
    snippets = [_snippet(variant) for variant in _VARIANTS]
    by_id = {snippet["id"]: snippet for snippet in snippets}
    local, stub, rest = by_id["local"], by_id["custom"], by_id["rest"]
    return {
        "operation": OPERATION,
        "method": "update_stock",
        "product_id": PRODUCT_ID,
        "quantity": QUANTITY,
        "snippets": snippets,
        "javascript_fetch": {
            "title": "Navigateur — fetch()",
            "language": "javascript",
            "code": _JAVASCRIPT_FETCH,
            "lines": count_code_lines(_JAVASCRIPT_FETCH, "javascript"),
            "concerns": list(_JAVASCRIPT_CONCERNS),
        },
        "takeaway": (
            "Les quatre écritures font la même chose et renvoient le même stock. Avec un stub, l'appel "
            f"distant tient en {stub['lines']} lignes, autant que l'appel local ({local['lines']}) : le "
            "middleware se charge de la sérialisation, du transport et de la traduction des erreurs. Sans "
            f"stub, ces {len(rest['concerns'])} préoccupations reviennent à l'appelant — {rest['lines']} "
            "lignes, à réécrire pour chaque route. Mais cette transparence est aussi un piège : rien, dans "
            "le code, ne distingue l'appel qui coûte quelques microsecondes de celui qui traverse un réseau, "
            "peut expirer, et dont l'échec ne dit pas si le stock a été modifié."
        ),
    }


# --- Exécution comparée -----------------------------------------------------------------

# Une comparaison à la fois : deux exécutions simultanées se disputeraient le stock du même produit,
# chacune « rétablissant » un stock que l'autre vient de modifier, et se croiraient non équivalentes.
_COMPARISON_LOCK = threading.Lock()

def _describe_error(error: Exception) -> dict[str, str]:
    """Erreur de n'importe laquelle des quatre écritures, ramenée au code canonique commun."""
    if isinstance(error, grpc.RpcError):
        # La classe concrète est un détail interne de grpcio : on nomme l'exception que l'appelant attrape.
        status, message = rpc_status(error)
        return {"type": "grpc.RpcError", "code": GRPC_TO_CANONICAL.get(status, INTERNAL), "message": message}
    return {"type": type(error).__name__, "code": error.code, "message": error.message}


def _measure(
    variant: _Variant, arguments: tuple[Any, ...], restore: Callable[[], None], repeats: int
) -> dict[str, Any]:
    """Exécute une écriture ``repeats`` fois (après un passage d'échauffement) et rend son issue et sa durée."""
    durations: list[float] = []
    outcome: dict[str, Any] = {}
    for attempt in range(repeats + 1):
        started = time.perf_counter_ns()
        try:
            outcome = {"ok": True, "result": variant.function(*arguments), "error": None}
        except (DomainError, RpcError, grpc.RpcError) as error:
            outcome = {"ok": False, "result": None, "error": _describe_error(error)}
        elapsed_ms = (time.perf_counter_ns() - started) / 1e6
        restore()   # chaque passage, et chaque écriture, repart du même stock
        if not outcome["ok"]:
            # Un échec ne se rejoue pas : face à un serveur muet, chaque passage coûterait un délai entier.
            durations = [elapsed_ms]
            break
        if attempt:   # le premier passage ouvre connexion et canal HTTP/2 : il n'est pas mesuré
            durations.append(elapsed_ms)
    return {
        "id": variant.id,
        "title": variant.title,
        "function": variant.function.__name__,
        **outcome,
        "duration_ms": round(statistics.median(durations), 4),
        "min_ms": round(min(durations), 4),
    }


def run_comparison(
    runtime: LabRuntime, *, product_id: str = PRODUCT_ID, quantity: int = QUANTITY, repeats: int = 5
) -> dict[str, Any]:
    """Exécute les quatre écritures sur un laboratoire démarré et vérifie qu'elles sont équivalentes.

    Toutes partent du même stock — il est rétabli après chaque appel,
    directement dans le service — et doivent donc renvoyer le même nouveau stock,
    ou échouer avec le même code d'erreur. ``duration_ms`` est la médiane de
    ``repeats`` appels, bus de traces coupé. Au retour, le stock du produit est
    celui d'avant. Des comparaisons lancées en même temps s'exécutent l'une
    après l'autre. Lève ``ProductNotFound`` si le produit n'existe pas.

    ``{operation, product_id, quantity, repeats, stock_before, expected_new_stock,
    stock_after, runs: [{id, title, function, ok, result, error, duration_ms,
    min_ms, ratio_to_local}], equivalent, summary}``.
    """
    if repeats < 1:
        raise ValueError(f"« repeats » doit être ≥ 1 (reçu : {repeats})")
    service = runtime.service
    with _COMPARISON_LOCK:
        stock_before = service.check_stock(product_id)["stock"]

        def restore() -> None:
            drift = service.check_stock(product_id)["stock"] - stock_before
            if drift:
                service.update_stock(product_id, -drift)

        stub = RpcClientStub(*runtime.endpoint("custom"), bus=runtime.bus)
        channel = grpc.insecure_channel("{}:{}".format(*runtime.endpoint("grpc")))
        targets: dict[str, tuple[Any, ...]] = {
            "local": (service,),
            "custom": (stub,),
            "grpc": (InventoryServiceStub(channel),),
            "rest": runtime.endpoint("rest"),
        }
        try:
            # Bus coupé : on compare les quatre écritures, pas leur instrumentation — et la version REST,
            # qui n'envoie pas d'identifiant de corrélation, ne laisse pas de trace orpheline côté serveur.
            with runtime.bus.muted():
                runs = [
                    _measure(variant, (*targets[variant.id], product_id, quantity), restore, repeats)
                    for variant in _VARIANTS
                ]
        finally:
            stub.close()
            channel.close()
            restore()
        stock_after = service.check_stock(product_id)["stock"]

    local_ms = runs[0]["duration_ms"]
    for run in runs:
        run["ratio_to_local"] = round(run["duration_ms"] / local_ms, 1) if local_ms > 0 else None
    outcomes = {(run["ok"], run["result"], run["error"] and run["error"]["code"]) for run in runs}
    equivalent = len(outcomes) == 1
    if not equivalent:
        summary = (
            "Les quatre écritures n'ont pas donné le même résultat : un serveur est injoignable, ou le stock "
            "de ce produit a été modifié par ailleurs pendant la comparaison."
        )
    elif runs[0]["ok"]:
        # Sans cette précision, la durée de l'écriture REST (connexion comprise) passerait pour celle du protocole.
        summary = (
            f"Les quatre écritures renvoient le même nouveau stock ({runs[0]['result']}) : seuls changent la "
            "quantité de code à la charge de l'appelant et le temps de l'aller-retour. Ces durées ne classent "
            "pas les protocoles : l'écriture REST ouvre une connexion TCP à chaque appel, quand les deux stubs "
            "réutilisent la leur."
        )
    else:
        summary = (
            f"Les quatre écritures échouent de la même façon ({runs[0]['error']['code']}) : l'erreur métier "
            "traverse chaque middleware sans changer de sens."
        )
    return {
        "operation": _operation(product_id, quantity),
        "product_id": product_id,
        "quantity": quantity,
        "repeats": repeats,
        "stock_before": stock_before,
        "expected_new_stock": stock_before - quantity,
        "stock_after": stock_after,
        "runs": runs,
        "equivalent": equivalent,
        "summary": summary,
    }


# --- Affichage dans le terminal ----------------------------------------------------------

ACCENT = "#8B7CFF"
SUCCESS = "#4ADE80"
DANGER = "#F87171"
MUTED = "grey58"
MAX_WIDTH = 118
_STYLES = {"local": "#8A94A6", "custom": "#5AA2FF", "grpc": "#2FD9C4", "rest": "#F2789F"}


def _duration(milliseconds: float) -> str:
    text = f"{milliseconds * 1000:.1f} µs" if milliseconds < 1 else f"{milliseconds:.2f} ms"
    return text.replace(".", ",")


def main() -> int:
    """Affiche les quatre écritures puis les exécute ; code 1 si elles ne sont pas équivalentes."""
    for stream in (sys.stdout, sys.stderr):   # console Windows : accents et filets en UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    width = min(shutil.get_terminal_size((MAX_WIDTH, 40)).columns, MAX_WIDTH)
    console = Console(width=width, highlight=False, markup=False)

    comparison = code_comparison()
    with LabRuntime.ephemeral(bus=EventBus()) as runtime:
        outcome = run_comparison(runtime)
    runs = {run["id"]: run for run in outcome["runs"]}

    console.print()
    console.print(Panel(
        Group(
            Text.assemble((APP_NAME, f"bold {ACCENT}"), ("  ·  Transparence de localisation", "bold")),
            Text(f"{comparison['operation']} — la même opération, écrite de quatre façons.", style=MUTED),
        ),
        border_style=ACCENT, box=box.ROUNDED, padding=(1, 2),
    ))
    for snippet in comparison["snippets"]:
        style = _STYLES[snippet["id"]]
        concerns = snippet["concerns"]
        footer = Text(style=MUTED)
        footer.append(
            f"À la charge de l'appelant : {len(concerns)} préoccupations" if concerns
            else "Rien à la charge de l'appelant",
            style="bold",
        )
        footer.append("".join(f"\n  · {concern}" for concern in concerns))
        footer.append(f"\n{snippet['note']}")
        console.print(Panel(
            Group(Syntax(snippet["code"], "python", theme="ansi_dark", background_color="default"), Text(), footer),
            title=Text(f" {snippet['title']} — {snippet['lines']} lignes ", style=f"bold {style}"),
            title_align="left", border_style=style, box=box.ROUNDED, padding=(1, 2),
        ))

    table = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", pad_edge=False)
    table.add_column("Écriture")
    table.add_column("Lignes", justify="right")
    table.add_column("À gérer soi-même", justify="right")
    table.add_column("Nouveau stock", justify="right")
    table.add_column("Durée médiane", justify="right")
    table.add_column("× local", justify="right")
    for snippet in comparison["snippets"]:
        run = runs[snippet["id"]]
        result = Text(str(run["result"]), style=SUCCESS) if run["ok"] else Text(run["error"]["code"], style=DANGER)
        ratio = run["ratio_to_local"]
        table.add_row(
            Text(snippet["title"], style=f"bold {_STYLES[snippet['id']]}"),
            str(snippet["lines"]),
            str(len(snippet["concerns"])),
            result,
            _duration(run["duration_ms"]),
            "—" if ratio is None else f"× {ratio:,.0f}".replace(",", " "),
        )
    console.print(table)
    console.print(Text(outcome["summary"], style=f"bold {SUCCESS if outcome['equivalent'] else DANGER}"))
    console.print()
    console.print(Text(comparison["takeaway"], style=MUTED))
    console.print()
    return 0 if outcome["equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
