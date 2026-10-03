"""Squelette serveur (« skeleton ») : la moitié serveur du middleware RPC.

Le stub client transforme un appel de fonction en octets ; le squelette fait le
chemin inverse, puis renvoie le résultat par le même procédé ::

    ④ réception      lire une trame complète sur la connexion
    ⑤ démarshalling  octets → requête JSON-RPC (nom de procédure + paramètres)
    ⑥ dispatch       nom → procédure enregistrée ; paramètres → arguments Python
    ⑦ exécution      appel de la vraie procédure — qui ignore tout du réseau
    ⑧ marshalling    résultat ou exception → réponse JSON-RPC → octets
    ⑨ envoi          écrire la trame de réponse

(Les numéros sont ceux du pipeline de ``common.telemetry`` ; ①–③ et ⑩–⑫ se
déroulent dans ``client_stub.py``.)

Concurrence : un thread lecteur par connexion lit les trames et confie chacune
à un pool de workers. Plusieurs requêtes d'une même connexion s'exécutent donc
EN PARALLÈLE, et leurs réponses repartent dans l'ordre où elles sont prêtes :
c'est l'``id`` qui permet au client de s'y retrouver. Les éléments d'un lot,
eux, sont exécutés dans l'ordre du lot.
"""
from __future__ import annotations

import inspect
import re
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

from common import errors
from common.config import HOST, MAX_PAYLOAD_CAPTURE
from common.errors import RpcProtocolError
from common.telemetry import BUS, EventBus, TraceEvent

from . import protocol as wire
from .protocol import ErrorObject, Request, Response

MAX_WORKERS = 32
_JOIN_TIMEOUT_S = 2.0
_DRAIN_POLL_S = 0.05
_STREAM_ANNOTATION = re.compile(r"\b(Iterator|Generator)\b")


@dataclass(frozen=True, slots=True)
class Procedure:
    """Une procédure exposée : la fonction, et sa signature pour lui lier des paramètres."""

    name: str
    func: Callable[..., Any]
    signature: inspect.Signature
    streaming: bool

    @classmethod
    def of(cls, name: str, func: Callable[..., Any]) -> "Procedure":
        signature = inspect.signature(func)
        streaming = inspect.isgeneratorfunction(func) or bool(
            _STREAM_ANNOTATION.search(str(signature.return_annotation))
        )
        return cls(name, func, signature, streaming)

    @property
    def target(self) -> str:
        """Nom qualifié de la fonction réellement appelée (affiché à l'étape de dispatch)."""
        qualname = getattr(self.func, "__qualname__", self.name)
        module = getattr(self.func, "__module__", None)
        return f"{module}.{qualname}" if module else qualname

    def describe(self) -> dict[str, Any]:
        """Description renvoyée par ``rpc.discover`` : le seul « contrat » de ce middleware."""
        return {
            "name": self.name,
            "params": [_describe_param(param) for param in self.signature.parameters.values()],
            "doc": inspect.getdoc(self.func) or "",
            "streaming": self.streaming,
        }


def _describe_param(param: inspect.Parameter) -> dict[str, Any]:
    variadic = param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
    required = param.default is param.empty and not variadic
    default = None if param.default is param.empty else param.default
    if not isinstance(default, (str, int, float, bool, type(None))):
        default = repr(default)
    annotation = "" if param.annotation is param.empty else param.annotation
    if not isinstance(annotation, str):
        annotation = getattr(annotation, "__name__", str(annotation))
    return {
        "name": param.name,
        "kind": param.kind.name,
        "default": default,
        "annotation": annotation,
        "required": required,
    }


class _RequestFault(Exception):
    """La requête ne peut pas être servie : procédure inconnue ou paramètres invalides."""

    def __init__(self, code: int, message: str, canonical: str) -> None:
        super().__init__(message)
        self.error = ErrorObject(code, message, {"canonical": canonical})


class _Connection:
    """Une connexion cliente : sa socket, son verrou d'écriture, ses requêtes en cours."""

    __slots__ = ("sock", "write_lock", "in_flight", "reader", "closed")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.write_lock = threading.Lock()
        self.in_flight: set[Future] = set()
        self.reader: threading.Thread | None = None
        self.closed = False         # fermée par le serveur (stop), et non par le client

    def send(self, data: bytes) -> None:
        # Plusieurs workers répondent sur la même connexion : sans ce verrou,
        # deux trames pourraient s'entrelacer et le flux deviendrait illisible.
        with self.write_lock:
            self.sock.sendall(data)

    def close(self) -> None:
        self.closed = True
        _close_socket(self.sock)


class RpcServerSkeleton:
    """Serveur JSON-RPC 2.0 sur TCP : registre de procédures + dispatcher."""

    def __init__(self, host: str = HOST, port: int = 0, *, name: str = "custom", bus: EventBus = BUS) -> None:
        self.host = host
        self.port = port            # port demandé ; après start(), port réellement écouté
        self.name = name
        self._bus = bus
        self._procedures: dict[str, Procedure] = {
            wire.DISCOVER_METHOD: Procedure.of(wire.DISCOVER_METHOD, self._discover),
        }
        self._state_lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._acceptor: threading.Thread | None = None
        self._workers: ThreadPoolExecutor | None = None
        self._connections: set[_Connection] = set()

    # -- registre des procédures ------------------------------------------------

    def register(self, func: Callable[..., Any], name: str | None = None) -> Callable[..., Any]:
        """Expose ``func`` sous ``name`` (par défaut : son propre nom) ; renvoie ``func``."""
        name = name or func.__name__
        if name.startswith(wire.RESERVED_PREFIX):
            raise ValueError(f"Le préfixe « {wire.RESERVED_PREFIX} » est réservé au protocole : {name}")
        self._procedures[name] = Procedure.of(name, func)
        return func

    def expose(self, func: Callable[..., Any] | None = None, *, name: str | None = None) -> Callable[..., Any]:
        """Décorateur : ``@server.expose`` ou ``@server.expose(name="alias")``."""
        if func is None:
            return lambda target: self.register(target, name)
        return self.register(func, name)

    def register_instance(self, obj: object, names: Iterable[str] | None = None) -> None:
        """Expose les méthodes publiques de ``obj``, ou seulement celles listées dans ``names``."""
        if names is None:
            names = [name for name, _ in inspect.getmembers(obj, inspect.isroutine) if not name.startswith("_")]
        for name in names:
            self.register(getattr(obj, name), name)

    def methods(self) -> list[dict[str, Any]]:
        """Description des procédures exposées (hors extensions ``rpc.*``)."""
        return [
            procedure.describe()
            for name, procedure in self._procedures.items()
            if not name.startswith(wire.RESERVED_PREFIX)
        ]

    def _discover(self) -> dict[str, Any]:
        """Introspection : liste les procédures exposées et leurs paramètres."""
        return {"methods": self.methods()}

    # -- cycle de vie -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._listener is not None

    def start(self) -> "RpcServerSkeleton":
        """Ouvre le port d'écoute ; au retour, le serveur accepte réellement les connexions."""
        with self._state_lock:
            if self._listener is not None:
                return self
            try:
                listener = socket.create_server((self.host, self.port))
            except OSError as exc:
                reason = exc.strerror or exc
                raise OSError(
                    exc.errno, f"Impossible d'écouter sur {self.host}:{self.port} ({self.name}) : {reason}"
                ) from exc
            self.port = listener.getsockname()[1]
            workers = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix=f"{self.name}-worker")
            acceptor = threading.Thread(
                target=self._accept_loop, args=(listener, workers), name=f"{self.name}-accept", daemon=True
            )
            self._listener, self._workers, self._acceptor = listener, workers, acceptor
            acceptor.start()
        return self

    def stop(self) -> None:
        """Ferme l'écoute ET toutes les connexions ouvertes ; idempotent, le port est libéré au retour."""
        with self._state_lock:
            listener, self._listener = self._listener, None
            acceptor, self._acceptor = self._acceptor, None
            workers, self._workers = self._workers, None
            connections = list(self._connections)
            self._connections.clear()
        if listener is None or acceptor is None or workers is None:
            return
        _close_socket(listener)
        for conn in connections:
            conn.close()
        # Les procédures en cours se terminent seules ; leur réponse sera perdue (socket fermée).
        workers.shutdown(wait=False, cancel_futures=True)
        acceptor.join(_JOIN_TIMEOUT_S)
        for conn in connections:
            if conn.reader is not None:
                conn.reader.join(_JOIN_TIMEOUT_S)

    def __enter__(self) -> "RpcServerSkeleton":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- transport : connexions et trames -------------------------------------------

    def _accept_loop(self, listener: socket.socket, workers: ThreadPoolExecutor) -> None:
        """Thread d'écoute : un thread lecteur par connexion acceptée."""
        while True:
            try:
                sock, _ = listener.accept()
            except OSError:
                if self._listener is not listener:
                    return          # écouteur fermé par stop()
                continue            # connexion avortée avant d'être acceptée : on continue d'écouter
            # Sans TCP_NODELAY, l'algorithme de Nagle retient les petites trames (~40 ms par appel).
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = _Connection(sock)
            conn.reader = threading.Thread(
                target=self._read_loop, args=(conn, workers), name=f"{self.name}-conn", daemon=True
            )
            with self._state_lock:
                if self._listener is not listener:
                    conn.close()    # arrêt demandé pendant l'accept
                    return
                self._connections.add(conn)
            conn.reader.start()

    def _read_loop(self, conn: _Connection, workers: ThreadPoolExecutor) -> None:
        """Thread lecteur d'une connexion : ④ lit les trames, une par une, tant que le client reste connecté."""
        try:
            while (body := wire.read_frame(conn.sock)) is not None:
                # Le lecteur ne traite rien lui-même : il retourne aussitôt lire la
                # trame suivante, pendant qu'un worker exécute celle-ci.
                task = workers.submit(self._process_frame, conn, body)
                conn.in_flight.add(task)
                task.add_done_callback(conn.in_flight.discard)
            # Le client a fini d'émettre : ses requêtes en cours se terminent avant qu'on
            # raccroche — sauf si stop() ferme la connexion entre-temps.
            while not conn.closed and wait(tuple(conn.in_flight), timeout=_DRAIN_POLL_S).not_done:
                pass
        except RpcProtocolError as exc:
            # Trame inexploitable (trop grande, tronquée) : le flux est désynchronisé,
            # on prévient le client puis on raccroche.
            self._reject(conn, exc)
        except (OSError, RuntimeError):
            pass                    # connexion coupée, ou pool de workers arrêté par stop()
        finally:
            with self._state_lock:
                self._connections.discard(conn)
            conn.close()

    def _reject(self, conn: _Connection, cause: RpcProtocolError) -> None:
        reply = _error_response(None, wire.INVALID_REQUEST, cause.message, errors.PROTOCOL)
        try:
            conn.send(wire.frame(wire.encode_response(reply)))
        except OSError:
            pass

    # -- traitement d'une trame (dans un worker) --------------------------------------

    def _process_frame(self, conn: _Connection, body: bytes) -> None:
        """Traite une trame reçue : une requête isolée ou un lot."""
        traced = self._bus.enabled      # testé une fois par trame : bus coupé, rien n'est mesuré ni construit
        started = time.perf_counter_ns() if traced else 0

        # ⑤ Démarshalling : octets → requêtes. Un message illisible ou hors norme
        # devient directement sa réponse d'erreur (id null : on ne sait pas à qui répondre).
        entries: list[Request | Response]
        try:
            document = wire.parse_document(body)
        except RpcProtocolError as exc:
            is_batch = False
            entries = [_error_response(None, wire.PARSE_ERROR, exc.message, errors.PROTOCOL)]
        else:
            is_batch = isinstance(document, list) and len(document) > 0
            entries = [_to_request(item) for item in (document if is_batch else [document])]

        call_id = method = ""
        if traced:
            unmarshal_us = _elapsed_us(started)
            call_id, method = _trace_anchor(entries)
            received = wire.frame(body)
            self._emit("server.receive", call_id, method, payload=received, detail=wire.describe_frame(received))
            self._emit("server.unmarshal", call_id, method, duration_us=unmarshal_us,
                       detail={"batch": len(entries)} if is_batch else None)

        # ⑥ ⑦ ⑧ Chaque requête est servie ; une notification ne produit aucune réponse.
        replies: list[bytes] = []
        for entry in entries:
            if isinstance(entry, Request):
                reply = self._serve(conn, entry, traced)
            else:
                reply = self._refuse(entry, traced)
            if reply is not None:
                replies.append(reply)
        if not replies:
            return

        # ⑨ Envoi : une réponse seule, ou le tableau des réponses du lot.
        try:
            payload = wire.join_batch(replies) if is_batch else replies[0]
        except RpcProtocolError as exc:     # réponses du lot trop volumineuses pour une seule trame
            payload = self._refuse(Response(None, error=wire.exception_to_error(exc)), traced)
        self._send(conn, payload, traced, call_id, method)

    def _serve(self, conn: _Connection, request: Request, traced: bool) -> bytes | None:
        """Sert UNE requête ; renvoie sa réponse sérialisée, ou ``None`` pour une notification."""
        call_id, method = _call_id(request), request.method
        result: Any = None
        error: ErrorObject | None = None
        cause = ""
        try:
            # ⑥ Dispatch : retrouver la procédure, lui lier les paramètres reçus.
            started = time.perf_counter_ns() if traced else 0
            procedure, arguments = self._dispatch(request)
            if traced:
                self._emit("server.dispatch", call_id, method, duration_us=_elapsed_us(started),
                           detail={"target": procedure.target, "bound_args": dict(arguments.arguments)})

            # ⑦ Exécution : le seul moment où le code métier tourne.
            started = time.perf_counter_ns() if traced else 0
            try:
                result = procedure.func(*arguments.args, **arguments.kwargs)
                if inspect.isgenerator(result):
                    result = self._stream(conn, request, result, traced)
            finally:
                if traced:
                    self._emit("server.execute", call_id, method, duration_us=_elapsed_us(started))
        except _RequestFault as fault:
            error = fault.error
        except Exception as exc:    # une procédure peut lever n'importe quoi : le serveur doit survivre et répondre
            error = wire.exception_to_error(exc)
            cause = f"{type(exc).__name__}: {exc}"
        if error is not None and traced:
            # La cause exacte reste dans la trace du serveur ; le client, lui, ne reçoit que ``error``.
            self._emit("server.error", call_id, method, detail=_error_detail(error, cause))

        if request.is_notification:
            return None             # JSON-RPC : jamais de réponse à une notification, même en cas d'erreur
        # ⑧ Marshalling du résultat ou de l'erreur.
        return self._marshal(Response(request.id, result, error), traced, call_id, method)

    def _dispatch(self, request: Request) -> tuple[Procedure, inspect.BoundArguments]:
        procedure = self._procedures.get(request.method)
        if procedure is None:
            raise _RequestFault(
                wire.METHOD_NOT_FOUND, f"Procédure inconnue : {request.method}", errors.METHOD_NOT_FOUND
            )
        # JSON n'a pas de signature : c'est Python qui vérifie, à l'exécution, que
        # les paramètres reçus correspondent à ceux de la procédure.
        params = request.params
        try:
            if isinstance(params, dict):
                arguments = procedure.signature.bind(**params)
            else:
                arguments = procedure.signature.bind(*(params or ()))
        except TypeError as exc:
            reason = _explain_mismatch(procedure.signature, params, exc)
            raise _RequestFault(
                wire.INVALID_PARAMS, f"Paramètres invalides pour {request.method} : {reason}", errors.INVALID_ARGUMENT
            ) from None
        return procedure, arguments

    def _stream(self, conn: _Connection, request: Request, generator: Iterator[Any], traced: bool) -> dict[str, Any]:
        """Extension « flux serveur » : un générateur devient une suite de trames.

        Chaque élément part, dès qu'il est produit, dans une notification
        ``rpc.stream.item`` portant l'``id`` de la requête ; le résultat renvoyé
        ici forme la réponse finale, qui clôt le flux.
        """
        count = 0
        try:
            if not request.is_notification:     # sans id, rien ne rattacherait les éléments à un appel
                for count, item in enumerate(generator, start=1):
                    body = wire.encode_request(wire.stream_item(request.id, count, item))
                    if traced:
                        self._emit("server.stream_item", _call_id(request), request.method, payload=body,
                                   detail={"seq": count})
                    conn.send(wire.frame(body))
        finally:
            generator.close()
        return wire.stream_end(count)

    def _refuse(self, reply: Response, traced: bool) -> bytes:
        """Élément illisible ou hors norme : sa réponse d'erreur a été préparée au démarshalling."""
        if traced and reply.error is not None:
            self._emit("server.error", "", "", detail=_error_detail(reply.error))
        return self._marshal(reply, traced, "", "")

    def _marshal(self, reply: Response, traced: bool, call_id: str, method: str) -> bytes:
        started = time.perf_counter_ns() if traced else 0
        try:
            body = wire.encode_response(reply)
        except RpcProtocolError as exc:
            # Résultat non sérialisable ou trop gros : le client reçoit tout de même une réponse.
            body = wire.encode_response(Response(reply.id, error=wire.exception_to_error(exc)))
        if traced:
            self._emit("server.marshal", call_id, method, duration_us=_elapsed_us(started), payload=body,
                       detail={"text": body[:MAX_PAYLOAD_CAPTURE].decode("utf-8", "replace")})
        return body

    def _send(self, conn: _Connection, payload: bytes, traced: bool, call_id: str, method: str) -> None:
        started = time.perf_counter_ns() if traced else 0
        data = wire.frame(payload)
        # Publié AVANT l'écriture, avec la durée du tramage : dès que les octets partent,
        # le client peut publier sa réception, et la chronologie affichée doit rester
        # causale. La durée est complétée (tramage + écriture) au retour de sendall.
        event = None
        if traced:
            event = self._emit("server.send", call_id, method, duration_us=_elapsed_us(started), payload=data,
                               detail=wire.describe_frame(data))
        try:
            conn.send(data)
        except OSError:
            return                  # le client est parti : plus personne à qui répondre
        if event is not None:
            event.duration_us = _elapsed_us(started)

    def _emit(self, stage: str, call_id: str, method: str, **fields: Any) -> TraceEvent | None:
        return self._bus.emit(
            call_id=call_id, protocol=self.name, side="server", stage=stage, method=method, **fields
        )


def _to_request(document: Any) -> Request | Response:
    """Valide un élément reçu ; un élément invalide devient sa propre réponse d'erreur."""
    try:
        message = wire.build_message(document)
    except RpcProtocolError as exc:
        return _error_response(None, wire.INVALID_REQUEST, exc.message, errors.PROTOCOL)
    if isinstance(message, Response):
        return _error_response(
            None, wire.INVALID_REQUEST, "Message JSON-RPC invalide : une requête était attendue", errors.PROTOCOL
        )
    return message


def _explain_mismatch(signature: inspect.Signature, params: wire.Params, cause: TypeError) -> str:
    """Dit pourquoi ``params`` ne correspond pas à ``signature`` (``bind`` vient de le refuser)."""
    parameters = list(signature.parameters.values())
    kinds = {param.kind for param in parameters}
    if isinstance(params, dict):
        accepted = {p.name for p in parameters if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
        unknown = [name for name in params if name not in accepted]
        if unknown and inspect.Parameter.VAR_KEYWORD not in kinds:
            return "paramètre inconnu " + ", ".join(f"« {name} »" for name in unknown)
        supplied = set(params)
    else:
        positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        count = len(params or ())
        if count > len(positional) and inspect.Parameter.VAR_POSITIONAL not in kinds:
            return f"{count} paramètres reçus, {len(positional)} acceptés au plus"
        supplied = {p.name for p in positional[:count]}
    missing = [
        p.name for p in parameters
        if p.default is p.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD) and p.name not in supplied
    ]
    if missing:
        return "paramètre obligatoire manquant " + ", ".join(f"« {name} »" for name in missing)
    return str(cause)


def _error_response(request_id: wire.RequestId, code: int, message: str, canonical: str) -> Response:
    return Response(request_id, error=ErrorObject(code, message, {"canonical": canonical}))


def _error_detail(error: ErrorObject, cause: str = "") -> dict[str, Any]:
    detail = {"code": error.data["canonical"], "jsonrpc_code": error.code, "message": error.message}
    if cause:
        detail["cause"] = cause
    return detail


def _call_id(request: Request) -> str:
    """Identifiant de corrélation des traces : l'``id`` choisi par le client (vide pour une notification)."""
    return "" if request.is_notification or request.id is None else str(request.id)


def _trace_anchor(entries: list[Request | Response]) -> tuple[str, str]:
    """Appel auquel rattacher les évènements d'une trame : la première requête portant un ``id``."""
    requests = [entry for entry in entries if isinstance(entry, Request)]
    anchor = next((request for request in requests if _call_id(request)), requests[0] if requests else None)
    return (_call_id(anchor), anchor.method) if anchor is not None else ("", "")


def _elapsed_us(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1000


def _close_socket(sock: socket.socket) -> None:
    # shutdown() réveille un thread bloqué dans accept() ou recv() ; close() seul n'y suffit pas partout.
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()
