"""Serveur de l'API REST de référence : HTTP/1.1 + JSON sur la bibliothèque standard.

Le même service métier que les serveurs RPC, exposé « à la REST » : la procédure
n'est plus nommée dans le message, elle se déduit du verbe et du chemin ; ses
arguments arrivent en trois morceaux (chemin, chaîne de requête, corps JSON) et
tout ce qui passe par l'URL est du texte que le serveur doit retyper.

Deux précautions rendent la comparaison de latence honnête : ``TCP_NODELAY`` et
une seule écriture par réponse (ligne de statut + en-têtes + corps). Sans elles,
l'algorithme de Nagle et les ACK retardés ajoutent ~40 ms à chaque appel.

Lancement autonome : ``python -m rest_api.rest_server``.
"""
from __future__ import annotations

import json
import re
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO, Callable, Iterable, Iterator
from urllib.parse import parse_qsl, unquote

from common.config import HOST, VERSION, default_ports
from common.errors import (
    HTTP_STATUS,
    HTTP_TO_CANONICAL,
    INTERNAL,
    INVALID_ARGUMENT,
    METHOD_NOT_FOUND,
    UNAVAILABLE,
    DomainError,
    InvalidArgument,
)
from common.inventory import InventoryService
from common.telemetry import BUS, EventBus, TraceEvent, new_call_id

from . import (
    CALL_ID_HEADER,
    JSON_MEDIA_TYPE,
    NDJSON_MEDIA_TYPE,
    PROTOCOL,
    REQUEST_LINE,
    STATUS_LINE,
    StageTracer,
    encode_json,
    http_segments,
    json_text,
)

MAX_BODY_BYTES = 1024 * 1024  # taille maximale d'un corps de requête
_ACCEPT_POLL_S = 0.02         # réactivité de stop() : période de la boucle d'acceptation
_INTEGER = re.compile(r"-?[0-9]{1,18}")
_INTERNAL_MESSAGE = "Erreur interne du serveur"


# --- Table de routage --------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Inputs:
    """Les trois endroits où une requête REST transporte ses arguments."""

    path: dict[str, str]
    query: dict[str, str]
    body: Any


@dataclass(frozen=True, slots=True)
class _Route:
    verb: str
    template: str                               # forme lisible, ex. « /api/products/{product_id} »
    pattern: re.Pattern[str]
    procedure: str                              # procédure du service métier visée
    bind: Callable[[_Inputs], dict[str, Any]]   # arguments HTTP → arguments de la procédure
    streaming: bool


class _NoRoute(Exception):
    """Aucune procédure derrière cette requête : chemin inconnu (404) ou verbe refusé (405)."""

    def __init__(self, status: HTTPStatus, message: str, allow: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.allow = allow


def _integer(name: str, text: str) -> int:
    """Dans une URL tout est texte : c'est au serveur de retrouver le type attendu."""
    if _INTEGER.fullmatch(text):
        return int(text)
    raise InvalidArgument(f"« {name} » doit être un entier (reçu : « {text[:40]} »)", param=name)


def _query_integers(query: dict[str, str], *names: str) -> dict[str, int]:
    """Paramètres entiers fournis ; les absents gardent la valeur par défaut du service."""
    return {name: _integer(name, query[name]) for name in names if query.get(name)}


def _bind_factorial(inputs: _Inputs) -> dict[str, Any]:
    return {"n": _integer("n", inputs.path["n"])}


def _bind_product(inputs: _Inputs) -> dict[str, Any]:
    return {"product_id": inputs.path["product_id"]}


def _bind_listing(inputs: _Inputs) -> dict[str, Any]:
    arguments: dict[str, Any] = _query_integers(inputs.query, "limit")
    if "category" in inputs.query:
        arguments["category"] = inputs.query["category"]
    return arguments


def _bind_stock_update(inputs: _Inputs) -> dict[str, Any]:
    body = inputs.body
    if not isinstance(body, dict):
        raise InvalidArgument("Le corps de la requête doit être un objet JSON")
    if "delta" not in body:
        raise InvalidArgument("« delta » est obligatoire dans le corps JSON", param="delta")
    return {
        "product_id": inputs.path["product_id"],
        "delta": body["delta"],
        "idempotency_key": body.get("idempotency_key", ""),
    }


def _bind_analytics(inputs: _Inputs) -> dict[str, Any]:
    return _query_integers(inputs.query, "samples", "interval_ms")


def _bind_nothing(inputs: _Inputs) -> dict[str, Any]:
    return {}


def _route(
    verb: str, template: str, procedure: str, bind: Callable[[_Inputs], dict[str, Any]], *, streaming: bool = False
) -> _Route:
    # Un segment variable peut être vide : c'est alors le service qui refuse l'argument, comme en local.
    pattern = re.compile(re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]*)", template))
    return _Route(verb, template, pattern, procedure, bind, streaming)


_HEALTH = _route("GET", "/api/health", "health", _bind_nothing)

ROUTES: tuple[_Route, ...] = (
    _route("GET", "/api/factorial/{n}", "calculate_factorial", _bind_factorial),
    _route("GET", "/api/products", "list_products", _bind_listing),
    _route("GET", "/api/products/{product_id}", "get_product_details", _bind_product),
    _route("POST", "/api/stock/{product_id}", "update_stock", _bind_stock_update),
    _route("GET", "/api/analytics/stream", "stream_analytics", _bind_analytics, streaming=True),
    _HEALTH,
)


def _resolve(verb: str, path: str) -> tuple[_Route, dict[str, str]]:
    """Retrouve la procédure visée : en REST, son « nom » est le couple verbe + chemin."""
    allowed: list[str] = []
    for route in ROUTES:
        match = route.pattern.fullmatch(path)
        if match is None:
            continue
        if route.verb == verb:
            return route, {name: unquote(value) for name, value in match.groupdict().items()}
        allowed.append(route.verb)
    shown = path[:120]  # on ne renvoie pas au client un chemin démesuré
    if allowed:
        raise _NoRoute(
            HTTPStatus.METHOD_NOT_ALLOWED, f"Verbe {verb} non autorisé sur {shown}", allow=", ".join(allowed)
        )
    raise _NoRoute(HTTPStatus.NOT_FOUND, f"Aucune route pour {verb} {shown}")


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise InvalidArgument("Corps de requête illisible : un document JSON est attendu") from None


def _health() -> dict[str, Any]:
    """Sonde de vivacité : répond sans toucher au service métier."""
    return {"status": "ok", "protocol": PROTOCOL, "version": VERSION}


# --- Squelette HTTP ----------------------------------------------------------

class _HeaderRecorder:
    """Relaie ``readline`` en gardant les octets lus (en-têtes bruts de la requête)."""

    __slots__ = ("stream", "lines")

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.lines: list[bytes] = []

    def readline(self, limit: int = -1) -> bytes:
        line = self.stream.readline(limit)
        self.lines.append(line)
        return line


class _RestRequestHandler(BaseHTTPRequestHandler):
    """Squelette REST : une instance par connexion, une itération par requête."""

    protocol_version = "HTTP/1.1"     # connexions persistantes (keep-alive) par défaut
    disable_nagle_algorithm = True    # TCP_NODELAY sur chaque connexion acceptée
    server: _RestHTTPServer

    _tracing = False
    _raw_head = b""

    def parse_request(self) -> bool:
        """Décide une fois par requête si elle est tracée et, si oui, garde ses en-têtes bruts.

        ``BaseHTTPRequestHandler`` consomme les en-têtes sans les conserver, or
        l'étape ``server.receive`` doit montrer exactement ce qui est arrivé.
        """
        self._tracing = self.server.bus.enabled
        if not self._tracing:
            return super().parse_request()
        recorder = _HeaderRecorder(self.rfile)
        self.rfile = recorder
        try:
            return super().parse_request()
        finally:
            self.rfile = recorder.stream
            self._raw_head = self.raw_requestline + b"".join(recorder.lines)

    def _serve(self) -> None:
        """Déroule une requête : démarshalling, dispatch, exécution, marshalling, envoi."""
        body = self._read_body()
        if body is None:
            return
        trace = StageTracer(self.server.bus, "server") if self._tracing else None
        path, _, query = self.path.partition("?")
        try:
            route, path_arguments = _resolve(self.command, path)
        except _NoRoute as failure:
            if trace:
                self._mark_received(trace, body, "")
            self._send_failure(trace, failure.status, METHOD_NOT_FOUND, failure.message, allow=failure.allow)
            return
        if route is _HEALTH:
            trace = None  # la sonde de vivacité n'est pas un appel RPC : elle ne laisse pas de trace
        if trace:
            self._mark_received(trace, body, route.procedure)
        try:
            inputs = _Inputs(
                path_arguments,
                dict(parse_qsl(query, keep_blank_values=True)),
                _decode_json(body) if route.verb == "POST" else None,
            )
            if trace:
                trace.step("server.unmarshal")
            arguments = route.bind(inputs)
            procedure = _health if route is _HEALTH else getattr(self.server.service, route.procedure)
            if trace:
                trace.step("server.dispatch", detail={
                    "target": f"{type(self.server.service).__name__}.{route.procedure}",
                    "bound_args": arguments,
                    "route": f"{route.verb} {route.template}",
                })
            result = procedure(**arguments)
        except DomainError as exc:
            self._send_failure(trace, HTTP_STATUS.get(exc.code, 500), exc.code, exc.message, exc.data)
            return
        except Exception:  # erreur imprévue : ni pile ni message interne ne doivent fuiter vers le client
            self._send_failure(trace, HTTPStatus.INTERNAL_SERVER_ERROR, INTERNAL, _INTERNAL_MESSAGE)
            return
        if trace:
            trace.step("server.execute")
        if route.streaming:
            self._send_stream(trace, result)
        else:
            self._send_json(trace, HTTPStatus.OK, result)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

    # -- lecture ----------------------------------------------------------------

    def _read_body(self) -> bytes | None:
        """Lit le corps annoncé par ``Content-Length`` ; ``None`` si la requête est inexploitable."""
        if "Transfer-Encoding" in self.headers:
            self._refuse(HTTPStatus.LENGTH_REQUIRED, "Corps fragmenté non pris en charge : Content-Length requis")
            return None
        declared = self.headers.get("Content-Length", "0")
        if not declared.isdecimal() or len(declared) > 9:
            self._refuse(HTTPStatus.BAD_REQUEST, "En-tête Content-Length invalide")
            return None
        length = int(declared)
        if length > MAX_BODY_BYTES:
            self._refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"Corps limité à {MAX_BODY_BYTES} octets")
            return None
        body = self.rfile.read(length) if length else b""
        if len(body) < length:
            self.close_connection = True  # le client a raccroché au milieu de son envoi
            return None
        return body

    def _mark_received(self, trace: StageTracer, body: bytes, method: str) -> None:
        """Publie ``server.receive`` sous l'identifiant du client (en-tête ``X-Call-Id``)."""
        trace.call_id = self.headers.get(CALL_ID_HEADER, "")[:64] or new_call_id(PROTOCOL)
        trace.method = method
        head = self._raw_head
        trace.mark("server.receive", payload=head + body, detail={
            "segments": http_segments(REQUEST_LINE, len(self.raw_requestline), len(head), len(head) + len(body)),
            "http": {"method": self.command, "path": self.path, "status": None},
        })

    # -- écriture ---------------------------------------------------------------

    def _write(
        self, data: bytes, trace: StageTracer | None = None, announced: TraceEvent | None = None
    ) -> bool:
        """Un seul ``sendall`` par message ; ``False`` si le client est déjà parti.

        L'étape d'envoi (``announced``) est publiée juste avant l'écriture : sinon
        le client, déjà réveillé par la réponse, pourrait annoncer sa réception
        avant que ce thread n'ait annoncé l'envoi. Sa durée est complétée ici, et
        un échec d'écriture est signalé après coup.
        """
        try:
            self.wfile.write(data)
        except OSError:
            self.close_connection = True
            if trace:
                trace.step("server.error", detail={
                    "code": UNAVAILABLE,
                    "message": "Client déconnecté : la réponse n'a pas pu lui être remise",
                })
            return False
        if trace:
            trace.settle(announced)
        return True

    def _head(self, status: int, headers: Iterable[tuple[str, str]]) -> bytes:
        lines = [f"HTTP/1.1 {int(status)} {HTTPStatus(status).phrase}", f"Date: {formatdate(usegmt=True)}"]
        lines.extend(f"{name}: {value}" for name, value in headers)
        if self.close_connection:
            lines.append("Connection: close")
        return "\r\n".join(lines).encode("latin-1") + b"\r\n\r\n"

    def _send_json(self, trace: StageTracer | None, status: int, document: Any, *, allow: str = "") -> None:
        """Réponse JSON complète : statut, en-têtes et corps partent dans la même écriture."""
        try:
            body = encode_json(document)
        except (TypeError, ValueError):  # résultat non représentable en JSON : la faute est côté serveur
            self._send_failure(trace, HTTPStatus.INTERNAL_SERVER_ERROR, INTERNAL, _INTERNAL_MESSAGE)
            return
        if trace:
            trace.step("server.marshal", payload=body, detail={"text": json_text(body)})
        headers = [("Content-Type", JSON_MEDIA_TYPE), ("Content-Length", str(len(body)))]
        if allow:
            headers.append(("Allow", allow))
        head = self._head(status, headers)
        message = head if self.command == "HEAD" else head + body
        wire = self._wire_detail(status, head, len(message)) if trace else None
        announced = trace.step("server.send", payload=message, detail=wire) if trace else None
        self._write(message, trace, announced)

    def _send_failure(
        self, trace: StageTracer | None, status: int, code: str, message: str, detail: Any = None, *, allow: str = ""
    ) -> None:
        """Erreur : statut HTTP + corps ``{"error": {"code", "message", "detail"}}`` (plan §8)."""
        if trace:
            trace.step("server.error", detail={"code": code, "message": message, "http_status": int(status)})
        self._send_json(trace, status, {"error": {"code": code, "message": message, "detail": detail}}, allow=allow)

    def _send_stream(self, trace: StageTracer | None, items: Iterator[dict[str, Any]]) -> None:
        """Flux NDJSON : un objet JSON par ligne, une ligne par fragment (*chunk*) HTTP."""
        head = self._head(
            HTTPStatus.OK, (("Content-Type", NDJSON_MEDIA_TYPE), ("Transfer-Encoding", "chunked"))
        )
        wire = self._wire_detail(HTTPStatus.OK, head, len(head)) if trace else None
        announced = trace.step("server.send", payload=head, detail=wire) if trace else None
        if not self._write(head, trace, announced):
            return
        try:
            for seq, item in enumerate(items, start=1):
                line = encode_json(item)
                chunk = b"%x\r\n%b\n\r\n" % (len(line) + 1, line)
                announced = None
                if trace:
                    announced = trace.step(
                        "server.stream_item", payload=line, detail={"seq": seq, "wire_bytes": len(chunk)}
                    )
                if not self._write(chunk, trace, announced):
                    return  # client parti : inutile de produire la suite
            self._write(b"0\r\n\r\n")  # fragment vide : fin de flux
        except Exception:
            # Le statut 200 est déjà parti : seule une coupure peut signaler l'échec au client.
            self.close_connection = True
            if trace:
                trace.step("server.error", detail={
                    "code": INTERNAL, "message": "Flux interrompu par une erreur interne",
                })

    def _wire_detail(self, status: int, head: bytes, total: int) -> dict[str, Any]:
        return {
            "segments": http_segments(STATUS_LINE, head.index(b"\r\n") + 2, len(head), total),
            "http": {"method": self.command, "path": self.path, "status": int(status)},
        }

    # -- refus au niveau HTTP et comportements hérités ---------------------------

    def _refuse(self, status: int, message: str, detail: Any = None) -> None:
        """Refus au niveau HTTP ; le cadrage du flux n'étant plus fiable, la connexion est fermée."""
        self.close_connection = True
        if status == HTTPStatus.NOT_IMPLEMENTED:
            code = METHOD_NOT_FOUND  # verbe HTTP inconnu
        else:
            code = HTTP_TO_CANONICAL.get(status, INVALID_ARGUMENT if status < 500 else INTERNAL)
        self._send_failure(None, status, code, message, detail)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Refus prononcés par ``http.server`` lui-même (syntaxe HTTP, verbe inconnu) : même format JSON."""
        summary = "Requête HTTP invalide" if code < 500 else "Requête HTTP non prise en charge"
        self._refuse(code, summary, {"reason": message} if message else None)

    def log_message(self, format: str, *args: Any) -> None:
        """Pas de journal d'accès : sur stderr, il brouillerait le CLI et fausserait les mesures."""


class _RestHTTPServer(ThreadingHTTPServer):
    """Un thread démon par connexion ; garde la liste des connexions pour pouvoir les couper."""

    daemon_threads = True
    request_queue_size = 128
    # Sous Windows, SO_REUSEADDR laisserait deux serveurs écouter le même port sans erreur.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], service: InventoryService, bus: EventBus) -> None:
        self.service = service
        self.bus = bus
        self.accepted = 0
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        super().__init__(address, _RestRequestHandler)

    def server_bind(self) -> None:
        """Comme ``HTTPServer``, sans son ``getfqdn`` : une résolution DNS inverse peut coûter des secondes."""
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def get_request(self) -> tuple[socket.socket, Any]:
        connection, address = super().get_request()
        with self._connections_lock:
            self._connections.add(connection)
            self.accepted += 1
        return connection, address

    def shutdown_request(self, request: socket.socket) -> None:
        with self._connections_lock:
            self._connections.discard(request)
        super().shutdown_request(request)

    def handle_error(self, request: socket.socket, client_address: Any) -> None:
        """Une connexion qui meurt (client parti, coupure réseau) n'a pas à polluer stderr."""

    @property
    def active(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    def close_connections(self) -> None:
        """Coupe les connexions keep-alive encore ouvertes : chaque thread voit une fin de flux et se termine."""
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # déjà fermée par le client


class RestServerHandle:
    """Poignée du serveur REST : ``start()``, ``stop()``, ``port`` — comme tous les serveurs du laboratoire."""

    def __init__(self, service: InventoryService, host: str = HOST, port: int = 0, *, bus: EventBus = BUS) -> None:
        self.service = service
        self.host = host
        self._port = port
        self._bus = bus
        self._server: _RestHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        """Port réel une fois démarré (utile avec ``port=0``)."""
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self._port}"

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def connections_accepted(self) -> int:
        """Connexions TCP acceptées depuis le dernier démarrage (révèle le keep-alive)."""
        server = self._server
        return server.accepted if server else 0

    @property
    def connections_active(self) -> int:
        server = self._server
        return server.active if server else 0

    def start(self) -> RestServerHandle:
        """Rend la main quand le serveur écoute réellement. Un redémarrage reprend le même port."""
        with self._lock:
            if self._server is not None:
                return self
            try:
                server = _RestHTTPServer((self.host, self._port), self.service, self._bus)
            except OSError as exc:
                raise OSError(
                    exc.errno, f"Serveur REST : impossible d'écouter sur {self.host}:{self._port} ({exc})"
                ) from exc
            self._port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": _ACCEPT_POLL_S},
                name=f"rest-server-{self._port}",
                daemon=True,
            )
            thread.start()
            self._server, self._thread = server, thread
        return self

    def stop(self) -> None:
        """Arrête d'accepter, libère le port et coupe les connexions ouvertes. Idempotent."""
        with self._lock:
            server, thread = self._server, self._thread
            self._server = self._thread = None
        if server is None or thread is None:
            return
        server.shutdown()
        server.server_close()
        server.close_connections()
        thread.join(timeout=2.0)

    def __enter__(self) -> RestServerHandle:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def create_rest_server(
    service: InventoryService, host: str = HOST, port: int = 0, *, bus: EventBus = BUS
) -> RestServerHandle:
    """Prépare le serveur REST de ``service`` ; il n'écoute qu'après ``start()``."""
    return RestServerHandle(service, host, port, bus=bus)


def main() -> None:
    """Sert l'API sur le port par défaut jusqu'à Ctrl+C."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # accents intacts sous Windows, même sortie redirigée
    try:
        handle = create_rest_server(InventoryService(), port=default_ports().rest).start()
    except OSError as exc:
        raise SystemExit(exc.strerror) from None  # port occupé : un message clair plutôt qu'une pile d'appels
    routes = "\n".join(f"  {route.verb:<5}{route.template}" for route in ROUTES)
    # flush : la bannière doit apparaître tout de suite, même quand la sortie est redirigée.
    print(f"API REST de référence à l'écoute sur {handle.url}  (Ctrl+C pour arrêter)\n{routes}", flush=True)
    try:
        while True:  # pas de signal.pause() sous Windows ; sleep reste interruptible par Ctrl+C
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nArrêt du serveur REST.")
    finally:
        handle.stop()


if __name__ == "__main__":
    main()
