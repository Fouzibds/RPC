"""Client de l'API REST de référence : la « cérémonie » HTTP écrite en clair.

Là où un stub RPC cache tout derrière un appel de méthode, un client REST fait
chaque geste lui-même : construire l'URL, choisir le verbe, encoder le corps
JSON, vérifier le code de statut, décoder la réponse, traduire les erreurs.
Ce module garde ces gestes visibles — c'est le point de comparaison du
laboratoire — tout en offrant la même interface ``InventoryClient`` que les
stubs, avec des dictionnaires de résultat identiques.

Une connexion persistante (keep-alive) par thread : ``submit()`` obtient un vrai
parallélisme, et deux threads ne mélangent jamais leurs requêtes. Aucun rejeu
automatique : une panne remonte telle quelle, les retries sont montrés
explicitement par ``netsim.resilience``. Seule une connexion que le pair a
fermée pendant qu'elle était inactive est remplacée sans bruit, avant tout envoi.
"""
from __future__ import annotations

import http.client
import json
import select
import socket
import threading
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import quote, urlencode

from common.client_api import InventoryClient
from common.config import CONNECT_TIMEOUT_S, DEFAULT_TIMEOUT_S
from common.errors import (
    CANCELLED,
    HTTP_TO_CANONICAL,
    INTERNAL,
    InvalidArgumentError,
    RpcError,
    RpcProtocolError,
    RpcTimeoutError,
    RpcTransportError,
    error_from_code,
)
from common.telemetry import BUS, EventBus, new_call_id

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

_PREVIEW_CHARS = 240
_UNREADABLE = object()  # corps de réponse qui n'est pas du JSON


class _Connection(http.client.HTTPConnection):
    """Connexion HTTP/1.1 persistante qui émet chaque requête en un seul ``sendall``.

    ``http.client`` écrit les en-têtes puis le corps en deux envois ; on les
    intercepte pour les regrouper (plan §4) et l'on connaît ainsi, à l'octet
    près, ce qui part sur le fil : c'est le payload de l'étape ``client.send``.
    """

    def __init__(self, host: str, port: int, generation: int) -> None:
        super().__init__(host, port)
        self.generation = generation
        self._outgoing: list[bytes] = []

    def connect(self) -> None:
        super().connect()
        # http.client le fait déjà ; on l'écrit noir sur blanc car tout le benchmark en dépend.
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send(self, data: bytes) -> None:
        self._outgoing.append(data)

    def take_request(self) -> bytes:
        """Octets exacts de la requête assemblée, prêts à partir en un seul envoi."""
        wire = b"".join(self._outgoing)
        self._outgoing.clear()
        return wire

    def close(self) -> None:
        self._outgoing.clear()
        super().close()


@dataclass(slots=True)
class _Call:
    """Contexte d'un appel en cours, partagé par les gestes successifs de la cérémonie."""

    method: str
    call_id: str
    timeout: float
    trace: StageTracer | None  # None quand le bus est coupé : aucun coût de télémétrie

    def fail(self, error: RpcError) -> None:
        if self.trace:
            self.trace.finish("client.error", detail={"code": error.code, "message": error.message})


class RestInventoryClient(InventoryClient):
    """``InventoryClient`` au-dessus de HTTP/1.1 + JSON (``http.client``).

    ``timeout`` (``DEFAULT_TIMEOUT_S`` si ``None``) borne chaque attente réseau
    d'un appel ; pour un flux, c'est le silence maximal toléré entre deux
    éléments, pas la durée totale.
    """

    protocol = PROTOCOL

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float | None = None,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        bus: EventBus = BUS,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = DEFAULT_TIMEOUT_S if timeout is None else timeout
        self.connect_timeout = connect_timeout
        self._bus = bus
        self._local = threading.local()
        self._connections: dict[_Connection, threading.Thread | None] = {}  # connexion -> thread propriétaire
        self._lock = threading.Lock()
        self._generation = 0

    # -- les cinq procédures : un verbe, une URL, parfois un corps ----------------

    def calculate_factorial(self, n: int, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call("calculate_factorial", {"n": n}, "GET", ("api", "factorial", n), timeout=timeout)

    def get_product_details(self, product_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call(
            "get_product_details", {"product_id": product_id}, "GET", ("api", "products", product_id), timeout=timeout
        )

    def update_stock(
        self, product_id: str, delta: int, *, idempotency_key: str = "", timeout: float | None = None
    ) -> dict[str, Any]:
        body = {"delta": delta, "idempotency_key": idempotency_key}
        return self._call(
            "update_stock", {"product_id": product_id, **body}, "POST", ("api", "stock", product_id),
            body=body, timeout=timeout,
        )

    def list_products(self, limit: int = 20, category: str = "", *, timeout: float | None = None) -> dict[str, Any]:
        query = {"limit": limit, "category": category}
        return self._call("list_products", query, "GET", ("api", "products"), query=query, timeout=timeout)

    def stream_analytics(
        self, samples: int = 10, interval_ms: int = 200, *, timeout: float | None = None
    ) -> Iterator[dict[str, Any]]:
        """Flux NDJSON : la requête part tout de suite, les éléments sont rendus à mesure qu'ils arrivent.

        Le flux occupe une connexion qui lui est propre : le thread appelant
        peut lancer d'autres appels pendant qu'il le consomme.
        """
        query = {"samples": samples, "interval_ms": interval_ms}
        call = self._begin("stream_analytics", query, timeout)
        connection = self._open(None)
        streaming = False
        try:
            target, payload = self._marshal(call, "GET", ("api", "analytics", "stream"), query, None)
            try:
                response = self._send(call, connection, "GET", target, payload, accept=NDJSON_MEDIA_TYPE)
                refusal = response.read() if response.status != http.client.OK else None
            except (OSError, http.client.HTTPException) as exc:
                raise self._transport_error(call, exc) from exc
            if refusal is not None:
                self._conclude(call, response, refusal, "GET", target)
                raise RpcProtocolError(
                    f"Statut {response.status} inattendu pour un flux", protocol=PROTOCOL, method=call.method
                )
            if call.trace:
                _trace_received(call.trace, response, b"", "GET", target)
            streaming = True
        except RpcError as error:
            call.fail(error)
            raise
        finally:
            if not streaming:
                self._release(connection)
        return self._stream_items(call, connection, response)

    # -- cycle de vie -------------------------------------------------------------

    def close(self) -> None:
        """Ferme les connexions de tous les threads ; un appel ultérieur se reconnecte."""
        super().close()
        with self._lock:
            self._generation += 1
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            # On ne touche qu'à la socket : l'objet connexion appartient à son thread, qui verra
            # une coupure (appel en cours) ou le remplacera (prochain appel).
            sock = connection.sock
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # déjà coupée par le serveur
            sock.close()

    # -- la cérémonie REST ----------------------------------------------------------

    def _call(
        self,
        method: str,
        arguments: dict[str, Any],
        verb: str,
        path: tuple[Any, ...],
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Appel unaire complet : une requête, une réponse JSON."""
        call = self._begin(method, arguments, timeout)
        try:
            # 1. Marshalling : les arguments se répartissent entre l'URL et le corps JSON.
            target, payload = self._marshal(call, verb, path, query, body)
            # 2. Transport : requête sur la connexion persistante du thread, puis attente de la réponse.
            connection = self._connection()
            answered = False
            try:
                response = self._send(call, connection, verb, target, payload)
                content = response.read()
                answered = True
            except (OSError, http.client.HTTPException) as exc:
                raise self._transport_error(call, exc) from exc
            finally:
                if not answered:
                    # Panne, délai dépassé ou interruption : l'état de la connexion est inconnu, et
                    # une réponse tardive ne doit pas être prise pour celle de l'appel suivant.
                    connection.close()
            # 3. Démarshalling : le statut HTTP dit si le corps est un résultat ou une erreur.
            result = self._conclude(call, response, content, verb, target)
        except RpcError as error:
            call.fail(error)
            raise
        if call.trace:
            call.trace.finish("client.return", detail={"result_preview": _preview(content)})
        return result

    def _begin(self, method: str, arguments: dict[str, Any], timeout: float | None) -> _Call:
        """Ouvre un appel : identifiant de corrélation, délai effectif, traceur si le bus écoute."""
        call_id = self.last_call_id = new_call_id(PROTOCOL)
        trace = StageTracer(self._bus, "client", call_id, method) if self._bus.enabled else None
        if trace:
            trace.mark("client.call", detail={"args": [], "kwargs": arguments})
        return _Call(method, call_id, self.timeout if timeout is None else timeout, trace)

    def _marshal(
        self,
        call: _Call,
        verb: str,
        path: tuple[Any, ...],
        query: dict[str, Any] | None,
        body: dict[str, Any] | None,
    ) -> tuple[str, bytes]:
        """Construit la cible (chemin + chaîne de requête) et encode le corps JSON éventuel."""
        # Dans l'URL, chaque argument devient du texte : le type est perdu, le serveur devra le retrouver.
        target = "/" + "/".join(quote(str(segment), safe="") for segment in path)
        if query:
            target += "?" + urlencode(query)
        try:
            payload = encode_json(body) if body is not None else b""
        except (TypeError, ValueError) as exc:
            raise InvalidArgumentError(
                f"Argument non représentable en JSON : {exc}", protocol=PROTOCOL, method=call.method
            ) from exc
        if call.trace:
            # Sans corps (GET), le payload est vide : tout le « message » tient dans la cible.
            detail: dict[str, Any] = {"http": {"method": verb, "path": target, "status": None}}
            if payload:
                detail["text"] = json_text(payload)
            call.trace.step("client.marshal", payload=payload, detail=detail)
        return target, payload

    def _send(
        self,
        call: _Call,
        connection: _Connection,
        verb: str,
        target: str,
        payload: bytes,
        *,
        accept: str = JSON_MEDIA_TYPE,
    ) -> http.client.HTTPResponse:
        """Écrit la requête en un seul envoi, puis attend la ligne de statut et les en-têtes."""
        if call.timeout <= 0:
            raise RpcTimeoutError(
                f"Délai de {call.timeout:g} s déjà écoulé : l’appel n’a pas été envoyé",
                protocol=PROTOCOL, method=call.method, detail={"timeout_s": call.timeout},
            )
        if connection.sock is not None and _hung_up(connection.sock):
            connection.close()  # pas un rejeu : rien n'est encore parti, on évite juste d'écrire dans le vide
        if connection.sock is None:
            self._connect(call, connection)
        connection.sock.settimeout(call.timeout)
        connection.putrequest(verb, target, skip_accept_encoding=True)
        connection.putheader("Accept", accept)
        connection.putheader(CALL_ID_HEADER, call.call_id)
        if payload:
            connection.putheader("Content-Type", JSON_MEDIA_TYPE)
            connection.putheader("Content-Length", str(len(payload)))
        connection.endheaders(payload or None)
        wire = connection.take_request()
        trace = call.trace
        announced = None
        if trace:
            # Publié juste avant l'écriture : sinon le serveur, déjà réveillé, pourrait annoncer
            # « server.receive » avant que ce thread n'ait annoncé « client.send ». La durée
            # (mise en trame) est complétée par celle de l'écriture au retour de sendall.
            announced = trace.step("client.send", payload=wire, detail={
                "segments": http_segments(
                    REQUEST_LINE, wire.index(b"\r\n") + 2, len(wire) - len(payload), len(wire)
                ),
                "http": {"method": verb, "path": target, "status": None},
            })
        connection.sock.sendall(wire)
        if trace:
            trace.settle(announced)
        return connection.getresponse()

    def _conclude(
        self, call: _Call, response: http.client.HTTPResponse, content: bytes, verb: str, target: str
    ) -> Any:
        """Décode le corps ; hors 2xx, lève la ``RpcError`` correspondante.

        Le code d'erreur du corps JSON fait foi ; sans corps exploitable (proxy,
        serveur tiers), seul le statut HTTP renseigne.
        """
        if call.trace:
            _trace_received(call.trace, response, content, verb, target)
        try:
            document = json.loads(content)
        except ValueError:
            document = _UNREADABLE
        if call.trace:
            call.trace.step("client.unmarshal")
        status = response.status
        if 200 <= status < 300:
            if document is _UNREADABLE:
                raise RpcProtocolError(
                    f"Réponse {status} illisible : un document JSON était attendu",
                    protocol=PROTOCOL, method=call.method,
                )
            return document
        try:
            error = document["error"]
            code, message, detail = str(error["code"]), str(error["message"]), error.get("detail")
        except (KeyError, TypeError):
            code = HTTP_TO_CANONICAL.get(status, INTERNAL)
            message, detail = f"HTTP {status} {response.reason}", {"http_status": status}
        raise error_from_code(code, message, protocol=PROTOCOL, method=call.method, detail=detail)

    def _stream_items(
        self, call: _Call, connection: _Connection, response: http.client.HTTPResponse
    ) -> Iterator[dict[str, Any]]:
        """Rend chaque ligne NDJSON dès que son fragment arrive, sans attendre la fin du corps."""
        trace = call.trace
        count = 0
        pending = b""
        try:
            while True:
                try:
                    data = response.read1()
                except (OSError, http.client.HTTPException) as exc:
                    raise self._transport_error(call, exc) from exc
                if not data:
                    break
                *lines, pending = (pending + data).split(b"\n")
                for line in lines:
                    try:
                        item = json.loads(line)
                    except ValueError as exc:
                        raise RpcProtocolError(
                            "Élément de flux illisible : une ligne JSON était attendue",
                            protocol=PROTOCOL, method=call.method,
                        ) from exc
                    count += 1
                    if trace:
                        trace.step("client.stream_item", payload=line, detail={"seq": count})
                    yield item
            if trace:
                trace.finish("client.return", detail={"result_preview": f"{count} éléments reçus", "count": count})
        except RpcError as error:
            call.fail(error)
            raise
        except GeneratorExit:
            if trace:
                trace.finish("client.error", detail={"code": CANCELLED, "message": "Flux abandonné par l’appelant"})
            raise
        finally:
            self._release(connection)

    # -- connexions -----------------------------------------------------------------

    def connect(self, timeout: float | None = None) -> bool:
        """Ouvre la connexion persistante du thread appelant, celle que ses appels suivants utiliseront."""
        connection = self._connection()
        if connection.sock is not None and not _hung_up(connection.sock):
            return True
        connection.close()
        connection.timeout = self.connect_timeout if timeout is None else min(self.connect_timeout, timeout)
        try:
            connection.connect()
        except OSError:
            connection.close()
            return False
        return True

    def _connection(self) -> _Connection:
        """Connexion persistante du thread courant, remplacée après un ``close()`` du client."""
        connection: _Connection | None = getattr(self._local, "connection", None)
        if connection is None or connection.generation != self._generation:
            if connection is not None:
                connection.close()
            connection = self._local.connection = self._open(threading.current_thread())
        return connection

    def _open(self, owner: threading.Thread | None) -> _Connection:
        """Nouvelle connexion (pas encore établie), enregistrée pour que ``close()`` puisse la couper.

        ``owner`` est le thread auquel elle est réservée ; ``None`` pour un flux,
        dont l'itérateur peut être consommé par un autre thread que celui qui l'a ouvert.
        """
        with self._lock:
            # Un thread terminé ne fermera jamais sa connexion : on le fait pour lui au passage.
            orphans = [known for known, thread in self._connections.items() if thread and not thread.is_alive()]
            for orphan in orphans:
                del self._connections[orphan]
                orphan.close()
            connection = _Connection(self.host, self.port, self._generation)
            self._connections[connection] = owner
        return connection

    def _release(self, connection: _Connection) -> None:
        """Ferme et oublie une connexion à usage unique (celle d'un flux)."""
        connection.close()
        with self._lock:
            self._connections.pop(connection, None)

    def _connect(self, call: _Call, connection: _Connection) -> None:
        connection.timeout = min(self.connect_timeout, call.timeout)
        try:
            connection.connect()
        except OSError as exc:
            # Rien n'est parti : l'appel n'a pas pu s'exécuter, même si c'est un délai qui a expiré.
            connection.close()
            cause = type(exc).__name__
            raise RpcTransportError(
                f"Connexion impossible à {self.host}:{self.port} ({cause})",
                protocol=PROTOCOL, method=call.method, detail={"phase": "connect", "cause": cause},
            ) from exc

    def _transport_error(self, call: _Call, exc: Exception) -> RpcError:
        """Traduit une panne de transport : délai dépassé, connexion coupée ou réponse HTTP invalide."""
        peer = f"{self.host}:{self.port}"
        if isinstance(exc, TimeoutError):
            return RpcTimeoutError(
                f"Aucune réponse de {peer} en {call.timeout:g} s : l’issue de l’appel est inconnue",
                protocol=PROTOCOL, method=call.method, detail={"timeout_s": call.timeout},
            )
        cause = type(exc).__name__
        garbled = isinstance(exc, http.client.HTTPException) and not isinstance(
            exc, (ConnectionError, http.client.IncompleteRead)
        )
        if garbled:
            return RpcProtocolError(
                f"Réponse HTTP invalide de {peer} ({cause})",
                protocol=PROTOCOL, method=call.method, detail={"cause": cause},
            )
        return RpcTransportError(
            f"Connexion à {peer} interrompue pendant l’appel ({cause})",
            protocol=PROTOCOL, method=call.method, detail={"phase": "exchange", "cause": cause},
        )


def _trace_received(
    trace: StageTracer, response: http.client.HTTPResponse, content: bytes, verb: str, target: str
) -> None:
    """Publie ``client.receive`` avec le message HTTP tel qu'il a été reçu.

    ``http.client`` décode la ligne de statut et les en-têtes sans garder leurs
    octets : on les recompose à l'identique (même ordre, même casse).
    """
    status_line = f"HTTP/{response.version // 10}.{response.version % 10} {response.status} {response.reason}\r\n"
    headers = "".join(f"{name}: {value}\r\n" for name, value in response.getheaders())
    head = f"{status_line}{headers}\r\n".encode("latin-1")
    trace.step("client.receive", payload=head + content, detail={
        "segments": http_segments(STATUS_LINE, len(status_line), len(head), len(head) + len(content)),
        "http": {"method": verb, "path": target, "status": response.status},
    })


def _hung_up(sock: socket.socket) -> bool:
    """Vrai si le pair a fermé cette connexion pendant qu'elle était inactive.

    Une connexion keep-alive au repos n'a rien à lire : si elle est « lisible »,
    c'est une fin de flux (ou des octets hors protocole) et elle est inutilisable.
    """
    try:
        readable, _, _ = select.select((sock,), (), (), 0)
    except (OSError, ValueError):  # socket déjà fermée par un close() du client venu d'un autre thread
        return True
    return bool(readable)


def _preview(content: bytes) -> str:
    """Début lisible d'un corps JSON, pour l'étape ``client.return``."""
    text = content[: _PREVIEW_CHARS * 4].decode("utf-8", "ignore")
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"
