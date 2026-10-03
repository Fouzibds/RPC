"""Dashboard web : API HTTP + WebSocket (FastAPI) au-dessus du laboratoire (plan §13).

    python -m dashboard.server [--port N] [--no-browser]

Le dashboard ne contient aucune logique RPC : chaque route délègue au laboratoire
(``LabRuntime``), au banc d'essai (``benchmark_lab``) ou au réseau simulé (``netsim``) et
renvoie leurs dictionnaires tels quels. Ce module assemble l'application ; le travail est
réparti entre ``calls`` (appels RPC), ``jobs`` (tâches de fond), ``hub`` (WebSocket),
``summary`` (bilan) et ``payloads`` (validation et réponses JSON).

Conventions de l'API :

* tout est JSON ; une erreur d'API a la forme ``{"error": {"code", "message"}}`` — 422 si le
  corps est mal formé, 400 si le laboratoire refuse ses valeurs, 404 pour un identifiant
  inconnu, 409 quand une tâche de fond occupe le laboratoire ;
* un appel RPC qui échoue n'est PAS une erreur d'API : ``/api/call`` et ``/api/inspect``
  répondent 200 avec ``ok: false`` — l'échec d'un appel distant est un résultat à étudier ;
* rien de bloquant ne s'exécute dans la boucle d'évènements.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import socket
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import uvicorn
from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.concurrency import run_in_threadpool
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from benchmark_lab import failure_simulation
from benchmark_lab.benchmark_perf import QUICK_CONFIG, resolve_config
from benchmark_lab.contract_evolution import CONTRACT_SCENARIOS, contract_overview, run_contract_scenario
from benchmark_lab.report import list_reports, load_report, to_csv, to_markdown
from benchmark_lab.transparency_demo import PRODUCT_ID, QUANTITY, code_comparison, run_comparison
from common.catalog import METHODS
from common.config import (
    APP_NAME,
    APP_TAGLINE,
    DEFAULT_TIMEOUT_S,
    HOST,
    PROTOCOL_LABELS,
    PROTOCOL_TRANSPORTS,
    PROTOCOLS,
    REMOTE_PROTOCOLS,
    VERSION,
)
from common.errors import ProductNotFound, RpcError
from common.telemetry import PIPELINE, STAGE_INFO
from lab import LabRuntime
from netsim import presets_dict

from .calls import (
    ASYNC_WORKERS,
    MAX_ASYNC_CALLS,
    MAX_TIMEOUT_MS,
    MODES,
    CallRequest,
    CallService,
    InspectRequest,
    required_params,
    trace_payload,
)
from .hub import TOPICS, Hub
from .jobs import JobManager, LabBusy
from .payloads import (
    BUSY,
    INTERNAL,
    NOT_FOUND,
    UNAVAILABLE,
    ApiError,
    Fields,
    malformed,
    not_found,
    query_int,
    read_object,
    refused,
    reply,
    to_json,
)
from .summary import build_summary

STATIC_DIR = Path(__file__).resolve().parent / "static"

MAX_TRACES = 400                # capacité du collecteur de traces
MAX_ARMED_FAULTS = 1000
BROWSER_PATIENCE_S = 15.0       # délai laissé au serveur pour répondre avant d'ouvrir le navigateur
GRACEFUL_SHUTDOWN_S = 3         # à l'arrêt, les WebSockets encore ouverts ne retiennent pas le processus

_log = logging.getLogger(__name__)

_REPORT_MEDIA_TYPES: dict[str, str] = {
    "json": "application/json",
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
}
_REPORT_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "json": lambda report: json.dumps(report, ensure_ascii=False, indent=2),
    "md": to_markdown,
    "csv": to_csv,
}
_HTTP_ERROR_CODES: dict[int, str] = {404: NOT_FOUND, 405: "METHOD_NOT_ALLOWED"}
_FAILURE_SCENARIOS = {scenario["id"]: scenario for scenario in failure_simulation.SCENARIOS}
_CONTRACT_SCENARIO_IDS = tuple(scenario["id"] for scenario in CONTRACT_SCENARIOS)
_ALL = "all"

_NOT_BUILT_PAGE = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{APP_NAME} · {APP_TAGLINE}</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #07090D;
         color: #EDEFF3; font: 15px/1.6 system-ui, "Segoe UI", sans-serif; }}
  main {{ max-width: 34rem; padding: 2rem; }}
  h1 {{ font-size: 1.4rem; letter-spacing: -0.02em; margin: 0 0 .75rem; }}
  p {{ color: #A9B1BF; margin: .5rem 0; }}
  code, a {{ color: #8B7CFF; font-family: ui-monospace, Consolas, monospace; text-decoration: none; }}
</style>
</head>
<body>
<main>
  <h1>L'interface web n'est pas encore construite</h1>
  <p>Le laboratoire {APP_NAME} est bien démarré et son API répond, mais le fichier
     <code>dashboard/static/index.html</code> est introuvable.</p>
  <p>En attendant : <a href="/api/status">/api/status</a> · <a href="/api/catalog">/api/catalog</a> ·
     <a href="/api/summary">/api/summary</a></p>
</main>
</body>
</html>
"""


# --- Données statiques ---------------------------------------------------------------

@lru_cache(maxsize=1)
def _static_catalog() -> dict[str, Any]:
    """Partie du catalogue qui ne change pas pendant la vie du processus, et son empreinte."""
    catalog: dict[str, Any] = {
        "methods": [{**spec.to_dict(), "required": list(required_params(spec.name))} for spec in METHODS],
        "protocols": [
            {"id": protocol, "label": PROTOCOL_LABELS[protocol], "transport": PROTOCOL_TRANSPORTS[protocol]}
            for protocol in PROTOCOLS
        ],
        "stages": STAGE_INFO,
        "pipeline": list(PIPELINE),
        "limits": {
            "async_calls_max": MAX_ASYNC_CALLS,
            "async_workers": ASYNC_WORKERS,
            "timeout_ms_default": int(DEFAULT_TIMEOUT_S * 1000),
            "timeout_ms_max": MAX_TIMEOUT_MS,
        },
        "modes": list(MODES),
        "topics": list(TOPICS),
    }
    # Empreinte annoncée dans « hello » : une interface restée ouverte sait si elle doit recharger le catalogue.
    return {"version": hashlib.sha1(to_json(catalog).encode("utf-8")).hexdigest()[:12], **catalog}


@lru_cache(maxsize=1)
def _contract_overview() -> dict[str, Any]:
    return contract_overview()      # lecture et diff des deux .proto : une fois suffit


@lru_cache(maxsize=1)
def _code_comparison() -> dict[str, Any]:
    return code_comparison()


# --- État partagé par les routes -----------------------------------------------------

class _Lab:
    """Le laboratoire et les services du dashboard qui s'y branchent."""

    def __init__(self, runtime: LabRuntime) -> None:
        self.runtime = runtime
        self.hub = Hub(runtime.bus, runtime.collector, status=self.status, greeting=self.greeting)
        self.jobs = JobManager(runtime, self.hub.publish)
        self.calls = CallService(runtime, self.hub.publish)
        self.contract_results: dict[str, dict[str, Any]] = {}   # dernier résultat de chaque scénario de contrat

    def status(self) -> dict[str, Any]:
        """État complet (bloquant : ``LabRuntime.status()`` sonde les serveurs en TCP)."""
        return {**self.runtime.status(), "job": self.jobs.active(), "breakers": self.calls.breakers()}

    def greeting(self) -> dict[str, Any]:
        return {
            "app": {"name": APP_NAME, "tagline": APP_TAGLINE, "version": VERSION},
            "catalog_version": _static_catalog()["version"],
            "network": self.runtime.conditions.snapshot(),
            "job": self.jobs.active(),
        }

    def network(self) -> dict[str, Any]:
        runtime = self.runtime
        return {
            "conditions": runtime.conditions.snapshot(),
            "presets": presets_dict(),
            "proxies": {protocol: proxy.stats() for protocol, proxy in runtime.proxies.items()},
            "armed": {protocol: proxy.armed() for protocol, proxy in runtime.proxies.items()},
        }

    def require_started(self) -> None:
        if not self.runtime.started:
            raise ApiError(503, UNAVAILABLE, "Le laboratoire n'est pas démarré : ses serveurs ne répondent pas.")

    def reset(self) -> None:
        self.runtime.reset()
        self.calls.reset()
        self.jobs.forget_results()
        self.contract_results.clear()

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        await self.hub.start()
        try:
            yield
        finally:
            await self.hub.stop()
            self.calls.close()


# --- Routes : état, catalogue, traces --------------------------------------------------

def _lab_routes(lab: _Lab) -> APIRouter:
    router = APIRouter()
    runtime = lab.runtime

    @router.get("/api/health")
    async def health() -> Response:
        return reply({"status": "ok", "app": APP_NAME, "version": VERSION, "started": runtime.started})

    @router.get("/api/status")
    async def status() -> Response:
        return reply(await run_in_threadpool(lab.status))

    @router.get("/api/catalog")
    async def catalog() -> Response:
        service = runtime.service
        listing = service.list_products(limit=len(service.product_ids()))
        return reply({
            **_static_catalog(),
            "products": [
                {key: product[key] for key in ("id", "name", "category", "stock")}
                for product in listing["products"]
            ],
            "categories": service.categories(),
        })

    @router.get("/api/traces")
    async def traces(request: Request) -> Response:
        limit = query_int(request, "limit", 50, 1, MAX_TRACES)
        return reply({"traces": [trace.summary() for trace in runtime.collector.recent(limit)]})

    @router.get("/api/traces/{call_id}")
    async def trace(call_id: str) -> Response:
        found = runtime.collector.get(call_id)
        if found is None:
            raise not_found(
                f"Trace inconnue : {call_id!r} (le collecteur ne garde que les {MAX_TRACES} derniers appels)."
            )
        return reply(trace_payload(found))

    @router.get("/api/summary")
    async def summary() -> Response:
        def build() -> dict[str, Any]:      # le dernier rapport peut être relu sur disque
            return build_summary(
                lab.jobs.latest_benchmark(), lab.jobs.failure_results, lab.contract_results, lab.calls.latest_batches
            )

        return reply(await run_in_threadpool(build))

    @router.post("/api/reset")
    async def reset() -> Response:
        lab.jobs.require_idle("remettre le laboratoire à zéro")
        await run_in_threadpool(lab.reset)
        return reply({"ok": True, "status": await run_in_threadpool(lab.status)})

    return router


# --- Routes : appels RPC ---------------------------------------------------------------

def _call_routes(lab: _Lab) -> APIRouter:
    router = APIRouter()

    @router.post("/api/call")
    async def call(request: Request) -> Response:
        demand = CallRequest.parse(await read_object(request))
        lab.require_started()
        if demand.via_proxy:
            lab.jobs.require_idle("lancer un appel à travers le proxy de chaos")
        if demand.mode == "stream":
            stream_id = lab.calls.start_stream(demand)
            return reply({
                "ok": True, "mode": "stream", "stream_id": stream_id,
                "protocol": demand.protocol, "method": demand.method, "via_proxy": demand.via_proxy,
            })
        return reply(await lab.calls.execute(demand))

    @router.post("/api/inspect")
    async def inspect(request: Request) -> Response:
        demand = InspectRequest.parse(await read_object(request))
        lab.require_started()
        # Une inspection lit les traces : pendant le banc d'essai, le bus est coupé par intermittence.
        lab.jobs.require_idle("inspecter un appel")
        return reply({
            "method": demand.method,
            "params": demand.params,
            "via_proxy": demand.via_proxy,
            "traces": await lab.calls.inspect(demand),
        })

    return router


# --- Routes : réseau simulé ------------------------------------------------------------

def _network_routes(lab: _Lab) -> APIRouter:
    router = APIRouter()
    runtime = lab.runtime

    @router.get("/api/network")
    async def network() -> Response:
        return reply(lab.network())

    @router.put("/api/network")
    async def update_network(request: Request) -> Response:
        changes = await read_object(request)
        if not changes:
            raise malformed("Indiquez un préréglage (« preset ») ou au moins un champ de conditions réseau.")
        lab.jobs.require_idle("modifier le réseau simulé")
        try:
            # « preset » sert de base, les autres champs le surchargent : {preset} seul applique le préréglage.
            runtime.conditions.update(**changes)
        except (ValueError, TypeError) as exc:
            raise refused(f"Conditions réseau refusées : {exc}") from None
        return reply(lab.network())

    @router.post("/api/network/arm")
    async def arm(request: Request) -> Response:
        fields = Fields(await read_object(request), ("kind", "protocol", "count"))
        kind, protocol = fields.text("kind"), fields.text("protocol")
        count = fields.integer("count", 1, 0, MAX_ARMED_FAULTS)
        lab.jobs.require_idle("armer une panne")
        try:
            runtime.arm(kind, protocol, count)
        except ValueError as exc:
            raise refused(str(exc)) from None
        return reply({"armed": {name: proxy.armed() for name, proxy in runtime.proxies.items()}})

    return router


# --- Routes : expériences (banc d'essai, pannes, contrat, transparence) ----------------

def _experiment_routes(lab: _Lab) -> APIRouter:
    router = APIRouter()
    runtime = lab.runtime

    @router.post("/api/benchmark/run")
    async def run_benchmark(request: Request) -> Response:
        settings = await read_object(request)
        # « quick » est propre à l'API : il choisit la base (réglages courts) que les autres champs surchargent.
        quick = settings.pop("quick", None)
        if quick is not None and not isinstance(quick, bool):
            raise malformed(f"Le champ « quick » doit être un booléen (reçu : {quick!r}).")
        if quick:
            settings = {**QUICK_CONFIG, **{name: value for name, value in settings.items() if value is not None}}
        try:
            config = resolve_config(settings)   # clé inconnue, protocole inconnu, nombre hors bornes…
        except ValueError as exc:
            raise refused(str(exc)) from None
        lab.require_started()
        job = lab.jobs.start_benchmark(config)
        return reply({"job_id": job.id, "job": job.summary(), "config": config}, 202)

    @router.get("/api/benchmark/latest")
    async def latest_benchmark() -> Response:
        return reply(await run_in_threadpool(lab.jobs.latest_benchmark))

    @router.get("/api/failures/scenarios")
    async def failure_scenarios() -> Response:
        return reply({
            "scenarios": [
                {**scenario, "options": failure_simulation.DEFAULT_OPTIONS[scenario["id"]]}
                for scenario in failure_simulation.SCENARIOS
            ],
            "metrics": failure_simulation.METRIC_INFO,
            "step_kinds": list(failure_simulation.STEP_KINDS),
            "step_statuses": list(failure_simulation.STEP_STATUSES),
            "results": lab.jobs.failure_results,
        })

    @router.post("/api/failures/run")
    async def run_failure(request: Request) -> Response:
        fields = Fields(await read_object(request), ("scenario", "protocol", "options"))
        scenario = fields.text("scenario")
        if scenario not in _FAILURE_SCENARIOS:
            raise not_found(f"Scénario de panne inconnu : {scenario!r} (connus : {', '.join(_FAILURE_SCENARIOS)}).")
        protocol = fields.text("protocol", "custom", choices=REMOTE_PROTOCOLS)
        options = fields.mapping("options", None)
        accepted = failure_simulation.DEFAULT_OPTIONS[scenario]
        unknown = sorted(set(options or ()) - set(accepted))
        if unknown:
            raise malformed(
                f"Option(s) inconnue(s) pour « {scenario} » : {', '.join(unknown)} "
                f"(acceptées : {', '.join(accepted) or 'aucune'})."
            )
        lab.require_started()
        job = lab.jobs.start_failure(scenario, protocol, options)
        return reply({"job_id": job.id, "job": job.summary()}, 202)

    @router.get("/api/jobs")
    async def jobs() -> Response:
        return reply({"jobs": [job.summary() for job in lab.jobs.recent()], "active": lab.jobs.active()})

    @router.get("/api/jobs/{job_id}")
    async def job(job_id: str) -> Response:
        found = lab.jobs.get(job_id)
        if found is None:
            raise not_found(f"Tâche inconnue : {job_id!r}.")
        return reply(found.snapshot())

    @router.get("/api/contract")
    async def contract() -> Response:
        overview = await run_in_threadpool(_contract_overview)
        return reply({**overview, "strict": runtime.grpc_v2.strict, "results": lab.contract_results})

    @router.post("/api/contract/run")
    async def run_contract(request: Request) -> Response:
        fields = Fields(await read_object(request), ("scenario", "strict"))
        scenario = fields.text("scenario", _ALL)
        strict = fields.boolean("strict", None)
        if scenario != _ALL and scenario not in _CONTRACT_SCENARIO_IDS:
            raise not_found(
                f"Scénario de contrat inconnu : {scenario!r} (connus : {', '.join(_CONTRACT_SCENARIO_IDS)}, "
                f"ou « {_ALL} »)."
            )
        lab.require_started()

        def play() -> list[dict[str, Any]]:
            if strict is not None:
                # Chaque scénario impose sa propre valeur de « strict » le temps de son appel, puis rétablit
                # celle-ci : le réglage demandé vaut pour le serveur v2 entre deux scénarios.
                runtime.grpc_v2.strict = strict
            results = []
            for scenario_id in _CONTRACT_SCENARIO_IDS if scenario == _ALL else (scenario,):
                result = run_contract_scenario(runtime, scenario_id)
                lab.contract_results[scenario_id] = result
                results.append(result)
            return results

        try:
            results = await run_in_threadpool(play)
        except RpcError as exc:
            raise ApiError(503, UNAVAILABLE, f"Serveur « contrat v2 » injoignable : {exc.message}") from None
        return reply({"results": results, "strict": runtime.grpc_v2.strict})

    @router.get("/api/code-compare")
    async def code_compare() -> Response:
        return reply(await run_in_threadpool(_code_comparison))

    @router.post("/api/code-compare/run")
    async def run_code_compare(request: Request) -> Response:
        fields = Fields(await read_object(request), ("product_id", "quantity", "repeats"))
        product_id = fields.text("product_id", PRODUCT_ID)
        quantity = fields.integer("quantity", QUANTITY, 1, 1000)
        repeats = fields.integer("repeats", 5, 1, 50)
        lab.require_started()
        # La comparaison coupe le bus de traces : pendant un banc d'essai, elle en fausserait la mesure des tailles.
        lab.jobs.require_idle("exécuter les quatre écritures")
        try:
            result = await run_in_threadpool(
                run_comparison, runtime, product_id=product_id, quantity=quantity, repeats=repeats
            )
        except ProductNotFound as exc:
            raise not_found(exc.message) from None
        return reply(result)

    return router


# --- Routes : rapports -----------------------------------------------------------------

def _report_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/reports")
    def reports() -> Response:
        return reply({"reports": list_reports()})

    @router.get("/api/reports/{name}")
    def report(name: str, request: Request) -> Response:
        export = request.query_params.get("format", "json")
        if export not in _REPORT_RENDERERS:
            raise malformed(f"Format inconnu : {export!r} (attendu : {', '.join(_REPORT_RENDERERS)}).")
        try:
            document = load_report(name)
        except FileNotFoundError as exc:
            raise not_found(str(exc)) from None
        except ValueError as exc:   # nom refusé (chemin, caractères interdits) ou fichier illisible
            raise refused(str(exc)) from None
        # ``load_report`` n'accepte que lettres, chiffres, « . », « _ » et « - » : le nom est sûr dans un en-tête.
        filename = f"{name.removesuffix('.json')}.{export}"
        return Response(
            _REPORT_RENDERERS[export](document),
            media_type=_REPORT_MEDIA_TYPES[export],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router


# --- Erreurs, cache, interface -----------------------------------------------------------

def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def on_api_error(request: Request, exc: ApiError) -> Response:
        return reply(exc.body(), exc.status)

    @app.exception_handler(LabBusy)
    async def on_busy(request: Request, exc: LabBusy) -> Response:
        return reply({"error": {"code": BUSY, "message": exc.message, "job": exc.job.summary()}}, 409)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.url.path
        if exc.status_code == 404:
            message = f"Ressource introuvable : {path}"
        elif exc.status_code == 405:
            message = f"Méthode {request.method} non autorisée pour {path}"
        else:
            message = str(exc.detail)
        code = _HTTP_ERROR_CODES.get(exc.status_code, "HTTP_ERROR")
        return reply({"error": {"code": code, "message": message}}, exc.status_code, exc.headers)

    @app.exception_handler(Exception)
    async def on_crash(request: Request, exc: Exception) -> Response:
        _log.error("Erreur inattendue sur %s %s", request.method, request.url.path, exc_info=exc)
        message = f"Erreur interne du dashboard ({type(exc).__name__}) : {exc}"
        return reply({"error": {"code": INTERNAL, "message": message}}, 500)


class _NoStore:
    """Interdit toute mise en cache des réponses.

    L'interface est servie sans étape de build : un fichier modifié doit être rechargé tel
    quel, et une mesure de l'API ne doit jamais être relue dans le cache du navigateur.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self._app(scope, receive, send_no_store)


class _InterfaceFiles(StaticFiles):
    """Fichiers de l'interface.

    Un dossier absent est ici un état normal — l'interface n'a pas encore été déposée : chaque
    fichier demandé donne alors un 404, au lieu de l'erreur de configuration que lève Starlette.
    """

    async def check_config(self) -> None:
        return None


def _mount_interface(app: FastAPI, static_dir: Path) -> None:
    """Sert l'interface à la racine — après les routes de l'API, qui gardent ainsi la priorité."""
    index = static_dir / "index.html"

    @app.get("/", include_in_schema=False)
    async def interface() -> Response:
        # Testé à chaque requête : l'interface peut être déposée pendant que le serveur tourne.
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse(_NOT_BUILT_PAGE)

    app.mount("/", _InterfaceFiles(directory=static_dir, html=True, check_dir=False), name="static")


def create_app(runtime: LabRuntime, *, static_dir: Path | None = None) -> FastAPI:
    """Construit l'application du dashboard autour d'un laboratoire (démarré ou non).

    ``static_dir`` remplace ``dashboard/static`` (utile aux tests). L'application ne démarre
    ni n'arrête ``runtime`` : c'est le rôle de ``serve()``.
    """
    lab = _Lab(runtime)
    app = FastAPI(
        title=f"{APP_NAME} · {APP_TAGLINE}",
        version=VERSION,
        lifespan=lab.lifespan,
        # Pas de documentation interactive : elle chargerait ses scripts depuis Internet, or le
        # laboratoire doit fonctionner hors ligne.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(_NoStore)
    _install_error_handlers(app)
    routers = (_lab_routes(lab), _call_routes(lab), _network_routes(lab), _experiment_routes(lab), _report_routes())
    for router in routers:
        app.include_router(router)

    @app.websocket("/ws")
    async def live(websocket: WebSocket) -> None:
        await lab.hub.serve(websocket)

    _mount_interface(app, STATIC_DIR if static_dir is None else Path(static_dir))
    return app


# --- Service au premier plan -------------------------------------------------------------

_ACCENT = "#8B7CFF"
_MUTED = "grey58"
_PROTOCOL_STYLES = {"custom": "#5AA2FF", "grpc": "#2FD9C4", "rest": "#F2789F"}


def _utf8_console() -> None:
    """Console Windows : accents et cadres sortent en UTF-8, quelle que soit la page de codes."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _listen(host: str, port: int) -> socket.socket:
    """Réserve le port du dashboard avant tout le reste : une erreur claire vaut mieux qu'un arrêt d'uvicorn."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            # Sous Windows, SO_REUSEADDR laisserait deux dashboards écouter le même port sans erreur.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
    except OSError as exc:
        listener.close()
        raise RuntimeError(
            f"Le dashboard ne peut pas écouter sur {host}:{port} : le port est déjà utilisé (peut-être par un "
            "autre dashboard). Choisissez-en un autre avec --port, ou décalez tous les ports du laboratoire "
            "avec la variable d'environnement RPCX_PORT_OFFSET."
        ) from exc
    return listener


def _print_banner(console: Console, runtime: LabRuntime, url: str) -> None:
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style=_MUTED)
    summary.add_column()
    summary.add_row("Dashboard", Text(url, style=f"bold {_ACCENT} underline"))
    summary.add_row("Temps réel", Text(url.replace("http://", "ws://", 1) + "/ws"))
    summary.add_row("Arrêt", Text("Ctrl+C"))
    console.print()
    console.print(Panel(
        summary,
        title=f"[bold]{APP_NAME}[/] [{_MUTED}]·[/] {APP_TAGLINE} [{_MUTED}]v{VERSION}[/]",
        title_align="left",
        border_style=_ACCENT,
        padding=(1, 2),
        expand=False,
    ))
    # Trois colonnes courtes : le tableau tient dans un terminal de 80 colonnes sans se replier.
    servers = Table(box=box.SIMPLE_HEAD, header_style=f"bold {_MUTED}", padding=(0, 2))
    servers.add_column("Serveur", no_wrap=True)
    servers.add_column("Accès direct", justify="right", no_wrap=True)
    servers.add_column("Proxy de chaos", justify="right", no_wrap=True)
    for server in runtime.status()["servers"]:
        style = _PROTOCOL_STYLES.get(server["id"].removesuffix("_v2"), "")
        proxy = f"{server['host']}:{server['proxy_port']}" if server["proxy_port"] else "—"
        servers.add_row(
            Text(server["label"], style=style),
            f"{server['host']}:{server['port']}",
            Text(proxy, style="" if server["proxy_port"] else _MUTED),
        )
    console.print(servers)


def _open_browser_when_ready(host: str, port: int, url: str) -> None:
    """Attend que le serveur réponde, puis ouvre le navigateur : jamais d'onglet « connexion refusée »."""
    deadline = time.monotonic() + BROWSER_PATIENCE_S
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection(host, port, timeout=1.0)
        try:
            connection.request("GET", "/api/health")
            ready = connection.getresponse().status == 200
        except (OSError, http.client.HTTPException):
            ready = False
        finally:
            connection.close()
        if ready:
            webbrowser.open(url)
            return
        time.sleep(0.1)


def serve(runtime: LabRuntime, host: str = HOST, port: int | None = None, open_browser: bool = True) -> None:
    """Sert le dashboard au premier plan jusqu'à Ctrl+C, puis arrête le laboratoire.

    Démarre ``runtime`` s'il ne l'est pas. ``port=None`` : ``runtime.ports.dashboard``
    (0 laisse le système choisir). Lève ``RuntimeError`` si un port est déjà pris.
    """
    _utf8_console()
    console = Console()
    listener = _listen(host, runtime.ports.dashboard if port is None else port)
    try:
        runtime.start()
    except BaseException:
        listener.close()
        raise
    try:
        bound_port = listener.getsockname()[1]
        local_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        url = f"http://{local_host}:{bound_port}"
        _print_banner(console, runtime, url)
        server = uvicorn.Server(uvicorn.Config(
            create_app(runtime),
            host=host,
            port=bound_port,
            log_level="warning",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        ))
        if open_browser:
            threading.Thread(
                target=_open_browser_when_ready,
                args=(local_host, bound_port, url),
                name="dashboard-browser",
                daemon=True,
            ).start()
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            pass    # uvicorn s'est arrêté proprement, puis relaie Ctrl+C : ce n'est pas une erreur
    finally:
        listener.close()
        runtime.stop()
        console.print(f"[{_MUTED}]Laboratoire arrêté.[/]")


def main(argv: list[str] | None = None) -> int:
    _utf8_console()     # avant argparse : l'aide aussi contient des accents
    parser = argparse.ArgumentParser(
        prog="python -m dashboard.server",
        description="Démarre le laboratoire et sert son dashboard web.",
    )
    parser.add_argument("--host", default=HOST, help="adresse d'écoute du dashboard")
    parser.add_argument(
        "--port", type=int, default=None, help="port du dashboard (par défaut : celui du plan d'adressage)"
    )
    parser.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
    args = parser.parse_args(argv)
    try:
        serve(LabRuntime(), host=args.host, port=args.port, open_browser=not args.no_browser)
    except RuntimeError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
