"""Outillage des tests de bout en bout du dashboard (``tests/test_frontend_smoke.py``).

* ``LiveDashboard`` — le dashboard d'un laboratoire servi pour de vrai : uvicorn dans un
  thread, sur un port choisi par le système ;
* ``PageWatch`` — note tout ce qui ne devrait pas arriver dans une page : erreur de console,
  exception JavaScript, requête en échec, réponse HTTP ≥ 400 ;
* ``launch_browser`` — Microsoft Edge sans interface piloté par Playwright, ou à défaut le
  Chromium fourni avec Playwright (machines Linux, intégration continue) ;
* ``lingering_threads`` — les threads qui auraient survécu à l'arrêt du laboratoire.

Ce module importe Playwright : le fichier de tests ne le charge qu'après
``pytest.importorskip``, pour que le projet reste testable sans navigateur.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from typing import Any

import uvicorn
from playwright.sync_api import (
    Browser,
    ConsoleMessage,
    Error as PlaywrightError,
    Page,
    Playwright,
    Request,
    Response,
    WebSocket,
)

from dashboard import create_app
from lab import LabRuntime

STARTUP_PATIENCE_S = 20.0
SHUTDOWN_PATIENCE_S = 15.0
IDLE_PATIENCE_S = 90.0          # un banc d'essai « rapide » interrompu par un test en échec doit pouvoir finir
THREADS_PATIENCE_S = 5.0        # les serveurs arrêtés rendent leurs threads de travail en quelques dixièmes de seconde
GRACEFUL_SHUTDOWN_S = 3         # comme ``dashboard.server.serve`` : un WebSocket ouvert ne retient pas l'arrêt
API_TIMEOUT_S = 30.0

# Requête annulée par la page elle-même (``AbortController`` au démontage, rechargement,
# téléchargement) : Chromium la signale comme un échec, ce n'en est pas un.
_CANCELLED = "net::ERR_ABORTED"


class BrowserUnavailable(RuntimeError):
    """Ni Microsoft Edge ni le Chromium de Playwright ne peuvent être lancés sur cette machine."""


def launch_browser(playwright: Playwright) -> Browser:
    """Lance Edge sans interface, sinon le Chromium de Playwright ; lève ``BrowserUnavailable`` si aucun ne démarre.

    Edge est présent sur tout Windows récent ; ailleurs, ``playwright install chromium`` suffit.
    """
    failures = []
    for channel in ("msedge", None):
        try:
            return playwright.chromium.launch(channel=channel, headless=True)
        except PlaywrightError as exc:
            failures.append(f"{channel or 'chromium'} : {str(exc).splitlines()[0]}")
    raise BrowserUnavailable(" ; ".join(failures))


class LiveDashboard:
    """Le dashboard d'un laboratoire démarré, servi par uvicorn dans un thread.

    Gestionnaire de contexte : ``with LiveDashboard(runtime) as dashboard`` rend la main une
    fois le serveur à l'écoute (``dashboard.base_url``) et, à la sortie, attend la fin du
    thread et libère le port. Le laboratoire lui-même n'est ni démarré ni arrêté ici.
    """

    def __init__(self, runtime: LabRuntime) -> None:
        self.runtime = runtime
        # Port réservé avant de démarrer uvicorn : il est connu d'emblée et personne ne peut le prendre entre-temps.
        self._listener = socket.create_server((runtime.host, 0))
        self.port: int = self._listener.getsockname()[1]
        self.base_url = f"http://{runtime.host}:{self.port}"
        self._server = uvicorn.Server(uvicorn.Config(
            create_app(runtime),
            log_level="warning",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        ))
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._listener]}, name="frontend-smoke-dashboard", daemon=True
        )

    def __enter__(self) -> "LiveDashboard":
        self._thread.start()
        deadline = time.monotonic() + STARTUP_PATIENCE_S
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self._listener.close()
                raise RuntimeError("Le dashboard de test n’a pas démarré (uvicorn s’est arrêté ou ne répond pas).")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.should_exit = True
        self._thread.join(SHUTDOWN_PATIENCE_S)
        self._listener.close()
        if self._thread.is_alive():
            raise RuntimeError("Le thread du dashboard de test ne s’est pas arrêté.")
        try:
            # Reprendre le port prouve que plus personne n'y écoute.
            socket.create_server((self.runtime.host, self.port)).close()
        except OSError as exc:
            raise RuntimeError(f"Le port {self.port} du dashboard de test n’a pas été libéré.") from exc

    def api(self, method: str, path: str, body: Any = None) -> Any:
        """Appelle l'API du dashboard hors du navigateur et renvoie le JSON décodé (``HTTPError`` si ≥ 400)."""
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
            return json.load(response)

    def wait_until_idle(self) -> None:
        """Attend la fin de la tâche de fond en cours (banc d'essai, scénario de panne), s'il y en a une."""
        deadline = time.monotonic() + IDLE_PATIENCE_S
        while self.api("GET", "/api/jobs")["active"] is not None:
            if time.monotonic() > deadline:
                raise RuntimeError(f"Le laboratoire est encore occupé après {IDLE_PATIENCE_S:.0f} s.")
            time.sleep(0.1)

    def reset(self) -> None:
        """Remet le laboratoire à zéro : stocks, réseau simulé, pannes armées, traces, résultats de scénarios."""
        self.wait_until_idle()
        self.api("POST", "/api/reset")


class PageWatch:
    """Consigne les anomalies d'une page : la liste ``problems`` reste vide tant que tout va bien."""

    def __init__(self, page: Page) -> None:
        self.problems: list[str] = []
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("requestfailed", self._on_request_failed)
        page.on("response", self._on_response)
        page.on("websocket", self._on_websocket)

    def _on_console(self, message: ConsoleMessage) -> None:
        if message.type == "error":
            where = message.location
            origin = f" ({where['url']}:{where['lineNumber']})" if where.get("url") else ""
            self.problems.append(f"console.error : {message.text}{origin}")

    def _on_page_error(self, error: PlaywrightError) -> None:
        # Message et première ligne de la pile : assez pour retrouver le fichier fautif.
        trace = " | ".join(line.strip() for line in (error.stack or str(error)).splitlines()[:2])
        self.problems.append(f"exception JavaScript : {trace}")

    def _on_request_failed(self, request: Request) -> None:
        if request.failure != _CANCELLED:
            self.problems.append(f"requête en échec : {request.method} {request.url} — {request.failure}")

    def _on_response(self, response: Response) -> None:
        if response.status >= 400:
            self.problems.append(f"HTTP {response.status} : {response.request.method} {response.url}")

    def _on_websocket(self, websocket: WebSocket) -> None:
        websocket.on("socketerror", lambda error: self.problems.append(f"WebSocket {websocket.url} : {error}"))

    def report(self) -> str:
        """Les anomalies relevées, une par ligne (chaîne vide s'il n'y en a aucune)."""
        return "\n".join(f"  - {problem}" for problem in self.problems)


def lingering_threads(known: set[threading.Thread]) -> list[str]:
    """Noms des threads absents de ``known`` et toujours vivants après ``THREADS_PATIENCE_S``."""
    deadline = time.monotonic() + THREADS_PATIENCE_S
    while True:
        extra = [thread.name for thread in threading.enumerate() if thread not in known]
        if not extra or time.monotonic() > deadline:
            return extra
        time.sleep(0.05)


__all__ = ["BrowserUnavailable", "LiveDashboard", "PageWatch", "launch_browser", "lingering_threads"]
