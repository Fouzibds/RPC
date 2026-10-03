"""Flux temps réel du dashboard : des threads du laboratoire jusqu'aux clients WebSocket.

Les évènements naissent dans les threads des stubs, des squelettes et des proxys ; les
WebSockets vivent dans la boucle d'évènements. Le ``Hub`` fait le pont, avec trois garanties :

* un thread du laboratoire ne fait JAMAIS d'entrée/sortie réseau pour le dashboard : il
  dépose un rappel dans la boucle (``call_soon_threadsafe``) et repart ;
* un client lent ne retient personne : chaque client a sa file bornée, dont le message le
  plus ancien est évincé quand elle déborde ;
* un sujet que personne n'écoute ne coûte rien : rien n'est sérialisé pour lui.

Sujets : ``stats``, ``call``, ``network``, ``resilience``, ``job``, ``stream`` (abonnement
par défaut) et ``trace`` (chaque ``TraceEvent`` avec ses octets — sur demande seulement).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from typing import Any, Callable

from starlette.websockets import WebSocket, WebSocketState

from common.config import PROTOCOLS
from common.telemetry import CallTrace, EventBus, TraceCollector, TraceEvent

from .payloads import to_json

TOPICS: tuple[str, ...] = ("stats", "call", "network", "resilience", "job", "stream", "trace")
DEFAULT_TOPICS: frozenset[str] = frozenset(TOPICS) - {"trace"}
QUEUE_LIMIT = 1024          # messages en attente par client ; au-delà, les plus anciens sont perdus
STATS_INTERVAL_S = 1.0

# Un message est un dictionnaire, ou une fonction qui le construit : elle n'est appelée que
# dans la boucle, et seulement si quelqu'un écoute — le thread émetteur ne paie rien.
Message = dict[str, Any] | Callable[[], dict[str, Any]]

_log = logging.getLogger(__name__)

# Évènements du bus relayés sous leur propre sujet, en plus du sujet « trace ».
_SIDE_TOPICS: frozenset[str] = frozenset({"network", "resilience"})
_RATE_COUNTERS: tuple[tuple[str, str], ...] = (
    ("calls", "calls_per_s"),
    ("errors", "errors_per_s"),
    ("bytes_out", "bytes_out_per_s"),
    ("bytes_in", "bytes_in_per_s"),
)

Totals = dict[str, dict[str, float]]


class _Subscriber:
    """Un client WebSocket : ses sujets, sa file bornée et la tâche qui la vide."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.topics: set[str] = set(DEFAULT_TOPICS)
        self.dropped = 0
        self._queue: deque[str] = deque(maxlen=QUEUE_LIMIT)
        self._wakeup = asyncio.Event()

    def push(self, text: str) -> None:
        if len(self._queue) == QUEUE_LIMIT:
            self.dropped += 1   # deque(maxlen) évince le plus ancien : le client garde les faits récents
        self._queue.append(text)
        self._wakeup.set()

    async def pump(self) -> None:
        """Envoie les messages en attente ; seul endroit où l'on attend le réseau du client."""
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while self._queue:
                await self.websocket.send_text(self._queue.popleft())


class Hub:
    """Diffuse l'activité du laboratoire aux clients WebSocket.

    ``status`` (bloquant : il sonde les serveurs) fournit le contenu du message ``stats`` ;
    ``greeting`` (immédiat) fournit celui du message ``hello``.
    """

    def __init__(
        self,
        bus: EventBus,
        collector: TraceCollector,
        *,
        status: Callable[[], dict[str, Any]],
        greeting: Callable[[], dict[str, Any]],
    ) -> None:
        self._bus = bus
        self._collector = collector
        self._status = status
        self._greeting = greeting
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[_Subscriber] = set()
        # Lu sans verrou par les threads émetteurs : un entier périmé d'un instant ne fait
        # qu'envoyer, ou retenir, un message de trop.
        self._audience: dict[str, int] = dict.fromkeys(TOPICS, 0)
        self._unsubscribe: Callable[[], None] | None = None
        self._ticker: asyncio.Task[None] | None = None
        # Le collecteur n'offre pas de désabonnement : l'écoute est posée une fois, et
        # ``publish`` ne fait rien tant que le hub n'est pas démarré.
        collector.on_call_complete(self._on_call_complete)

    # -- cycle de vie (dans la boucle) ----------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._unsubscribe = self._bus.subscribe(self._on_event)
        self._ticker = asyncio.create_task(self._tick_forever(), name="dashboard-stats")

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._loop = None
        if self._ticker is not None:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
            self._ticker = None
        for subscriber in tuple(self._subscribers):
            if subscriber.websocket.application_state is WebSocketState.CONNECTED:
                with contextlib.suppress(Exception):   # client déjà parti : rien à fermer
                    await subscriber.websocket.close(code=1001, reason="Arrêt du laboratoire")

    # -- publication (depuis n'importe quel thread) ---------------------------------

    def publish(self, topic: str, message: Message) -> None:
        """Diffuse ``message`` aux abonnés de ``topic`` ; sans effet si personne n'écoute."""
        loop = self._loop
        if loop is None or not self._audience[topic]:
            return
        try:
            loop.call_soon_threadsafe(self._deliver, topic, message)
        except RuntimeError:
            pass    # boucle fermée entre-temps : le serveur s'arrête

    def _on_event(self, event: TraceEvent) -> None:
        """Abonné du bus : appelé dans le thread émetteur, à chaque étape de chaque appel."""
        side = event.side
        if side in _SIDE_TOPICS:
            self.publish(side, lambda: {"type": side, "event": event.to_dict(include_payload=False)})
        if self._audience["trace"]:
            # Sérialisé plus tard, dans la boucle : l'hexadécimal du payload n'est pas calculé
            # dans le thread de l'appel RPC, dont il fausserait les durées affichées.
            self.publish("trace", lambda: {"type": "trace", "event": event.to_dict()})

    def _on_call_complete(self, trace: CallTrace) -> None:
        self.publish("call", lambda: {"type": "call", **trace.summary()})

    # -- diffusion (dans la boucle) -------------------------------------------------

    def _deliver(self, topic: str, message: Message) -> None:
        listeners = [subscriber for subscriber in self._subscribers if topic in subscriber.topics]
        if not listeners:
            return
        try:
            text = to_json(message() if callable(message) else message)   # une fois pour tous les clients
        except (TypeError, ValueError) as exc:
            _log.warning("Message « %s » non diffusé : %s", topic, exc)
            return
        for subscriber in listeners:
            subscriber.push(text)

    def _recount(self) -> None:
        self._audience = {
            topic: sum(topic in subscriber.topics for subscriber in self._subscribers) for topic in TOPICS
        }

    # -- un client ------------------------------------------------------------------

    async def serve(self, websocket: WebSocket) -> None:
        """Vie complète d'une connexion WebSocket : accueil, abonnements, puis diffusion jusqu'au départ."""
        subscriber = _Subscriber(websocket)
        # Inscrit avant même l'acceptation : aucun message ne peut se glisser entre « hello » et la suite.
        subscriber.push(to_json({
            "type": "hello",
            "ts": time.time(),
            "topics": sorted(subscriber.topics),
            "available_topics": list(TOPICS),
            **self._greeting(),
        }))
        self._subscribers.add(subscriber)
        self._recount()
        pump: asyncio.Task[None] | None = None
        try:
            await websocket.accept()
            pump = asyncio.create_task(subscriber.pump(), name="dashboard-ws-pump")
            while True:
                frame = await websocket.receive()
                if frame["type"] == "websocket.disconnect":
                    break
                self._handle(subscriber, frame.get("text"))
        finally:
            self._subscribers.discard(subscriber)
            self._recount()
            if pump is not None:
                pump.cancel()
                # La tâche peut aussi avoir échoué sur un envoi vers un client déjà parti.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pump

    def _handle(self, subscriber: _Subscriber, text: str | None) -> None:
        """Message reçu d'un client : ``{"type": "subscribe" | "unsubscribe", "topics": [...]}``."""
        try:
            message = json.loads(text or "")
        except ValueError:
            return
        if not isinstance(message, dict) or not isinstance(message.get("topics"), list):
            return
        topics = {topic for topic in message["topics"] if topic in TOPICS}
        if message.get("type") == "subscribe":
            subscriber.topics |= topics
        elif message.get("type") == "unsubscribe":
            subscriber.topics -= topics
        else:
            return
        self._recount()
        # Accusé de réception : le client sait à partir de quand le sujet est servi.
        subscriber.push(to_json({"type": "subscribed", "topics": sorted(subscriber.topics)}))

    # -- statistiques périodiques ---------------------------------------------------

    def _sample(self) -> tuple[float, Totals]:
        totals = {protocol: dict(counters) for protocol, counters in list(self._collector.totals.items())}
        return time.perf_counter(), totals

    async def _tick_forever(self) -> None:
        """Chaque seconde : état du laboratoire et débit par protocole sur l'intervalle écoulé."""
        previous = self._sample()
        while True:
            await asyncio.sleep(STATS_INTERVAL_S)
            current = self._sample()
            if self._audience["stats"]:
                try:
                    # status() sonde les serveurs en TCP : jamais dans la boucle d'évènements.
                    status = await asyncio.to_thread(self._status)
                except Exception:   # le battement doit survivre à un état momentanément illisible
                    _log.exception("État du laboratoire indisponible pour le message « stats »")
                else:
                    self._deliver("stats", {
                        "type": "stats",
                        "ts": time.time(),
                        **status,
                        "rates": tick_rates(previous, current),
                    })
            previous = current


def tick_rates(previous: tuple[float, Totals], current: tuple[float, Totals]) -> dict[str, dict[str, float | None]]:
    """Débits par protocole entre deux relevés des compteurs du collecteur.

    ``avg_ms`` est la durée moyenne des appels terminés PENDANT l'intervalle (``None`` s'il
    n'y en a eu aucun) — contrairement à ``totals.avg_ms``, moyenne depuis le démarrage.
    """
    (before_at, before), (after_at, after) = previous, current
    elapsed = max(after_at - before_at, 1e-6)
    rates: dict[str, dict[str, float | None]] = {}
    for protocol in dict.fromkeys((*PROTOCOLS, *after)):
        now = after.get(protocol, {})
        then = before.get(protocol, {})
        if now.get("calls", 0) < then.get("calls", 0):
            then = {}   # compteurs remis à zéro entre les deux relevés (POST /api/reset)
        calls = now.get("calls", 0) - then.get("calls", 0)
        spent_us = now.get("total_us", 0.0) - then.get("total_us", 0.0)
        rates[protocol] = {
            **{
                rate: round((now.get(counter, 0) - then.get(counter, 0)) / elapsed, 2)
                for counter, rate in _RATE_COUNTERS
            },
            "avg_ms": round(spent_us / calls / 1000, 3) if calls else None,
        }
    return rates
