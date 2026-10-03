"""API REST / JSON de référence : le point de comparaison des deux middlewares RPC.

Ici, ni stub ni contrat : une procédure se désigne par un verbe et une URL, et
ses arguments se répartissent entre le chemin, la chaîne de requête et un corps
JSON. Ce module porte le vocabulaire commun au serveur (``rest_server``) et au
client (``rest_client``) : en-têtes, sérialisation JSON et télémétrie.
"""
from __future__ import annotations

import json
import time
from importlib import import_module
from typing import Any

from common.config import MAX_PAYLOAD_CAPTURE
from common.telemetry import EventBus, TraceEvent

PROTOCOL = "rest"
CALL_ID_HEADER = "X-Call-Id"                # propage l'identifiant de corrélation jusqu'au serveur
JSON_MEDIA_TYPE = "application/json"
NDJSON_MEDIA_TYPE = "application/x-ndjson"  # flux : un objet JSON par ligne

REQUEST_LINE = "Ligne de requête"
STATUS_LINE = "Ligne de statut"

_ENCODER = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))


def encode_json(value: Any) -> bytes:
    """JSON compact en UTF-8 : le même réglage que le JSON-RPC maison, pour comparer à armes égales."""
    return _ENCODER.encode(value).encode("utf-8")


def json_text(body: bytes) -> str:
    """Forme lisible d'un corps JSON, pour le champ ``detail.text`` des étapes de marshalling."""
    return body[:MAX_PAYLOAD_CAPTURE].decode("utf-8", "replace")


def http_segments(first_line: str, line_end: int, head_end: int, total: int) -> list[dict[str, Any]]:
    """Découpe un message HTTP/1.1 pour la vue hexadécimale colorée (plan §5).

    ``line_end`` et ``head_end`` sont les positions de fin de la première ligne
    et du bloc d'en-têtes (ligne vide comprise) ; le corps, s'il existe, va
    jusqu'à ``total``. Les segments couvrent le message sans trou.
    """
    segments: list[dict[str, Any]] = [
        {"label": first_line, "start": 0, "end": line_end, "kind": "header"},
        {"label": "En-têtes HTTP", "start": line_end, "end": head_end, "kind": "header"},
    ]
    if total > head_end:
        segments.append({"label": "Corps JSON", "start": head_end, "end": total, "kind": "body"})
    return segments


class StageTracer:
    """Publie les étapes d'un appel sur le bus en chronométrant chacune d'elles.

    Il n'est créé que si le bus est actif : bus coupé (benchmarks), un appel ne
    paie ni lecture d'horloge ni construction d'évènement.
    """

    __slots__ = ("_bus", "_side", "call_id", "method", "_origin", "_lap")

    def __init__(self, bus: EventBus, side: str, call_id: str = "", method: str = "") -> None:
        self._bus = bus
        self._side = side
        self.call_id = call_id
        self.method = method
        self._origin = self._lap = time.perf_counter_ns()

    def mark(self, stage: str, *, payload: bytes | None = None, detail: dict[str, Any] | None = None) -> None:
        """Étape sans durée propre : le chronomètre de l'étape suivante continue de tourner."""
        before = time.perf_counter_ns()
        self._publish(stage, None, payload, detail)
        self._lap += time.perf_counter_ns() - before

    def step(
        self, stage: str, *, payload: bytes | None = None, detail: dict[str, Any] | None = None
    ) -> TraceEvent | None:
        """Étape chronométrée : sa durée est le temps écoulé depuis l'étape précédente."""
        elapsed_us = (time.perf_counter_ns() - self._lap) / 1000
        event = self._publish(stage, elapsed_us, payload, detail)
        # Le temps passé à publier n'appartient à aucune étape de l'appel.
        self._lap = time.perf_counter_ns()
        return event

    def settle(self, announced: TraceEvent | None) -> None:
        """Complète une étape d'envoi publiée juste avant son écriture.

        Dès que les octets partent, l'autre côté peut publier leur réception :
        pour que la chronologie reste causale, l'envoi est annoncé avant
        l'écriture (durée de la mise en trame), puis sa durée est complétée ici.
        """
        now = time.perf_counter_ns()
        if announced is not None and announced.duration_us is not None:
            announced.duration_us += (now - self._lap) / 1000
        self._lap = now

    def finish(self, stage: str, *, detail: dict[str, Any] | None = None) -> None:
        """Étape terminale : sa durée est celle de l'appel entier."""
        total_us = (time.perf_counter_ns() - self._origin) / 1000
        self._publish(stage, total_us, None, detail)

    def _publish(
        self, stage: str, duration_us: float | None, payload: bytes | None, detail: dict[str, Any] | None
    ) -> TraceEvent | None:
        return self._bus.emit(
            call_id=self.call_id,
            protocol=PROTOCOL,
            side=self._side,
            stage=stage,
            method=self.method,
            duration_us=duration_us,
            payload=payload,
            detail=detail,
        )


_LAZY_EXPORTS = {
    "RestInventoryClient": "rest_client",
    "RestServerHandle": "rest_server",
    "create_rest_server": "rest_server",
}

__all__ = [
    "CALL_ID_HEADER",
    "JSON_MEDIA_TYPE",
    "NDJSON_MEDIA_TYPE",
    "PROTOCOL",
    "RestInventoryClient",
    "RestServerHandle",
    "create_rest_server",
]


def __getattr__(name: str) -> Any:
    """Exports paresseux : importer le paquet ne charge ni le serveur ni le client.

    Un import direct ferait charger ``rest_server`` deux fois lors de
    ``python -m rest_api.rest_server``.
    """
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"{__name__}.{module}"), name)
