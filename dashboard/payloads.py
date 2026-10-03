"""Entrées et sorties JSON de l'API : lecture validée des requêtes, réponses, erreurs.

L'API ne s'appuie pas sur des modèles déclaratifs : chaque corps de requête est lu champ par
champ par ``Fields``, ce qui permet d'expliquer toute anomalie en français — ces messages
sont affichés tels quels par l'interface. Une erreur d'API a toujours la forme
``{"error": {"code", "message"}}``.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import PurePath
from typing import Any, Callable, Iterable

from starlette.requests import Request
from starlette.responses import Response

INVALID_ARGUMENT = "INVALID_ARGUMENT"
NOT_FOUND = "NOT_FOUND"
BUSY = "BUSY"
UNAVAILABLE = "UNAVAILABLE"
INTERNAL = "INTERNAL"

_ABSENT: Any = object()   # sentinelle : champ obligatoire, sans valeur par défaut


class ApiError(Exception):
    """Erreur renvoyée telle quelle au client HTTP : statut, code stable et message en français."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


def malformed(message: str) -> ApiError:
    """422 : le corps de la requête n'a pas la forme attendue par cette route."""
    return ApiError(422, INVALID_ARGUMENT, message)


def refused(message: str) -> ApiError:
    """400 : la requête est bien formée, mais le laboratoire refuse ses valeurs."""
    return ApiError(400, INVALID_ARGUMENT, message)


def not_found(message: str) -> ApiError:
    return ApiError(404, NOT_FOUND, message)


# --- Sérialisation -------------------------------------------------------------------

def _fallback(value: Any) -> Any:
    """Types que ``json`` ne connaît pas : un détail de trace ne doit jamais faire échouer une réponse."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if isinstance(value, PurePath):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return repr(value)


def to_json(value: Any) -> str:
    """JSON compact en UTF-8. ``allow_nan=False`` : un NaN serait illisible pour un navigateur."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=_fallback)


def reply(content: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    """Réponse JSON construite directement : aucun ré-encodage intermédiaire des gros résultats."""
    return Response(to_json(content), status, headers, media_type="application/json")


# --- Lecture des requêtes ------------------------------------------------------------

def _reject_constant(name: str) -> Any:
    raise ValueError(f"constante {name} non représentable en JSON strict")


async def read_object(request: Request) -> dict[str, Any]:
    """Corps JSON de la requête, obligatoirement un objet ; un corps vide vaut ``{}``."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        # NaN et Infinity sont refusés dès l'entrée : ils ne pourraient pas être renvoyés au navigateur.
        data = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, INVALID_ARGUMENT, f"Corps de requête illisible : JSON invalide ({exc}).") from None
    if not isinstance(data, dict):
        raise malformed("Le corps de la requête doit être un objet JSON.")
    return data


def query_int(request: Request, name: str, default: int, minimum: int, maximum: int) -> int:
    """Paramètre d'URL entier, ramené dans ``[minimum, maximum]`` (une borne dépassée n'est pas une erreur)."""
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise malformed(f"Le paramètre « {name} » doit être un entier (reçu : {raw!r}).") from None
    return max(minimum, min(maximum, value))


class Fields:
    """Corps d'une requête, lu champ par champ ; chaque anomalie devient un 422 explicite.

    Un champ absent ou ``null`` prend sa valeur par défaut. Un champ inconnu est refusé :
    une faute de frappe (``viaProxy``) ne doit pas passer pour un réglage ignoré.
    """

    def __init__(self, data: dict[str, Any], allowed: Iterable[str]) -> None:
        allowed = tuple(allowed)
        unknown = sorted(set(data) - set(allowed))
        if unknown:
            raise malformed(f"Champ(s) inconnu(s) : {', '.join(unknown)} (attendus : {', '.join(allowed)}).")
        self._data = data

    def _read(self, name: str, default: Any, valid: Callable[[Any], bool], expected: str) -> Any:
        """Valeur contrôlée du champ ; absent ou ``null``, il vaut ``default`` — ou manque, s'il est obligatoire."""
        value = self._data.get(name)
        if value is None:
            if default is _ABSENT:
                raise malformed(f"Le champ « {name} » est obligatoire.")
            return default
        if not valid(value):
            raise malformed(f"Le champ « {name} » doit être {expected} (reçu : {value!r}).")
        return value

    def text(self, name: str, default: Any = _ABSENT, *, choices: Iterable[str] | None = None) -> Any:
        if choices is None:
            return self._read(
                name, default, lambda value: isinstance(value, str) and bool(value), "une chaîne non vide"
            )
        accepted = tuple(choices)
        return self._read(
            name, default, lambda value: isinstance(value, str) and value in accepted,
            f"l'une des valeurs {', '.join(accepted)}",
        )

    def integer(self, name: str, default: Any, minimum: int, maximum: int) -> Any:
        return self._read(
            name, default, lambda value: _is_number(value, int) and minimum <= value <= maximum,
            f"un entier compris entre {minimum} et {maximum}",
        )

    def number(self, name: str, default: Any, minimum: float, maximum: float) -> Any:
        return self._read(
            name, default, lambda value: _is_number(value, (int, float)) and minimum <= value <= maximum,
            f"un nombre compris entre {minimum:g} et {maximum:g}",
        )

    def boolean(self, name: str, default: Any) -> Any:
        return self._read(name, default, lambda value: isinstance(value, bool), "un booléen")

    def mapping(self, name: str, default: Any = _ABSENT) -> Any:
        return self._read(name, default, lambda value: isinstance(value, dict), "un objet JSON")

    def sequence(self, name: str, default: Any = _ABSENT) -> Any:
        return self._read(name, default, lambda value: isinstance(value, list), "une liste")


def _is_number(value: Any, kinds: type | tuple[type, ...]) -> bool:
    # bool est un sous-type d'int : « count: true » passerait pour 1 sans ce garde-fou.
    return isinstance(value, kinds) and not isinstance(value, bool)
