"""Télémétrie « sous le capot » : chaque étape d'un appel RPC émet un évènement.

Le bus est un simple publish/subscribe en mémoire. Les stubs, squelettes,
intercepteurs et proxys y publient ; le CLI et le dashboard s'y abonnent pour
afficher, en temps réel, les messages bruts qui transitent sur le réseau.

Coût quasi nul quand il est coupé : les émetteurs testent ``BUS.enabled`` avant
de construire quoi que ce soit, et les benchmarks de latence tournent sous
``BUS.muted()`` pour ne pas mesurer l'instrumentation.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .config import MAX_PAYLOAD_CAPTURE

# Étapes canoniques d'un appel unaire, dans l'ordre où elles se produisent.
PIPELINE: tuple[str, ...] = (
    "client.call",
    "client.marshal",
    "client.send",
    "server.receive",
    "server.unmarshal",
    "server.dispatch",
    "server.execute",
    "server.marshal",
    "server.send",
    "client.receive",
    "client.unmarshal",
    "client.return",
)

# Étapes hors pipeline nominal.
TERMINAL_STAGES: tuple[str, ...] = ("client.return", "client.error")
EXTRA_STAGES: tuple[str, ...] = (
    "client.error",
    "client.stream_item",
    "server.stream_item",
    "server.error",
    "network.delay",
    "network.reset",
    "network.blackhole",
    "network.refuse",
    "network.lost_reply",
    "resilience.attempt",
    "resilience.backoff",
    "resilience.breaker",
    "resilience.dedup",
    "resilience.give_up",
)

# Libellé + explication pédagogique de chaque étape (affichés par le CLI et le dashboard).
STAGE_INFO: dict[str, dict[str, str]] = {
    "client.call": {
        "label": "Appel du stub",
        "role": "Stub client",
        "text": "Le code applicatif appelle une méthode du stub exactement comme une fonction locale.",
    },
    "client.marshal": {
        "label": "Marshalling",
        "role": "Stub client",
        "text": "Le stub sérialise le nom de la procédure et ses arguments en une suite d’octets.",
    },
    "client.send": {
        "label": "Envoi",
        "role": "Transport",
        "text": "Le message est encadré (framing) puis écrit sur la connexion réseau.",
    },
    "server.receive": {
        "label": "Réception",
        "role": "Transport",
        "text": "Le serveur lit une trame complète sur la socket.",
    },
    "server.unmarshal": {
        "label": "Démarshalling",
        "role": "Squelette serveur",
        "text": "Les octets sont décodés : on retrouve le nom de la procédure et ses arguments.",
    },
    "server.dispatch": {
        "label": "Dispatch",
        "role": "Squelette serveur",
        "text": "Le dispatcher retrouve la procédure enregistrée et lui lie les arguments reçus.",
    },
    "server.execute": {
        "label": "Exécution",
        "role": "Procédure métier",
        "text": "La procédure s’exécute réellement — sur le serveur, dans un autre espace mémoire.",
    },
    "server.marshal": {
        "label": "Marshalling du résultat",
        "role": "Squelette serveur",
        "text": "Le résultat (ou l’erreur) est sérialisé à son tour.",
    },
    "server.send": {
        "label": "Envoi de la réponse",
        "role": "Transport",
        "text": "La trame de réponse repart vers le client.",
    },
    "client.receive": {
        "label": "Réception de la réponse",
        "role": "Transport",
        "text": "Le stub lit la trame de réponse et la rattache à l’appel en attente via son identifiant.",
    },
    "client.unmarshal": {
        "label": "Démarshalling du résultat",
        "role": "Stub client",
        "text": "Les octets redeviennent une valeur du langage (ou une exception).",
    },
    "client.return": {
        "label": "Retour à l’appelant",
        "role": "Stub client",
        "text": "Le stub rend le résultat : pour l’appelant, tout s’est passé « comme en local ».",
    },
    "client.error": {
        "label": "Erreur remontée",
        "role": "Stub client",
        "text": "L’appel échoue : timeout, panne réseau ou erreur renvoyée par le serveur.",
    },
}


@dataclass(slots=True)
class TraceEvent:
    """Un fait observé pendant un appel RPC."""

    seq: int                # numéro d'ordre global (croissant)
    ts: float               # horodatage epoch (secondes)
    t_ns: int               # horloge monotone (perf_counter_ns) pour les durées
    call_id: str            # identifiant de corrélation, identique côté client et serveur
    protocol: str           # local | custom | grpc | rest
    side: str               # client | server | network | resilience
    stage: str              # ex. "client.marshal"
    method: str = ""
    duration_us: float | None = None  # durée propre de l'étape (µs)
    size: int | None = None           # taille en octets du message à cette étape
    payload: bytes | None = None      # octets bruts (tronqués à MAX_PAYLOAD_CAPTURE)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_payload: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "seq": self.seq,
            "ts": self.ts,
            "t_ns": self.t_ns,
            "call_id": self.call_id,
            "protocol": self.protocol,
            "side": self.side,
            "stage": self.stage,
            "method": self.method,
            "duration_us": self.duration_us,
            "size": self.size,
            "detail": self.detail,
        }
        if include_payload and self.payload is not None:
            data["payload_hex"] = self.payload.hex()
            data["payload_text"] = printable_text(self.payload)
        return data


Subscriber = Callable[[TraceEvent], None]


class EventBus:
    """Bus d'évènements en mémoire, sûr vis-à-vis des threads."""

    def __init__(self, history: int = 4000) -> None:
        self.enabled = True
        self._subscribers: list[Subscriber] = []
        self._history: deque[TraceEvent] = deque(maxlen=history)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._mute_depth = 0

    def emit(
        self,
        *,
        call_id: str,
        protocol: str,
        side: str,
        stage: str,
        method: str = "",
        duration_us: float | None = None,
        size: int | None = None,
        payload: bytes | None = None,
        detail: dict[str, Any] | None = None,
    ) -> TraceEvent | None:
        """Publie un évènement. Ne fait rien (et renvoie ``None``) si le bus est coupé."""
        if not self.enabled:
            return None
        detail = dict(detail) if detail else {}
        if payload is not None:
            payload = bytes(payload)
            if size is None:
                size = len(payload)
            if len(payload) > MAX_PAYLOAD_CAPTURE:
                payload = payload[:MAX_PAYLOAD_CAPTURE]
                detail["payload_truncated"] = True
        event = TraceEvent(
            seq=next(self._seq),
            ts=time.time(),
            t_ns=time.perf_counter_ns(),
            call_id=call_id,
            protocol=protocol,
            side=side,
            stage=stage,
            method=method,
            duration_us=duration_us,
            size=size,
            payload=payload,
            detail=detail,
        )
        self._history.append(event)
        for callback in tuple(self._subscribers):
            try:
                callback(event)
            except Exception:  # un abonné défaillant ne doit jamais casser un appel RPC
                pass
        return event

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Abonne ``callback`` ; renvoie la fonction de désabonnement."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def recent(self, limit: int = 200) -> list[TraceEvent]:
        return list(self._history)[-limit:]

    def clear(self) -> None:
        self._history.clear()

    @contextmanager
    def muted(self) -> Iterator[None]:
        """Coupe le bus le temps d'un bloc (benchmarks) ; ré-entrant."""
        with self._lock:
            self._mute_depth += 1
            self.enabled = False
        try:
            yield
        finally:
            with self._lock:
                self._mute_depth -= 1
                if self._mute_depth == 0:
                    self.enabled = True


BUS = EventBus()

_call_counter = itertools.count(1)


def new_call_id(protocol: str = "call") -> str:
    """Identifiant de corrélation unique dans le processus, ex. ``grpc-000042``."""
    return f"{protocol}-{next(_call_counter):06d}"


# --- Agrégation par appel ----------------------------------------------------

class CallTrace:
    """Tous les évènements d'un même appel, dans l'ordre d'émission."""

    __slots__ = ("call_id", "protocol", "method", "events")

    def __init__(self, call_id: str, protocol: str, method: str) -> None:
        self.call_id = call_id
        self.protocol = protocol
        self.method = method
        self.events: list[TraceEvent] = []

    def _first(self, stage: str) -> TraceEvent | None:
        return next((e for e in self.events if e.stage == stage), None)

    @property
    def terminal(self) -> TraceEvent | None:
        return next((e for e in reversed(self.events) if e.stage in TERMINAL_STAGES), None)

    @property
    def complete(self) -> bool:
        return self.terminal is not None

    def summary(self) -> dict[str, Any]:
        terminal = self.terminal
        sent, received, executed = (
            self._first("client.send"),
            self._first("client.receive"),
            self._first("server.execute"),
        )
        if terminal is None:
            status, error = "pending", None
        elif terminal.stage == "client.return":
            status, error = "ok", None
        else:
            status = "error"
            error = {
                "code": terminal.detail.get("code", "INTERNAL"),
                "message": terminal.detail.get("message", ""),
            }
        return {
            "call_id": self.call_id,
            "protocol": self.protocol,
            "method": self.method,
            "status": status,
            "error": error,
            "started_at": self.events[0].ts if self.events else None,
            "duration_us": terminal.duration_us if terminal else None,
            "request_bytes": sent.size if sent else None,
            "response_bytes": received.size if received else None,
            "server_us": executed.duration_us if executed else None,
            "events": len(self.events),
        }

    def to_dict(self, include_payload: bool = True) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "events": [e.to_dict(include_payload) for e in self.events],
        }


class TraceCollector:
    """S'abonne au bus et regroupe les évènements par ``call_id``."""

    def __init__(self, bus: EventBus = BUS, max_calls: int = 400) -> None:
        self._bus = bus
        self._max = max_calls
        self._calls: OrderedDict[str, CallTrace] = OrderedDict()
        self._lock = threading.Lock()
        self._unsubscribe: Callable[[], None] | None = None
        self._listeners: list[Callable[[CallTrace], None]] = []
        self.totals: dict[str, dict[str, float]] = {}

    def start(self) -> "TraceCollector":
        if self._unsubscribe is None:
            self._unsubscribe = self._bus.subscribe(self._on_event)
        return self

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def on_call_complete(self, listener: Callable[[CallTrace], None]) -> None:
        """``listener`` est appelé à chaque appel terminé (succès ou erreur)."""
        self._listeners.append(listener)

    def _on_event(self, event: TraceEvent) -> None:
        if not event.call_id:
            return
        finished: CallTrace | None = None
        with self._lock:
            trace = self._calls.get(event.call_id)
            if trace is None:
                trace = CallTrace(event.call_id, event.protocol, event.method)
                self._calls[event.call_id] = trace
                while len(self._calls) > self._max:
                    self._calls.popitem(last=False)
            if event.method and not trace.method:
                trace.method = event.method
            trace.events.append(event)
            if event.side == "client" and event.stage in TERMINAL_STAGES:
                finished = trace
                self._account(trace)
        if finished is not None:
            for listener in tuple(self._listeners):
                try:
                    listener(finished)
                except Exception:
                    pass

    def _account(self, trace: CallTrace) -> None:
        summary = trace.summary()
        totals = self.totals.setdefault(
            trace.protocol,
            {"calls": 0, "errors": 0, "bytes_out": 0, "bytes_in": 0, "total_us": 0.0},
        )
        totals["calls"] += 1
        totals["errors"] += summary["status"] == "error"
        totals["bytes_out"] += summary["request_bytes"] or 0
        totals["bytes_in"] += summary["response_bytes"] or 0
        totals["total_us"] += summary["duration_us"] or 0.0

    def get(self, call_id: str) -> CallTrace | None:
        with self._lock:
            return self._calls.get(call_id)

    def recent(self, limit: int = 50) -> list[CallTrace]:
        with self._lock:
            return list(self._calls.values())[-limit:][::-1]

    def clear(self) -> None:
        with self._lock:
            self._calls.clear()
            self.totals.clear()


# --- Utilitaires d'affichage -------------------------------------------------

def printable_text(data: bytes) -> str | None:
    """Renvoie le texte UTF-8 si ``data`` est lisible, sinon ``None`` (binaire)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
    return text if text and printable / len(text) > 0.95 else None


def hexdump(data: bytes, width: int = 16, limit: int | None = None) -> str:
    """Vue hexadécimale classique : offset, octets, puis rendu ASCII."""
    if limit is not None:
        data = data[:limit]
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hexa = " ".join(f"{byte:02x}" for byte in chunk)
        text = "".join(chr(byte) if 32 <= byte < 127 else "·" for byte in chunk)
        lines.append(f"{offset:06x}  {hexa:<{width * 3 - 1}}  {text}")
    return "\n".join(lines)
