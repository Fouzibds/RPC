"""Stub client : la moitié cliente du middleware RPC.

Le stub donne l'illusion d'un appel local. ``stub.update_stock("SKU-1001", -3)``
ne calcule rien : il transforme l'appel en message, l'envoie, attend la réponse
et la retransforme en valeur Python — ou en exception ::

    ① appel          le code applicatif appelle une méthode du stub
    ② marshalling    nom + arguments → requête JSON-RPC 2.0 → octets
    ③ envoi          tramage (longueur + message), puis un seul ``sendall``
         … le serveur travaille : étapes ④ à ⑨, voir server_skeleton.py …
    ⑩ réception      le thread lecteur lit une trame de réponse
    ⑪ démarshalling  octets → réponse, rattachée à son appel grâce à l'``id``
    ⑫ retour         le résultat est rendu à l'appelant, ou l'erreur est levée

Multiplexage : une seule connexion TCP et un thread lecteur. Chaque appel émis
est inscrit dans une table ``id → appel en attente`` où le lecteur retrouve le
destinataire de chaque réponse. Plusieurs appels peuvent donc être « en vol »
en même temps sur la même connexion (pipelining), et leurs réponses revenir
dans le désordre.

Ce qu'un appel distant ajoute à un appel local, et que le stub ne peut pas
cacher : pas de réponse à temps → ``RpcTimeoutError`` ; connexion refusée ou
coupée → ``RpcTransportError`` ; réponse illisible → ``RpcProtocolError`` ;
erreur renvoyée par le serveur → sous-classe de ``RpcRemoteError``.
"""
from __future__ import annotations

import functools
import json
import math
import queue
import socket
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, NamedTuple, Sequence

from common.config import CONNECT_TIMEOUT_S, DEFAULT_TIMEOUT_S, MAX_PAYLOAD_CAPTURE
from common.errors import RpcError, RpcProtocolError, RpcTimeoutError, RpcTransportError
from common.telemetry import BUS, EventBus, TraceEvent, new_call_id

from . import protocol as wire
from .protocol import Request, Response

_JOIN_TIMEOUT_S = 2.0
_PREVIEW_CHARS = 240
_END_OF_STREAM = object()


class RpcFuture(Future):
    """Résultat à venir d'un appel distant, étiqueté par son identifiant de corrélation."""

    def __init__(self, call_id: str, method: str) -> None:
        super().__init__()
        self.call_id = call_id
        self.method = method
        # La requête part aussitôt : côté client, il n'y a plus rien à annuler.
        self.set_running_or_notify_cancel()


@dataclass(slots=True)
class _PendingCall:
    """Un appel émis dont la réponse n'est pas encore arrivée."""

    future: RpcFuture
    timeout: float
    deadline: float                         # instant perf_counter ; repoussé à chaque élément d'un flux
    traced: bool
    items: queue.SimpleQueue | None = None  # appel en flux : éléments reçus, en attente de consommation
    started_ns: int = 0
    sent_ns: int = 0


class _Connection:
    """Une connexion TCP vers le serveur, et le thread qui la lit."""

    __slots__ = ("sock", "write_lock", "reader")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.write_lock = threading.Lock()
        self.reader: threading.Thread | None = None


class _Arrival(NamedTuple):
    """Mesures prises par le lecteur à l'arrivée d'une trame (pour la télémétrie)."""

    body: bytes
    received_ns: int
    unmarshal_us: float


class RpcClientStub:
    """Client JSON-RPC 2.0 sur TCP, utilisable depuis plusieurs threads.

    Toute méthode publique inconnue du stub est une procédure distante
    (``stub.update_stock(...)``) ; pour un nom qui se heurterait à l'API du stub
    (``call``, ``close``…) ou à un mot-clé ``timeout``, passer par ``call()``.

    ``last_call_id`` est l'identifiant du dernier appel émis ; entre plusieurs
    threads, préférer ``RpcFuture.call_id``, propre à chaque appel.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        bus: EventBus = BUS,
        protocol: str = "custom",
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.protocol = protocol
        self.last_call_id = ""
        self._bus = bus
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._conn: _Connection | None = None
        self._pending: dict[wire.RequestId, _PendingCall] = {}
        self._watchdog: threading.Thread | None = None
        self._next_wakeup = math.inf

    # -- API publique -------------------------------------------------------------

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """Proxy dynamique : ``stub.<procédure>(...)`` équivaut à ``stub.call("<procédure>", ...)``."""
        if name.startswith("_"):
            # Attributs privés et protocoles Python (copy, pickle…) : jamais d'appel distant.
            raise AttributeError(name)
        return functools.partial(self.call, name)

    def call(self, method: str, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        """Appel synchrone : bloque jusqu'à la réponse, l'échéance ou la perte de la connexion."""
        return self.call_async(method, *args, timeout=timeout, **kwargs).result()

    def call_async(self, method: str, *args: Any, timeout: float | None = None, **kwargs: Any) -> RpcFuture:
        """Appel asynchrone : émet la requête et rend aussitôt la main.

        Toute erreur d'appel distant, y compris une connexion refusée, est remise par le ``Future``.
        """
        return self._start(method, args, kwargs, timeout).future

    def stream(self, method: str, *args: Any, timeout: float | None = None, **kwargs: Any) -> Iterator[Any]:
        """Appelle une procédure en flux : la requête part maintenant, les éléments se consomment à l'itération.

        ``timeout`` borne l'attente ENTRE deux éléments. Abandonner l'itération
        n'arrête pas le serveur : ce protocole minimal n'a pas de message d'annulation.
        """
        items: queue.SimpleQueue = queue.SimpleQueue()
        return self._consume(self._start(method, args, kwargs, timeout, items), items)

    def notify(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Notification : requête sans ``id``. Le serveur l'exécute mais ne répond jamais.

        Le retour signifie seulement que les octets sont partis : ni résultat,
        ni erreur, ni accusé de réception.
        """
        params = _to_params(args, kwargs)
        traced = self._bus.enabled
        # Identifiant de trace purement local : rien de tel ne voyage sur le fil.
        call_id = self.last_call_id = new_call_id(self.protocol)
        started = time.perf_counter_ns() if traced else 0
        if traced:
            self._emit("client.call", call_id, method,
                       detail={"args": list(args), "kwargs": dict(kwargs), "notification": True})
        try:
            self._transmit(Request(method, params, is_notification=True), (), call_id, method, traced)
        except RpcError as error:
            if traced:
                self._emit("client.error", call_id, method, duration_us=_elapsed_us(started),
                           detail={"code": error.code, "message": error.message})
            raise
        if traced:
            self._emit("client.return", call_id, method, duration_us=_elapsed_us(started),
                       detail={"result_preview": None, "notification": True})

    def batch(self, calls: Iterable[tuple[str, Any]], *, timeout: float | None = None) -> list[Any]:
        """Lot : N appels ``(méthode, paramètres)`` dans UNE trame, N réponses dans une trame.

        Les résultats sont rendus dans l'ordre des appels. Un appel en échec
        est rendu sous la forme de son ``RpcError`` (non levée) : un échec ne
        doit pas faire perdre les résultats des autres.
        """
        traced = self._bus.enabled
        pending: list[_PendingCall] = []
        requests: list[Request] = []
        for method, params in calls:
            if params is not None and not isinstance(params, dict):
                params = list(params)
            call = self._new_call(method, timeout, traced)
            if traced:
                self._emit("client.call", call.future.call_id, method, detail={"params": params, "batch": True})
            pending.append(call)
            requests.append(Request(method, params, call.future.call_id))
        if not pending:
            return []               # JSON-RPC interdit le lot vide : rien à envoyer
        self._launch(pending, requests)
        results: list[Any] = []
        for call in pending:
            try:
                results.append(call.future.result())
            except RpcError as error:
                results.append(error)
        return results

    def discover(self) -> dict[str, Any]:
        """Interroge l'extension ``rpc.discover`` : procédures exposées et leurs paramètres."""
        return self.call(wire.DISCOVER_METHOD)

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def connect(self) -> None:
        """Ouvre la connexion dès maintenant (sinon elle l'est au premier appel)."""
        with self._lock:
            if self._conn is None:
                self._open_connection()

    def close(self) -> None:
        """Ferme la connexion ; les appels encore en attente échouent.

        Le stub reste utilisable : l'appel suivant rouvrira une connexion.
        """
        with self._lock:
            conn, watchdog = self._conn, self._watchdog
            self._watchdog = None
            self._wakeup.notify_all()
        threads = [watchdog]
        if conn is not None:
            self._drop_connection(conn, RpcTransportError, "Stub fermé avant l’arrivée de la réponse")
            threads.append(conn.reader)
        for thread in threads:
            if thread is not None and thread is not threading.current_thread():
                thread.join(_JOIN_TIMEOUT_S)

    def __enter__(self) -> "RpcClientStub":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- aller : ① appel, ② marshalling, ③ envoi --------------------------------------

    def _start(
        self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any], timeout: float | None,
        items: queue.SimpleQueue | None = None,
    ) -> _PendingCall:
        """Émet une requête et renvoie l'appel en attente, sans bloquer sur la réponse."""
        params = _to_params(args, kwargs)
        traced = self._bus.enabled      # testé une fois par appel : bus coupé, rien n'est mesuré ni construit
        call = self._new_call(method, timeout, traced, items)
        if traced:
            self._emit("client.call", call.future.call_id, method, detail={"args": list(args), "kwargs": dict(kwargs)})
        self._launch([call], Request(method, params, call.future.call_id))
        return call

    def _new_call(
        self, method: str, timeout: float | None, traced: bool, items: queue.SimpleQueue | None = None
    ) -> _PendingCall:
        timeout = self.timeout if timeout is None else timeout
        # L'identifiant de corrélation EST l'« id » JSON-RPC : le serveur publiera ses traces sous le même.
        call_id = self.last_call_id = new_call_id(self.protocol)
        started_ns = time.perf_counter_ns() if traced else 0
        return _PendingCall(
            future=RpcFuture(call_id, method),
            timeout=timeout,
            deadline=time.perf_counter() + timeout,
            traced=traced,
            items=items,
            started_ns=started_ns,
        )

    def _launch(self, calls: list[_PendingCall], message: Request | list[Request]) -> None:
        """Envoie le message portant ``calls`` ; un échec avant l'envoi est remis à chaque appel."""
        head = calls[0]
        try:
            self._transmit(message, calls, head.future.call_id, head.future.method, head.traced)
        except RpcError as error:
            # Marshalling ou connexion impossible : ces appels n'ont jamais été inscrits,
            # personne d'autre ne les terminera.
            for call in calls:
                self._fail(call, self._error(type(error), error.message, call))

    def _transmit(
        self, message: Request | list[Request], calls: Sequence[_PendingCall], call_id: str, method: str, traced: bool
    ) -> None:
        """② marshalling puis ③ envoi d'un message ; ``calls`` sont les appels qui en attendent la réponse."""
        # ② Marshalling : l'appel devient un texte JSON, puis des octets.
        started = time.perf_counter_ns() if traced else 0
        body = wire.encode_batch(message) if isinstance(message, list) else wire.encode_request(message)
        if traced:
            self._emit("client.marshal", call_id, method, duration_us=_elapsed_us(started), payload=body,
                       detail={"text": body[:MAX_PAYLOAD_CAPTURE].decode("utf-8", "replace")})

        # ③ Envoi : inscription des appels en attente, tramage, écriture.
        conn = self._register(calls)
        if traced:
            sent_ns = time.perf_counter_ns()
            for call in calls:
                call.sent_ns = sent_ns
        try:
            self._write(conn, body, call_id, method, traced)
        except RpcTransportError:
            if not calls:
                raise               # notification : cet échec ne sera signalé nulle part ailleurs
            # Sinon l'abandon de la connexion a déjà fait échouer tous les appels inscrits.

    def _register(self, calls: Sequence[_PendingCall]) -> _Connection:
        """Inscrit les appels AVANT l'écriture : la réponse peut arriver avant le retour de ``sendall``."""
        with self._lock:
            # Connexion paresseuse : ouverte au premier appel, rouverte après une coupure.
            conn = self._conn or self._open_connection()
            for call in calls:
                self._pending[call.future.call_id] = call
            if calls:
                self._arm_watchdog(min(call.deadline for call in calls))
        return conn

    def _write(self, conn: _Connection, body: bytes, call_id: str, method: str, traced: bool) -> None:
        started = time.perf_counter_ns() if traced else 0
        data = wire.frame(body)
        # Publié AVANT l'écriture, avec la durée du tramage : dès que les octets partent,
        # le serveur peut publier sa réception, et la chronologie affichée doit rester
        # causale. La durée est complétée (tramage + écriture) au retour de sendall.
        event = None
        if traced:
            event = self._emit("client.send", call_id, method, duration_us=_elapsed_us(started), payload=data,
                               detail=wire.describe_frame(data))
        try:
            with conn.write_lock:
                # Un seul sendall par message : en-tête et corps partent dans le même segment TCP.
                conn.sock.sendall(data)
        except OSError as exc:
            message = f"Envoi impossible vers {self.host}:{self.port} : {exc}"
            self._drop_connection(conn, RpcTransportError, message)
            raise RpcTransportError(message, protocol=self.protocol, method=method) from exc
        if event is not None:
            event.duration_us = _elapsed_us(started)

    def _open_connection(self) -> _Connection:
        """Ouvre la connexion et lance son thread lecteur (appelé verrou tenu)."""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except TimeoutError as exc:
            # Pas un RpcTimeoutError : rien n'a été envoyé, l'issue de l'appel n'a donc rien d'inconnu.
            # Les clients gRPC et REST classent ce cas de la même façon (UNAVAILABLE).
            raise RpcTransportError(
                f"Connexion à {self.host}:{self.port} non établie en {_seconds(self.connect_timeout)}",
                protocol=self.protocol,
            ) from exc
        except OSError as exc:
            raise RpcTransportError(
                f"Connexion impossible à {self.host}:{self.port} : {exc}", protocol=self.protocol
            ) from exc
        # Lectures bloquantes : les échéances sont tenues par appel (gardien), pas par la socket.
        sock.settimeout(None)
        # Sans TCP_NODELAY, l'algorithme de Nagle retient les petites trames (~40 ms par appel).
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn = _Connection(sock)
        conn.reader = threading.Thread(
            target=self._read_loop, args=(conn,), name=f"{self.protocol}-stub-reader", daemon=True
        )
        self._conn = conn
        conn.reader.start()
        return conn

    # -- retour : ⑩ réception, ⑪ démarshalling, ⑫ retour --------------------------------

    def _read_loop(self, conn: _Connection) -> None:
        """Thread lecteur : lit les trames et remet chaque réponse à l'appel qui l'attend."""
        try:
            # ⑩ Réception : une trame complète à la fois.
            while (body := wire.read_frame(conn.sock)) is not None:
                self._on_frame(body)
            kind, message = RpcTransportError, "Connexion fermée par le serveur"
        except RpcProtocolError as exc:
            kind, message = RpcProtocolError, exc.message
        except OSError as exc:
            kind, message = RpcTransportError, f"Connexion perdue : {exc}"
        self._drop_connection(conn, kind, message)

    def _on_frame(self, body: bytes) -> None:
        traced = self._bus.enabled
        received_ns = time.perf_counter_ns() if traced else 0
        # ⑪ Démarshalling : octets → réponse(s). Un contenu hors norme fait abandonner la connexion.
        message = wire.decode_message(body)
        arrival = _Arrival(body, received_ns, _elapsed_us(received_ns)) if traced else None
        for entry in message if isinstance(message, list) else (message,):
            if isinstance(entry, Response):
                self._on_response(entry, arrival)
                arrival = None      # les évènements de trame ne sont publiés qu'une fois, même pour un lot
            elif entry.method == wire.STREAM_ITEM_METHOD:
                self._on_stream_item(entry, body)
            # Toute autre requête venue du serveur est ignorée : ce client n'expose aucune procédure.

    def _on_response(self, response: Response, arrival: _Arrival | None) -> None:
        # Corrélation : l'« id » de la réponse désigne l'appel en attente.
        with self._lock:
            call = self._pending.pop(response.id, None)
        if call is None:
            # Réponse tardive (l'appel a déjà expiré) ou inconnue : ignorée, jamais remise à un autre appel.
            return
        if call.traced and arrival is not None:
            call_id, method = call.future.call_id, call.future.method
            received = wire.frame(arrival.body)
            self._emit("client.receive", call_id, method, duration_us=(arrival.received_ns - call.sent_ns) / 1000,
                       payload=received, detail=wire.describe_frame(received))
            self._emit("client.unmarshal", call_id, method, duration_us=arrival.unmarshal_us)
        # ⑫ Retour : un résultat… ou l'erreur distante, retraduite en exception Python.
        if response.error is None:
            self._complete(call, response.result)
        else:
            self._fail(call, wire.error_to_exception(
                response.error, protocol=self.protocol, method=call.future.method
            ))

    def _on_stream_item(self, notification: Request, body: bytes) -> None:
        request_id, seq, item = wire.parse_stream_item(notification)
        with self._lock:
            call = self._pending.get(request_id)
            if call is None or call.items is None:
                return              # flux abandonné ou expiré : l'élément est ignoré
            call.deadline = time.perf_counter() + call.timeout  # l'échéance court ENTRE deux éléments
        if call.traced:
            self._emit("client.stream_item", call.future.call_id, call.future.method, payload=body,
                       detail={"seq": seq})
        call.items.put(item)

    def _consume(self, call: _PendingCall, items: queue.SimpleQueue) -> Iterator[Any]:
        """Itère sur les éléments d'un flux, puis relève l'issue de l'appel."""
        try:
            while (item := items.get()) is not _END_OF_STREAM:
                yield item
            final = call.future.result()        # lève l'erreur éventuelle : distante, échéance, coupure
            if not wire.is_stream_end(final):
                yield final                     # procédure unaire appelée comme un flux : un seul élément
        finally:
            with self._lock:                    # itération abandonnée : les éléments restants seront ignorés
                self._pending.pop(call.future.call_id, None)

    # -- issue d'un appel -------------------------------------------------------------
    #
    # Invariant : seul celui qui RETIRE un appel de la table ``_pending`` (réponse,
    # échéance, perte de connexion) le termine. Un appel a donc exactement une issue.

    def _complete(self, call: _PendingCall, result: Any) -> None:
        if call.traced:
            self._emit("client.return", call.future.call_id, call.future.method,
                       duration_us=_elapsed_us(call.started_ns), detail={"result_preview": _preview(result)})
        call.future.set_result(result)
        if call.items is not None:
            call.items.put(_END_OF_STREAM)

    def _fail(self, call: _PendingCall, error: RpcError) -> None:
        if call.traced:
            self._emit("client.error", call.future.call_id, call.future.method,
                       duration_us=_elapsed_us(call.started_ns), detail={"code": error.code, "message": error.message})
        call.future.set_exception(error)
        if call.items is not None:
            call.items.put(_END_OF_STREAM)

    def _error(self, kind: type[RpcError], message: str, call: _PendingCall) -> RpcError:
        return kind(message, protocol=self.protocol, method=call.future.method)

    def _drop_connection(self, conn: _Connection, kind: type[RpcError], message: str) -> None:
        """Abandonne une connexion morte : tous les appels en attente échouent, le prochain reconnectera."""
        with self._lock:
            if self._conn is not conn:
                return              # déjà abandonnée : lecteur et écrivain peuvent constater la même panne
            self._conn = None
            orphans = list(self._pending.values())
            self._pending.clear()
        _close_socket(conn.sock)
        for call in orphans:
            self._fail(call, self._error(kind, message, call))

    # -- échéances ----------------------------------------------------------------------

    def _arm_watchdog(self, deadline: float) -> None:
        """S'assure que le gardien se réveillera à temps pour ``deadline`` (appelé verrou tenu)."""
        if self._watchdog is None:
            self._watchdog = threading.Thread(
                target=self._watch_deadlines, name=f"{self.protocol}-stub-watchdog", daemon=True
            )
            self._watchdog.start()
        elif deadline < self._next_wakeup:
            self._wakeup.notify()

    def _watch_deadlines(self) -> None:
        """Thread gardien : un appel resté sans réponse au-delà de son échéance échoue en ``RpcTimeoutError``.

        Un appel local finit toujours par rendre la main ; un appel distant peut
        ne JAMAIS recevoir de réponse. Sans échéance, l'appelant attendrait
        indéfiniment — et après l'échéance, il ignore si le serveur a exécuté l'appel.
        """
        me = threading.current_thread()
        while True:
            with self._lock:
                if self._watchdog is not me:
                    return          # stub fermé
                # perf_counter plutôt que monotonic : sous Windows, ce dernier ne progresse que par pas de ~15 ms.
                now = time.perf_counter()
                overdue = [call for call in self._pending.values() if call.deadline <= now]
                for call in overdue:
                    # Retiré de la table : si la réponse arrive plus tard, elle ne trouvera plus de destinataire.
                    del self._pending[call.future.call_id]
                if not overdue:
                    self._next_wakeup = min((call.deadline for call in self._pending.values()), default=math.inf)
                    self._wakeup.wait(None if self._next_wakeup == math.inf else self._next_wakeup - now)
                    continue
            for call in overdue:
                self._fail(call, self._error(
                    RpcTimeoutError,
                    f"Pas de réponse à {call.future.method} après {_seconds(call.timeout)} : "
                    "l’issue de l’appel est inconnue",
                    call,
                ))

    def _emit(self, stage: str, call_id: str, method: str, **fields: Any) -> TraceEvent | None:
        return self._bus.emit(
            call_id=call_id, protocol=self.protocol, side="client", stage=stage, method=method, **fields
        )


def _to_params(args: tuple[Any, ...], kwargs: dict[str, Any]) -> wire.Params:
    """JSON-RPC 2.0 transporte des paramètres positionnels (tableau) OU nommés (objet), jamais un mélange."""
    if args and kwargs:
        raise TypeError("JSON-RPC 2.0 : paramètres positionnels OU nommés, pas les deux dans le même appel")
    return kwargs or list(args) or None


def _preview(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=repr)
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"


def _seconds(duration: float) -> str:
    return f"{duration:g} s".replace(".", ",")


def _elapsed_us(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1000


def _close_socket(sock: socket.socket) -> None:
    # shutdown() réveille le thread lecteur bloqué dans recv() ; close() seul n'y suffit pas partout.
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()
