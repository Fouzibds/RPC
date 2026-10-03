"""Hiérarchie d'erreurs commune aux trois middlewares.

Deux familles bien distinctes :

* ``DomainError``  — levée CÔTÉ SERVEUR par le service métier (produit inconnu,
  stock insuffisant…). Chaque middleware la traduit dans son propre vocabulaire
  (code JSON-RPC, statut gRPC, statut HTTP).
* ``RpcError``     — levée CÔTÉ CLIENT par un stub. C'est elle qui matérialise
  la différence entre un appel local et un appel distant : ``RpcTimeoutError``
  et ``NetworkError`` n'existent tout simplement pas pour une fonction locale.

Le champ ``code`` est un code canonique, identique quel que soit le protocole,
ce qui permet de comparer les comportements à l'identique.
"""
from __future__ import annotations

from typing import Any

# --- Codes canoniques --------------------------------------------------------

OK = "OK"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
NOT_FOUND = "NOT_FOUND"
FAILED_PRECONDITION = "FAILED_PRECONDITION"
METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
INTERNAL = "INTERNAL"
TIMEOUT = "TIMEOUT"
UNAVAILABLE = "UNAVAILABLE"
PROTOCOL = "PROTOCOL"
CIRCUIT_OPEN = "CIRCUIT_OPEN"
CANCELLED = "CANCELLED"

# Traductions par protocole (code canonique <-> vocabulaire du middleware).
JSONRPC_CODES: dict[str, int] = {
    PROTOCOL: -32700,             # Parse error / Invalid Request (-32600)
    METHOD_NOT_FOUND: -32601,
    INVALID_ARGUMENT: -32602,
    INTERNAL: -32603,
    NOT_FOUND: -32001,            # plage -32000..-32099 : erreurs définies par le serveur
    FAILED_PRECONDITION: -32002,
}
JSONRPC_TO_CANONICAL: dict[int, str] = {
    -32700: PROTOCOL,
    -32600: PROTOCOL,
    -32601: METHOD_NOT_FOUND,
    -32602: INVALID_ARGUMENT,
    -32603: INTERNAL,
    -32000: INTERNAL,
    -32001: NOT_FOUND,
    -32002: FAILED_PRECONDITION,
}

GRPC_STATUS: dict[str, str] = {
    INVALID_ARGUMENT: "INVALID_ARGUMENT",
    NOT_FOUND: "NOT_FOUND",
    FAILED_PRECONDITION: "FAILED_PRECONDITION",
    METHOD_NOT_FOUND: "UNIMPLEMENTED",
    INTERNAL: "INTERNAL",
    TIMEOUT: "DEADLINE_EXCEEDED",
    UNAVAILABLE: "UNAVAILABLE",
    CANCELLED: "CANCELLED",
}
GRPC_TO_CANONICAL: dict[str, str] = {
    "INVALID_ARGUMENT": INVALID_ARGUMENT,
    "OUT_OF_RANGE": INVALID_ARGUMENT,
    "NOT_FOUND": NOT_FOUND,
    "FAILED_PRECONDITION": FAILED_PRECONDITION,
    "UNIMPLEMENTED": METHOD_NOT_FOUND,
    "INTERNAL": INTERNAL,
    "UNKNOWN": INTERNAL,
    "DATA_LOSS": INTERNAL,
    "DEADLINE_EXCEEDED": TIMEOUT,
    "UNAVAILABLE": UNAVAILABLE,
    "RESOURCE_EXHAUSTED": UNAVAILABLE,
    "CANCELLED": CANCELLED,
}

HTTP_STATUS: dict[str, int] = {
    INVALID_ARGUMENT: 400,
    NOT_FOUND: 404,
    METHOD_NOT_FOUND: 404,
    FAILED_PRECONDITION: 409,
    INTERNAL: 500,
    UNAVAILABLE: 503,
    TIMEOUT: 504,
}
HTTP_TO_CANONICAL: dict[int, str] = {
    400: INVALID_ARGUMENT,
    404: METHOD_NOT_FOUND,   # route inconnue ; un 404 « ressource » porte son code dans le corps JSON
    405: METHOD_NOT_FOUND,
    409: FAILED_PRECONDITION,
    422: INVALID_ARGUMENT,
    500: INTERNAL,
    502: UNAVAILABLE,
    503: UNAVAILABLE,
    504: TIMEOUT,
}


# --- Erreurs métier (côté serveur) -------------------------------------------

class DomainError(Exception):
    """Erreur fonctionnelle levée par le service métier."""

    code = INTERNAL

    def __init__(self, message: str, **data: Any) -> None:
        super().__init__(message)
        self.message = message
        self.data = data


class InvalidArgument(DomainError):
    code = INVALID_ARGUMENT


class ProductNotFound(DomainError):
    code = NOT_FOUND


class InsufficientStock(DomainError):
    code = FAILED_PRECONDITION


# --- Erreurs d'appel distant (côté client) -----------------------------------

class RpcError(Exception):
    """Racine de toutes les erreurs qu'un stub peut lever."""

    code = INTERNAL
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        protocol: str = "",
        method: str = "",
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.protocol = protocol
        self.method = method
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "code": self.code,
            "message": self.message,
            "protocol": self.protocol,
            "method": self.method,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class RpcTransportError(RpcError):
    """Le réseau a échoué : connexion refusée, coupée, réinitialisée."""

    code = UNAVAILABLE
    retryable = True


NetworkError = RpcTransportError  # nom utilisé dans le cahier des charges


class RpcTimeoutError(RpcTransportError):
    """Pas de réponse dans le délai imparti — l'issue de l'appel est INCONNUE."""

    code = TIMEOUT


class RpcRemoteError(RpcError):
    """Le serveur a répondu… par une erreur."""


class MethodNotFoundError(RpcRemoteError):
    code = METHOD_NOT_FOUND


class InvalidArgumentError(RpcRemoteError):
    code = INVALID_ARGUMENT


class NotFoundError(RpcRemoteError):
    code = NOT_FOUND


class FailedPreconditionError(RpcRemoteError):
    code = FAILED_PRECONDITION


class InternalRemoteError(RpcRemoteError):
    code = INTERNAL


class RpcProtocolError(RpcError):
    """Message illisible : trame tronquée, JSON invalide, réponse hors contrat."""

    code = PROTOCOL


class CircuitOpenError(RpcError):
    """Le disjoncteur est ouvert : l'appel est refusé sans toucher au réseau."""

    code = CIRCUIT_OPEN


class RpcCancelledError(RpcError):
    code = CANCELLED


_BY_CODE: dict[str, type[RpcError]] = {
    INVALID_ARGUMENT: InvalidArgumentError,
    NOT_FOUND: NotFoundError,
    FAILED_PRECONDITION: FailedPreconditionError,
    METHOD_NOT_FOUND: MethodNotFoundError,
    INTERNAL: InternalRemoteError,
    TIMEOUT: RpcTimeoutError,
    UNAVAILABLE: RpcTransportError,
    PROTOCOL: RpcProtocolError,
    CIRCUIT_OPEN: CircuitOpenError,
    CANCELLED: RpcCancelledError,
}


def error_from_code(code: str, message: str, **kwargs: Any) -> RpcError:
    """Construit l'exception client correspondant à un code canonique."""
    return _BY_CODE.get(code, InternalRemoteError)(message, code=code, **kwargs)
