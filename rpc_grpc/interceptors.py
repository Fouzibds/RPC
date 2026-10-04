"""Traçage gRPC « sous le capot » : les étapes du plan §5, avec les octets réels.

gRPC cache sa mécanique — c'est son métier. Pour la montrer sans réécrire le
protocole, on s'insère aux deux seuls endroits où les octets Protobuf sont
visibles depuis Python :

* **côté client**, ``TracingChannel`` décore le canal sur lequel le stub généré
  par protoc est branché. Le stub lui confie ses fonctions de sérialisation ;
  le canal les exécute lui-même, les chronomètre, publie les étapes, puis remet
  les OCTETS au vrai canal gRPC (appel « brut », sans sérialiseur). Ce qui est
  affiché est donc exactement ce qui part sur le fil, et ce qui en revient ;
* **côté serveur**, ``TracingServerInterceptor`` reconstruit le gestionnaire de
  la méthode appelée en enveloppant son désérialiseur, son comportement et son
  sérialiseur.

L'identifiant de corrélation voyage dans la métadonnée ``x-call-id`` : client et
serveur publient sous le même ``call_id``.

Bus coupé, rien de tout cela ne s'exécute : le canal délègue au stub ordinaire
et l'intercepteur rend le gestionnaire d'origine, intact.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Iterable, Iterator

import grpc
from google.protobuf import text_format
from google.protobuf.descriptor import Descriptor
from google.protobuf.message import Message

from common.catalog import METHODS
from common.errors import GRPC_TO_CANONICAL, INTERNAL
from common.telemetry import BUS, EventBus, new_call_id

from .wire_inspector import FRAME_HEADER_SIZE, frame_segments, grpc_frame, segments_for

PROTOCOL = "grpc"
CALL_ID_KEY = "x-call-id"

_TEXT_LIMIT = 4096      # au-delà, le rendu texte du message est omis des traces
_PREVIEW_ITEMS = 3      # éléments conservés par champ répété dans un aperçu
_PYTHON_NAMES = {spec.grpc_method: spec.name for spec in METHODS}
_RESPONSE_HEADERS = {":status": "200", "content-type": "application/grpc"}
_OK_TRAILERS = {"grpc-status": "0"}
_TRANSPORT_NOTE = (
    "L’écriture sur la connexion HTTP/2 est faite par le cœur C de gRPC : la durée "
    "mesurée ici est celle du tramage ; l’aller-retour réseau est compté dans la réception."
)
_CLIENT_GONE = "Appel abandonné par le client (annulation ou échéance dépassée)"


def _identity(value: Any) -> Any:
    return value


def _us(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1000


# --- Outils partagés ---------------------------------------------------------

def procedure_name(path: str) -> str:
    """« /pkg.Service/UpdateStock » → « update_stock », le nom commun aux trois protocoles."""
    rpc = path.rsplit("/", 1)[-1]
    return _PYTHON_NAMES.get(rpc) or re.sub(r"(?<!^)(?=[A-Z])", "_", rpc).lower()


def rpc_status(error: grpc.RpcError) -> tuple[str, str]:
    """Nom du statut gRPC et message portés par une erreur de stub."""
    code = error.code() if callable(getattr(error, "code", None)) else None
    details = error.details() if callable(getattr(error, "details", None)) else None
    return (code.name if code is not None else "UNKNOWN", details or str(error))


def message_preview(message: Any, max_items: int = _PREVIEW_ITEMS) -> dict[str, Any]:
    """Vue dictionnaire d'un message pour les traces (JSON-sérialisable, listes abrégées)."""
    if not isinstance(message, Message):
        return {}
    preview: dict[str, Any] = {}
    for field in message.DESCRIPTOR.fields:
        value = getattr(message, field.name)
        nested = field.message_type is not None
        if field.is_repeated:
            items = [message_preview(item, max_items) if nested else item for item in value[:max_items]]
            if len(value) > max_items:
                items.append(f"… (+{len(value) - max_items})")
            value = items
        elif nested:
            value = message_preview(value, max_items)
        elif isinstance(value, bytes):
            value = value.hex()
        preview[field.name] = value
    return preview


class LocalRpcError(grpc.RpcError):
    """Erreur constatée par le stub lui-même, sans réponse du serveur."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details

    def trailing_metadata(self) -> tuple:
        return ()


class _Tally:
    """Cumul des messages d'un flux (nombre, octets, temps de (dé)sérialisation)."""

    __slots__ = ("count", "size", "spent_us")

    def __init__(self) -> None:
        self.count = 0
        self.size = 0
        self.spent_us = 0.0

    def add(self, size: int, elapsed_us: float) -> None:
        self.count += 1
        self.size += size
        self.spent_us += elapsed_us

    @property
    def framed_size(self) -> int:
        return self.size + self.count * FRAME_HEADER_SIZE


def _descriptor_of(source: Any) -> Descriptor | None:
    """Descriptor d'un message, ou de la classe à laquelle un ``FromString`` est lié."""
    return getattr(getattr(source, "__self__", source), "DESCRIPTOR", None)


def _message_detail(message: Any, size: int) -> dict[str, Any]:
    descriptor = _descriptor_of(message)
    if descriptor is None:
        return {}
    detail: dict[str, Any] = {"message_type": descriptor.full_name}
    if size <= _TEXT_LIMIT:
        detail["text"] = text_format.MessageToString(message, as_utf8=True)
    return detail


def _framed(data: bytes, descriptor: Descriptor | None) -> tuple[bytes, float, dict[str, Any]]:
    """Trame gRPC d'un message, durée du tramage et détail pour la vue hexadécimale."""
    started = time.perf_counter_ns()
    framed = grpc_frame(data)
    elapsed = _us(started)
    detail: dict[str, Any] = {
        "frame_header_hex": framed[:FRAME_HEADER_SIZE].hex(),
        "segments": frame_segments(data, descriptor, strict=False),
    }
    if descriptor is not None:
        detail["message_type"] = descriptor.full_name
    return framed, elapsed, detail


def _item_detail(direction: str, seq: int, data: bytes, descriptor: Descriptor | None) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "direction": direction,
        "seq": seq,
        "segments": segments_for(data, descriptor, strict=False),
    }
    if descriptor is not None:
        detail["message_type"] = descriptor.full_name
    return detail


def _printable(metadata: Iterable[tuple[str, Any]]) -> dict[str, str]:
    """Métadonnées gRPC → en-têtes affichables (les valeurs binaires « -bin » en hexadécimal)."""
    return {key: value if isinstance(value, str) else bytes(value).hex() for key, value in metadata}


# --- Côté client -------------------------------------------------------------

class _ClientTrace:
    """Publie les étapes client d'un appel, autour de l'appel gRPC « brut »."""

    def __init__(self, owner: "_TracedMultiCallable", timeout: float | None, metadata: Any) -> None:
        supplied = tuple(metadata or ())
        call_id = next((value for key, value in supplied if key == CALL_ID_KEY), None)
        if call_id is None:
            call_id = new_call_id(PROTOCOL)
            supplied += ((CALL_ID_KEY, call_id),)
        self.call_id: str = call_id
        self.metadata = supplied
        self._owner = owner
        self._method = procedure_name(owner.path)
        self._headers = {
            ":method": "POST",
            ":scheme": "http",
            ":path": owner.path,
            ":authority": owner.authority,
            "content-type": "application/grpc",
            "te": "trailers",
            "user-agent": f"grpc-python/{grpc.__version__}",
        }
        if timeout is not None:
            # L'échéance voyage avec la requête : le serveur sait combien de temps il lui reste.
            self._headers["grpc-timeout"] = f"{max(round(timeout * 1000), 1)}m"
        self._headers.update(_printable(supplied))
        self._outgoing = _Tally()
        self._incoming = _Tally()
        self._finished = False
        self._started = self._sent_at = time.perf_counter_ns()

    def _emit(self, stage: str, **fields: Any) -> None:
        self._owner.bus.emit(
            call_id=self.call_id, protocol=PROTOCOL, side="client", stage=stage, method=self._method, **fields
        )

    def call(self, request: Any = None) -> None:
        self._emit("client.call", detail={
            "args": [],
            "kwargs": message_preview(request),
            "rpc": self._owner.path,
            "kind": self._owner.kind,
        })

    def marshal(self, request: Any) -> bytes:
        """Requête unique : sérialisation (``client.marshal``) puis tramage (``client.send``)."""
        started = time.perf_counter_ns()
        try:
            data = self._owner.serialize(request)
        except Exception as exc:
            raise self._local_failure("Exception serializing request!") from exc
        self._emit("client.marshal", duration_us=_us(started), payload=data, detail=_message_detail(request, len(data)))
        framed, elapsed, detail = _framed(data, _descriptor_of(request))
        self._emit("client.send", duration_us=elapsed, payload=framed, detail={
            "http2_headers": self._headers, "note": _TRANSPORT_NOTE, **detail,
        })
        self._sent_at = time.perf_counter_ns()
        return data

    def outgoing(self, requests: Iterable[Any]) -> Iterator[bytes]:
        """Flux de requêtes : chaque élément est sérialisé et tracé au rythme où gRPC le consomme."""
        tally = self._outgoing
        for request in requests:
            started = time.perf_counter_ns()
            data = self._owner.serialize(request)
            elapsed = _us(started)
            tally.add(len(data), elapsed)
            self._emit("client.stream_item", duration_us=elapsed, payload=data,
                       detail=_item_detail("request", tally.count, data, _descriptor_of(request)))
            yield data
        self._emit("client.marshal", duration_us=tally.spent_us, size=tally.size, detail={"messages": tally.count})
        self._emit("client.send", size=tally.framed_size, detail={
            "messages": tally.count, "http2_headers": self._headers, "note": _TRANSPORT_NOTE,
        })
        self._sent_at = time.perf_counter_ns()

    def unmarshal(self, data: bytes) -> Any:
        """Réponse unique : réception (``client.receive``) puis désérialisation (``client.unmarshal``)."""
        waited = _us(self._sent_at)
        framed, _, detail = _framed(data, self._owner.response_descriptor)
        self._emit("client.receive", duration_us=waited, payload=framed, detail={
            "http2_headers": _RESPONSE_HEADERS, "http2_trailers": _OK_TRAILERS, **detail,
        })
        started = time.perf_counter_ns()
        try:
            response = self._owner.deserialize(data)
        except Exception as exc:
            raise self._local_failure("Exception deserializing response!") from exc
        self._emit("client.unmarshal", duration_us=_us(started), detail=_message_detail(response, len(data)))
        return response

    def incoming(self, data: bytes) -> Any:
        """Un élément d'un flux de réponses."""
        tally = self._incoming
        started = time.perf_counter_ns()
        try:
            message = self._owner.deserialize(data)
        except Exception as exc:
            raise self._local_failure("Exception deserializing response!") from exc
        elapsed = _us(started)
        tally.add(len(data), elapsed)
        self._emit("client.stream_item", duration_us=elapsed, payload=data,
                   detail=_item_detail("response", tally.count, data, self._owner.response_descriptor))
        return message

    def end_of_stream(self) -> None:
        """Fin normale d'un flux de réponses : bilan cumulé, puis retour à l'appelant."""
        if self._finished:
            return
        self._finished = True
        tally = self._incoming
        self._emit("client.receive", duration_us=_us(self._sent_at), size=tally.framed_size, detail={
            "messages": tally.count, "http2_headers": _RESPONSE_HEADERS, "http2_trailers": _OK_TRAILERS,
        })
        self._emit("client.unmarshal", duration_us=tally.spent_us, detail={"messages": tally.count})
        self._emit("client.return", duration_us=_us(self._started),
                   detail={"result_preview": {"stream": "end", "count": tally.count}})

    def done(self, response: Any) -> None:
        self._finished = True
        self._emit("client.return", duration_us=_us(self._started),
                   detail={"result_preview": message_preview(response)})

    def fail(self, error: grpc.RpcError) -> None:
        if self._finished:
            return
        self._finished = True
        status, message = rpc_status(error)
        self._emit("client.error", duration_us=_us(self._started), detail={
            "code": GRPC_TO_CANONICAL.get(status, INTERNAL),
            "message": message,
            "grpc_status": status,
        })

    def _local_failure(self, details: str) -> LocalRpcError:
        error = LocalRpcError(grpc.StatusCode.INTERNAL, details)
        self.fail(error)
        return error


class _TracedResponses:
    """Flux de réponses tracé ; le reste (``code()``, ``trailing_metadata()``…) est délégué à l'appel réel."""

    def __init__(self, call: Any, trace: _ClientTrace) -> None:
        self._call = call
        self._trace = trace

    def __iter__(self) -> "_TracedResponses":
        return self

    def __next__(self) -> Any:
        try:
            data = next(self._call)
        except StopIteration:
            self._trace.end_of_stream()
            raise
        except grpc.RpcError as error:
            self._trace.fail(error)
            raise
        return self._trace.incoming(data)

    def cancel(self) -> bool:
        cancelled = self._call.cancel()
        if cancelled:
            self._trace.fail(LocalRpcError(grpc.StatusCode.CANCELLED, "Flux interrompu par le client"))
        return cancelled

    def __getattr__(self, name: str) -> Any:
        return getattr(self._call, name)


# Méthode de ``grpc.Channel`` → (forme d'appel au sens de ``common.catalog``, requêtes en flux, réponses en flux).
_CALL_FORMS: dict[str, tuple[str, bool, bool]] = {
    "unary_unary": ("unary", False, False),
    "unary_stream": ("server_stream", False, True),
    "stream_unary": ("client_stream", True, False),
    "stream_stream": ("bidi_stream", True, True),
}


class _TracedMultiCallable:
    """Méthode de stub tracée ; même interface d'appel que les objets ``*MultiCallable`` de gRPC.

    Elle détient deux appels gRPC vers la même méthode : l'appel ordinaire (utilisé
    tel quel quand le bus est coupé) et son jumeau « brut », sans sérialiseur, qui
    transporte les octets que l'on a soi-même produits et observés.
    """

    def __init__(
        self,
        channel: "TracingChannel",
        form: str,
        path: str,
        request_serializer: Callable[[Any], bytes] | None,
        response_deserializer: Callable[[bytes], Any] | None,
        registered: bool,
    ) -> None:
        build = getattr(channel.inner, form)
        self._plain = build(
            path,
            request_serializer=request_serializer,
            response_deserializer=response_deserializer,
            _registered_method=registered,
        )
        self._raw = build(path, _registered_method=registered)
        self.kind, self._request_streaming, self._response_streaming = _CALL_FORMS[form]
        self.bus = channel.bus
        self.authority = channel.authority
        self.path = path
        self.serialize = request_serializer or _identity
        self.deserialize = response_deserializer or _identity
        self.response_descriptor = _descriptor_of(response_deserializer)

    def __call__(self, request: Any, timeout: float | None = None, metadata: Any = None, **options: Any) -> Any:
        """Appel bloquant (réponse unique) ou ouverture d'un flux de réponses, selon la forme."""
        if not self.bus.enabled:
            return self._plain(request, timeout=timeout, metadata=metadata, **options)
        if not self._response_streaming:
            return self._single_response(request, timeout, metadata, options)[0]
        trace, argument = self._begin(request, timeout, metadata)
        return _TracedResponses(self._raw(argument, timeout=timeout, metadata=trace.metadata, **options), trace)

    def with_call(self, request: Any, timeout: float | None = None, metadata: Any = None, **options: Any) -> tuple:
        """Réponse unique, accompagnée de l'objet ``grpc.Call`` (statut, métadonnées de fin)."""
        if not self.bus.enabled:
            return self._plain.with_call(request, timeout=timeout, metadata=metadata, **options)
        return self._single_response(request, timeout, metadata, options)

    def future(self, request: Any, timeout: float | None = None, metadata: Any = None, **options: Any) -> Any:
        """Appel asynchrone natif de gRPC : délégué tel quel, sans traçage."""
        return self._plain.future(request, timeout=timeout, metadata=metadata, **options)

    def _begin(self, request: Any, timeout: float | None, metadata: Any) -> tuple[_ClientTrace, Any]:
        """Ouvre la trace et prépare ce qui partira sur le fil : des octets, ou un flux d'octets."""
        trace = _ClientTrace(self, timeout, metadata)
        if self._request_streaming:
            trace.call()
            return trace, trace.outgoing(request)
        trace.call(request)
        return trace, trace.marshal(request)

    def _single_response(self, request: Any, timeout: float | None, metadata: Any, options: dict) -> tuple:
        trace, argument = self._begin(request, timeout, metadata)
        try:
            raw, call = self._raw.with_call(argument, timeout=timeout, metadata=trace.metadata, **options)
        except grpc.RpcError as error:
            trace.fail(error)
            raise
        response = trace.unmarshal(raw)
        trace.done(response)
        return response, call


class TracingChannel(grpc.Channel):
    """Canal décorateur : le stub généré par protoc s'y branche sans aucune modification.

        stub = service_pb2_grpc.InventoryServiceStub(TracingChannel(channel, authority="127.0.0.1:50051"))

    Chaque appel publie les étapes client du plan §5. L'identifiant de corrélation
    est lu dans la métadonnée ``x-call-id`` de l'appel (ou créé, puis ajouté).
    Seuls les arguments nommés ``credentials``, ``wait_for_ready`` et
    ``compression`` sont transmis tels quels à gRPC.
    """

    def __init__(self, channel: grpc.Channel, *, authority: str = "", bus: EventBus = BUS) -> None:
        self.inner = channel
        self.authority = authority
        self.bus = bus

    def unary_unary(self, method, request_serializer=None, response_deserializer=None, _registered_method=False):
        return self._traced("unary_unary", method, request_serializer, response_deserializer, _registered_method)

    def unary_stream(self, method, request_serializer=None, response_deserializer=None, _registered_method=False):
        return self._traced("unary_stream", method, request_serializer, response_deserializer, _registered_method)

    def stream_unary(self, method, request_serializer=None, response_deserializer=None, _registered_method=False):
        return self._traced("stream_unary", method, request_serializer, response_deserializer, _registered_method)

    def stream_stream(self, method, request_serializer=None, response_deserializer=None, _registered_method=False):
        return self._traced("stream_stream", method, request_serializer, response_deserializer, _registered_method)

    def _traced(self, form: str, *contract: Any) -> _TracedMultiCallable:
        """``contract`` : chemin de la méthode, sérialiseur, désérialiseur, enregistrement — fournis par le stub."""
        return _TracedMultiCallable(self, form, *contract)

    def subscribe(self, callback, try_to_connect=False):
        self.inner.subscribe(callback, try_to_connect)

    def unsubscribe(self, callback):
        self.inner.unsubscribe(callback)

    def close(self) -> None:
        self.inner.close()

    def __enter__(self) -> "TracingChannel":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False


# --- Côté serveur ------------------------------------------------------------

class _ServerTrace:
    """Publie les étapes serveur d'un appel en enveloppant son gestionnaire de méthode."""

    def __init__(
        self,
        bus: EventBus,
        call_id: str,
        path: str,
        headers: dict[str, str],
        lookup_us: float,
        handler: grpc.RpcMethodHandler,
    ) -> None:
        self._bus = bus
        self._call_id = call_id
        self._path = path
        self._method = procedure_name(path)
        self._headers = headers
        self._lookup_us = lookup_us
        self._handler = handler
        self._deserialize = handler.request_deserializer or _identity
        self._serialize = handler.response_serializer or _identity
        self._request_descriptor = _descriptor_of(handler.request_deserializer)
        self._incoming = _Tally()
        self._outgoing = _Tally()

    def _emit(self, stage: str, **fields: Any) -> None:
        self._bus.emit(
            call_id=self._call_id, protocol=PROTOCOL, side="server", stage=stage, method=self._method, **fields
        )

    def traced_handler(self) -> grpc.RpcMethodHandler:
        """Même gestionnaire, dont chaque maillon (octets → message → procédure → octets) est observé."""
        source = self._handler
        if source.request_streaming and source.response_streaming:
            factory, behavior = grpc.stream_stream_rpc_method_handler, self._streaming(source.stream_stream)
        elif source.request_streaming:
            factory, behavior = grpc.stream_unary_rpc_method_handler, self._single(source.stream_unary)
        elif source.response_streaming:
            factory, behavior = grpc.unary_stream_rpc_method_handler, self._streaming(source.unary_stream)
        else:
            factory, behavior = grpc.unary_unary_rpc_method_handler, self._single(source.unary_unary)
        return factory(behavior, request_deserializer=self._read, response_serializer=self._write)

    def _read(self, data: bytes) -> Any:
        """Désérialiseur enveloppé : gRPC lui remet les octets reçus, un message à la fois."""
        streaming = self._handler.request_streaming
        if not streaming:
            framed, _, detail = _framed(data, self._request_descriptor)
            self._emit("server.receive", payload=framed, detail={"http2_headers": self._headers, **detail})
        started = time.perf_counter_ns()
        try:
            message = self._deserialize(data)
        except Exception:
            self._error(grpc.StatusCode.INTERNAL, "Requête illisible : désérialisation impossible")
            raise
        elapsed = _us(started)
        if streaming:
            tally = self._incoming
            tally.add(len(data), elapsed)
            self._emit("server.stream_item", duration_us=elapsed, payload=data,
                       detail=_item_detail("request", tally.count, data, self._request_descriptor))
        else:
            self._emit("server.unmarshal", duration_us=elapsed, detail=_message_detail(message, len(data)))
        return message

    def _write(self, message: Any) -> bytes:
        """Sérialiseur enveloppé : ce qu'il rend est ce que gRPC écrit sur le fil."""
        started = time.perf_counter_ns()
        data = self._serialize(message)
        elapsed = _us(started)
        descriptor = _descriptor_of(message)
        if self._handler.response_streaming:
            tally = self._outgoing
            tally.add(len(data), elapsed)
            self._emit("server.stream_item", duration_us=elapsed, payload=data,
                       detail=_item_detail("response", tally.count, data, descriptor))
        else:
            self._emit("server.marshal", duration_us=elapsed, payload=data, detail=_message_detail(message, len(data)))
            framed, framing_us, detail = _framed(data, descriptor)
            self._emit("server.send", duration_us=framing_us, payload=framed, detail={
                "http2_headers": _RESPONSE_HEADERS, "http2_trailers": _OK_TRAILERS, "note": _TRANSPORT_NOTE, **detail,
            })
        return data

    def _requests(self, iterator: Iterable[Any]) -> Iterator[Any]:
        """Flux de requêtes : bilan cumulé publié quand le client a fini d'émettre."""
        yield from iterator
        tally = self._incoming
        self._emit("server.receive", size=tally.framed_size,
                   detail={"messages": tally.count, "http2_headers": self._headers})
        self._emit("server.unmarshal", duration_us=tally.spent_us, detail={"messages": tally.count})

    def _dispatch(self, behavior: Callable, request: Any) -> Any:
        """Publie ``server.dispatch`` et rend l'argument à passer à la procédure."""
        streaming = self._handler.request_streaming
        bound = {"request_iterator": "flux de requêtes"} if streaming else {"request": message_preview(request)}
        self._emit("server.dispatch", duration_us=self._lookup_us, detail={
            "target": getattr(behavior, "__qualname__", repr(behavior)),
            "rpc": self._path,
            "bound_args": bound,
        })
        return self._requests(request) if streaming else request

    def _single(self, behavior: Callable) -> Callable:
        def traced(request: Any, context: grpc.ServicerContext) -> Any:
            argument = self._dispatch(behavior, request)
            started = time.perf_counter_ns()
            try:
                response = behavior(argument, context)
            except Exception:
                self._failed(started, context)
                raise
            self._emit("server.execute", duration_us=_us(started))
            return response

        return traced

    def _streaming(self, behavior: Callable) -> Callable:
        def traced(request: Any, context: grpc.ServicerContext) -> Iterator[Any]:
            argument = self._dispatch(behavior, request)
            started = time.perf_counter_ns()
            try:
                # gRPC sérialise chaque élément (``_write``) avant de redemander le suivant.
                yield from behavior(argument, context)
            except Exception:
                self._failed(started, context)
                raise
            tally = self._outgoing
            self._emit("server.execute", duration_us=_us(started), detail={"messages": tally.count})
            self._emit("server.marshal", duration_us=tally.spent_us, size=tally.size, detail={"messages": tally.count})
            self._emit("server.send", size=tally.framed_size, detail={
                "messages": tally.count, "http2_trailers": _OK_TRAILERS, "note": _TRANSPORT_NOTE,
            })

        return traced

    def _failed(self, started: int, context: grpc.ServicerContext) -> None:
        """La procédure a échoué : le statut posé par ``context.abort`` part dans les trailers."""
        self._emit("server.execute", duration_us=_us(started), detail={"failed": True})
        if context.code() is None and not context.is_active():
            # Aucun statut posé et plus personne en face : c'est le client qui a mis fin à l'appel.
            self._error(grpc.StatusCode.CANCELLED, _CLIENT_GONE, answered=False)
            return
        details = context.details()
        message = details.decode("utf-8", errors="replace") if isinstance(details, bytes) else (details or "")
        self._error(context.code() or grpc.StatusCode.UNKNOWN, message)

    def _error(self, status: grpc.StatusCode, message: str, *, answered: bool = True) -> None:
        _emit_server_error(self._bus, self._call_id, self._method, status, message, answered=answered)


def _emit_server_error(
    bus: EventBus, call_id: str, method: str, status: grpc.StatusCode, message: str, *, answered: bool = True
) -> None:
    """Publie ``server.error`` ; ``answered=False`` quand aucun trailer ne part (le client n'écoute plus)."""
    detail: dict[str, Any] = {
        "code": GRPC_TO_CANONICAL.get(status.name, INTERNAL),
        "grpc_status": status.name,
        "message": message,
    }
    if answered:
        detail["http2_trailers"] = {"grpc-status": str(status.value[0]), "grpc-message": message}
    bus.emit(call_id=call_id, protocol=PROTOCOL, side="server", stage="server.error", method=method, detail=detail)


class TracingServerInterceptor(grpc.ServerInterceptor):
    """Intercepteur serveur : capture les octets réellement reçus et émis par chaque appel.

    L'appel n'est tracé que si le bus est actif ET si le client a transmis un
    ``x-call-id`` ; sinon le gestionnaire d'origine est rendu intact (surcoût nul).
    """

    def __init__(self, bus: EventBus = BUS) -> None:
        self._bus = bus

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler | None],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        bus = self._bus
        if not bus.enabled:
            return continuation(handler_call_details)
        # La résolution « chemin HTTP/2 → gestionnaire » EST le dispatch de gRPC : on la chronomètre.
        started = time.perf_counter_ns()
        handler = continuation(handler_call_details)
        lookup_us = _us(started)
        metadata = _printable(handler_call_details.invocation_metadata or ())
        call_id = metadata.get(CALL_ID_KEY)
        if not call_id:
            return handler
        path = handler_call_details.method
        if handler is None:
            _emit_server_error(bus, call_id, procedure_name(path), grpc.StatusCode.UNIMPLEMENTED,
                               f"Méthode inconnue du serveur : {path}")
            return None
        headers = {":method": "POST", ":path": path, "content-type": "application/grpc", **metadata}
        return _ServerTrace(bus, call_id, path, headers, lookup_us, handler).traced_handler()
