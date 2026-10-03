"""JSON-RPC 2.0 sur TCP : les messages et leur tramage.

Ce module ne connaît ni le stub ni le squelette : il décrit seulement ce qui
circule sur le fil, en deux couches indépendantes.

1. LE MESSAGE — un objet JSON-RPC 2.0 ::

       requête       {"jsonrpc":"2.0","method":"update_stock","params":{…},"id":"custom-000042"}
       notification  {"jsonrpc":"2.0","method":"log_event","params":[…]}      pas d'« id » : pas de réponse
       réponse       {"jsonrpc":"2.0","result":{…},"id":"custom-000042"}
       erreur        {"jsonrpc":"2.0","error":{"code":-32601,"message":"…"},"id":"custom-000042"}
       lot           [requête, notification, …]   →   [réponse, …]

2. LA TRAME — TCP transporte un flux d'octets sans frontières : un ``recv``
   peut rendre un demi-message, ou trois messages collés. Chaque message est
   donc précédé de sa longueur ::

       +---------------------------+----------------------------------------+
       | longueur : 4 octets       | message JSON, UTF-8, compact           |
       | (uint32, big-endian)      | (exactement « longueur » octets)       |
       +---------------------------+----------------------------------------+

Toutes les fonctions de ce module signalent un problème par ``RpcProtocolError``.
"""
from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass
from typing import Any, Iterable, Union

from common import errors
from common.config import MAX_FRAME_BYTES
from common.errors import DomainError, RpcError, RpcProtocolError

JSONRPC_VERSION = "2.0"

# --- Codes d'erreur JSON-RPC 2.0 ---------------------------------------------

PARSE_ERROR = -32700          # le message n'est pas du JSON
INVALID_REQUEST = -32600      # du JSON valide, mais pas un message JSON-RPC 2.0
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_ERROR = -32000         # plage -32000..-32099 : erreurs définies par le serveur
NOT_FOUND = -32001
FAILED_PRECONDITION = -32002

# --- Extensions (la spécification réserve le préfixe « rpc. ») ----------------

RESERVED_PREFIX = "rpc."
DISCOVER_METHOD = "rpc.discover"
STREAM_ITEM_METHOD = "rpc.stream.item"

RequestId = Union[str, int, float, None]
Params = Union[list[Any], dict[str, Any], None]


# --- Messages ----------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Request:
    """Appel de procédure : un nom et des paramètres positionnels OU nommés.

    Une notification (``is_notification``) part sans membre ``id`` : le serveur
    l'exécute mais ne répond jamais, pas même par une erreur. À ne pas confondre
    avec ``id=None`` seul, qui émet ``"id": null`` — légal, mais déconseillé.
    """

    method: str
    params: Params = None
    id: RequestId = None
    is_notification: bool = False


@dataclass(frozen=True, slots=True)
class ErrorObject:
    """Membre ``error`` d'une réponse ; ``data`` est libre (ici : le code canonique)."""

    code: int
    message: str
    data: Any = None


@dataclass(frozen=True, slots=True)
class Response:
    """Réponse à la requête de même ``id`` : soit ``result``, soit ``error``."""

    id: RequestId
    result: Any = None
    error: ErrorObject | None = None


Message = Union[Request, Response]


# --- Marshalling : objets Python → octets --------------------------------------

def encode_request(request: Request) -> bytes:
    return _dumps(_to_document(request))


def encode_response(response: Response) -> bytes:
    return _dumps(_to_document(response))


def encode_batch(messages: Iterable[Message]) -> bytes:
    """Un lot est un simple tableau JSON de messages."""
    return join_batch(_dumps(_to_document(message)) for message in messages)


def join_batch(bodies: Iterable[bytes]) -> bytes:
    """Assemble en lot des messages déjà sérialisés (le serveur sérialise chaque réponse à part)."""
    return _checked_size(b"[" + b",".join(bodies) + b"]")


def _to_document(message: Message) -> dict[str, Any]:
    if isinstance(message, Request):
        document: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": message.method}
        if message.params is not None:
            document["params"] = message.params
        if not message.is_notification:
            document["id"] = message.id
        return document
    if message.error is None:
        return {"jsonrpc": JSONRPC_VERSION, "result": message.result, "id": message.id}
    error: dict[str, Any] = {"code": message.error.code, "message": message.error.message}
    if message.error.data is not None:
        error["data"] = message.error.data
    return {"jsonrpc": JSONRPC_VERSION, "error": error, "id": message.id}


def _dumps(document: Any) -> bytes:
    # Compact et en UTF-8 direct : chaque octet économisé l'est sur CHAQUE appel.
    try:
        text = json.dumps(document, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        body = text.encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RpcProtocolError(f"Valeur non représentable en JSON : {exc}") from exc
    return _checked_size(body)


def _checked_size(body: bytes) -> bytes:
    # Vérifié dès la sérialisation : tout message encodé tient dans une trame.
    if len(body) > MAX_FRAME_BYTES:
        raise RpcProtocolError(f"Message trop volumineux : {len(body)} octets (maximum {MAX_FRAME_BYTES})")
    return body


# --- Démarshalling : octets → objets Python ------------------------------------

def decode_message(data: bytes) -> Message | list[Message]:
    """Décode un message ou un lot ; lève ``RpcProtocolError`` si le contenu est hors norme."""
    document = parse_document(data)
    if not isinstance(document, list):
        return build_message(document)
    if not document:
        raise _invalid("un lot ne peut pas être vide")
    return [build_message(item) for item in document]


def parse_document(data: bytes) -> Any:
    """Première moitié du démarshalling : octets → document JSON (échec = « Parse error »)."""
    try:
        return json.loads(data)
    except (ValueError, RecursionError) as exc:
        where = f" : erreur de syntaxe au caractère {exc.pos}" if isinstance(exc, json.JSONDecodeError) else ""
        raise RpcProtocolError(f"JSON invalide{where}", detail={"jsonrpc_code": PARSE_ERROR}) from exc


def build_message(document: Any) -> Message:
    """Seconde moitié : document JSON → message validé (échec = « Invalid Request »)."""
    if not isinstance(document, dict):
        raise _invalid("un message est un objet JSON")
    if document.get("jsonrpc") != JSONRPC_VERSION:
        raise _invalid('le membre "jsonrpc" doit valoir "2.0"')
    if "method" in document:
        return _build_request(document)
    if "result" in document or "error" in document:
        return _build_response(document)
    raise _invalid("ni requête (method) ni réponse (result / error)")


def _build_request(document: dict[str, Any]) -> Request:
    method, params = document["method"], document.get("params")
    if not isinstance(method, str):
        raise _invalid('"method" doit être une chaîne')
    if "params" in document and not isinstance(params, (list, dict)):
        raise _invalid('"params" doit être un tableau (positionnels) ou un objet (nommés)')
    if "id" not in document:
        return Request(method, params, is_notification=True)
    return Request(method, params, _checked_id(document["id"]))


def _build_response(document: dict[str, Any]) -> Response:
    if "id" not in document:
        raise _invalid('une réponse porte toujours un membre "id"')
    if ("result" in document) == ("error" in document):
        raise _invalid('une réponse porte soit "result", soit "error"')
    request_id = _checked_id(document["id"])
    if "result" in document:
        return Response(request_id, result=document["result"])
    error = document["error"]
    if not isinstance(error, dict):
        raise _invalid('"error" doit être un objet')
    code, message = error.get("code"), error.get("message")
    if not isinstance(code, int) or isinstance(code, bool) or not isinstance(message, str):
        raise _invalid('"error" exige un "code" entier et un "message" textuel')
    return Response(request_id, error=ErrorObject(code, message, error.get("data")))


def _checked_id(value: Any) -> RequestId:
    # bool est un sous-type d'int en Python, mais « true » n'est pas un identifiant JSON-RPC.
    if value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool)):
        return value
    raise _invalid('"id" doit être une chaîne, un nombre ou null')


def _invalid(reason: str) -> RpcProtocolError:
    return RpcProtocolError(f"Message JSON-RPC invalide : {reason}", detail={"jsonrpc_code": INVALID_REQUEST})


# --- Tramage : délimiter les messages dans le flux TCP ---------------------------

_HEADER = struct.Struct(">I")
HEADER_SIZE = _HEADER.size


def frame(body: bytes) -> bytes:
    """Préfixe ``body`` de sa longueur. En-tête et corps forment UN bloc, écrit par un seul ``sendall``."""
    return _HEADER.pack(len(_checked_size(body))) + body


def read_frame(sock: socket.socket) -> bytes | None:
    """Lit une trame complète et renvoie son corps ; ``None`` si le pair a fermé entre deux trames."""
    header = _recv_exactly(sock, HEADER_SIZE)
    if not header:
        return None
    if len(header) < HEADER_SIZE:
        raise RpcProtocolError("Connexion fermée au milieu de l'en-tête d'une trame")
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME_BYTES:
        # On refuse AVANT de lire : la longueur annoncée ne doit pas dicter la mémoire allouée.
        raise RpcProtocolError(f"Trame annoncée trop volumineuse : {length} octets (maximum {MAX_FRAME_BYTES})")
    body = _recv_exactly(sock, length)
    if len(body) < length:
        raise RpcProtocolError(f"Connexion fermée au milieu d'une trame ({len(body)} octets reçus sur {length})")
    return body


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    """Lit ``count`` octets — moins seulement si le pair ferme la connexion.

    ``recv`` rend ce qui est disponible, pas ce qu'on demande : il faut boucler.
    """
    buffer = bytearray(count)
    view = memoryview(buffer)
    received = 0
    while received < count:
        chunk = sock.recv_into(view[received:])
        if chunk == 0:
            break
        received += chunk
    return bytes(view[:received])


def describe_frame(data: bytes) -> dict[str, Any]:
    """Découpage d'une trame pour la vue hexadécimale annotée (``detail`` des évènements de trace)."""
    return {
        "frame_header_hex": data[:HEADER_SIZE].hex(),
        "segments": [
            {"label": "Longueur (uint32 big-endian)", "start": 0, "end": HEADER_SIZE, "kind": "frame",
             "value": len(data) - HEADER_SIZE},
            {"label": "Message JSON-RPC 2.0", "start": HEADER_SIZE, "end": len(data), "kind": "body"},
        ],
    }


# --- Extension « flux serveur » ------------------------------------------------

def stream_item(request_id: RequestId, seq: int, item: Any) -> Request:
    """Notification portant le ``seq``-ième élément (à partir de 1) du flux ouvert par ``request_id``."""
    return Request(STREAM_ITEM_METHOD, {"id": request_id, "seq": seq, "item": item}, is_notification=True)


def parse_stream_item(notification: Request) -> tuple[RequestId, int, Any]:
    """Inverse de ``stream_item`` : renvoie ``(id de la requête, seq, élément)``."""
    params = notification.params
    if not isinstance(params, dict) or not {"id", "seq", "item"} <= params.keys():
        raise _invalid(f"{STREAM_ITEM_METHOD} exige les paramètres id, seq et item")
    return _checked_id(params["id"]), params["seq"], params["item"]


def stream_end(count: int) -> dict[str, Any]:
    """Résultat de la réponse finale d'un flux : il n'y aura plus d'élément."""
    return {"stream": "end", "count": count}


def is_stream_end(result: Any) -> bool:
    return isinstance(result, dict) and result.keys() == {"stream", "count"} and result["stream"] == "end"


# --- Erreurs : exception ⇄ objet erreur ------------------------------------------

def exception_to_error(exc: BaseException) -> ErrorObject:
    """Côté serveur : traduit une exception en objet erreur JSON-RPC.

    Une erreur métier voyage avec son message et ses données. Toute autre
    exception devient une erreur interne anonyme : la pile d'appels et le
    message d'origine restent sur le serveur.
    """
    if isinstance(exc, DomainError):
        code = errors.JSONRPC_CODES.get(exc.code, SERVER_ERROR)
        return ErrorObject(code, exc.message, {"canonical": exc.code, **exc.data})
    return ErrorObject(INTERNAL_ERROR, "Erreur interne du serveur", {"canonical": errors.INTERNAL})


def error_to_exception(error: ErrorObject, *, protocol: str = "custom", method: str = "") -> RpcError:
    """Côté client : traduit un objet erreur en exception de ``common.errors``.

    Le code canonique placé dans ``error.data`` par notre squelette prime ; face
    à un serveur JSON-RPC quelconque, on se rabat sur le code numérique standard.
    """
    data = error.data if isinstance(error.data, dict) else {}
    canonical = data.get("canonical")
    if not isinstance(canonical, str):
        canonical = errors.JSONRPC_TO_CANONICAL.get(error.code, errors.INTERNAL)
    detail = {"jsonrpc_code": error.code, **{key: value for key, value in data.items() if key != "canonical"}}
    if error.data is not None and not isinstance(error.data, dict):
        detail["data"] = error.data
    return errors.error_from_code(canonical, error.message, protocol=protocol, method=method, detail=detail)
