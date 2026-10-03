"""Laboratoire de pannes : le « piège de la transparence », démontré par l'expérience.

    python -m benchmark_lab.failure_simulation [--scenario ID] [--protocol custom|grpc|rest]

Un stub donne à un appel distant l'apparence d'un appel local. L'apparence seulement : entre
l'appelant et la procédure il y a un réseau, qui ralentit, perd des réponses et tombe en
panne. Cinq scénarios le montrent, chacun avec ses mesures :

* ``latency_trap``        — la même boucle en local puis à distance : N appels, N allers-retours ;
* ``timeout_spike``       — un pic de latence, sans puis avec échéance : après un timeout, l'issue est inconnue ;
* ``connection_cut``      — une coupure en plein appel : client naïf contre client résilient ;
* ``server_outage``       — une panne : retries épuisés, disjoncteur ouvert, puis reprise ;
* ``duplicate_execution`` — une réponse perdue puis un retry : double exécution, corrigée par une clé d'idempotence.

Chaque scénario traverse le proxy de chaos du protocole choisi, provoque sa panne de façon
déterministe (conditions explicites, pannes armées — jamais un tirage au sort) et lit la
vérité terrain directement dans le service. En sortie, même sur exception, le réseau, les
stocks et le proxy sont remis dans l'état où le scénario les a trouvés.
"""
from __future__ import annotations

import argparse
import math
import queue
import shutil
import statistics
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, TypedDict

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from common.client_api import InventoryClient
from common.config import APP_NAME, DEFAULT_TIMEOUT_S, PROTOCOL_LABELS, REMOTE_PROTOCOLS
from common.errors import CIRCUIT_OPEN, OK, TIMEOUT, RpcError, RpcTransportError
from common.telemetry import EventBus, TraceEvent
from netsim import CircuitBreaker, ResilientClient, RetryPolicy

if TYPE_CHECKING:
    from lab import LabRuntime

STEP_KINDS: tuple[str, ...] = (
    "local_call", "rpc_call", "attempt", "backoff", "breaker", "network", "state", "note",
)
STEP_STATUSES: tuple[str, ...] = ("ok", "slow", "timeout", "error", "retry", "info", "open", "dedup")


class Step(TypedDict):
    """Une ligne de la chronologie d'un scénario."""

    t_ms: float               # instant de l'étape, en millisecondes depuis le début du scénario
    kind: str                 # l'une des valeurs de STEP_KINDS
    label: str                # phrase affichable telle quelle
    status: str               # l'une des valeurs de STEP_STATUSES
    detail: dict[str, Any]    # nombres et codes : duration_ms, attempt, code, backoff_ms, stock…


StepCallback = Callable[[Step], None]

# --- Réglages ------------------------------------------------------------------------

_CALL_TIMEOUT_S = 2.0          # échéance ordinaire des appels : assez courte pour qu'un imprévu ne fige pas le scénario
_SETTLE_MARGIN_S = 0.25        # marge laissée à une requête retardée pour produire son effet sur le serveur
_READY_PATIENCE_S = 3.0        # reconnexion après une panne : gRPC y met jusqu'à une seconde (attente par paliers)
_READY_POLL_S = 0.05
_RETRY_ATTEMPTS = 4            # une de plus que nécessaire : gRPC peut échouer une fois de trop pendant sa reconnexion
_RETRY_BASE_DELAY_MS = 100
_OUTAGE_ATTEMPTS = 3           # autant de tentatives que le seuil du disjoncteur : il s'ouvre quand elles s'épuisent
_BREAKER_THRESHOLD = 3
_FAST_FAIL_CALLS = 3
_MAX_RECOVERY_PROBES = 4
# time.monotonic, l'horloge du disjoncteur, avance par pas d'environ 16 ms sous Windows.
_BREAKER_CLOCK_MARGIN_S = 0.03
_SMALLEST_DURATION_MS = 0.001  # plancher des rapports de durées : jamais de division par zéro

# Un produit par scénario : aucun ne peut fausser le stock observé par un autre.
_SPIKE_PRODUCT = "SKU-1005"
_CUT_PRODUCT = "SKU-1002"
_OUTAGE_PRODUCT = "SKU-1010"
_DUPLICATE_PRODUCT = "SKU-1018"

# Les scénarios règlent le réseau simulé, qui est commun à tout le laboratoire : jamais deux à la fois.
_RUN_LOCK = threading.Lock()


def _fr(value: float, digits: int | None = None) -> str:
    """Nombre au format français : espace des milliers, virgule décimale.

    Sans ``digits``, la précision suit l'ordre de grandeur, pour qu'une durée de 0,042 ms
    et une autre de 2 431 ms restent toutes deux lisibles.
    """
    if digits is None:
        magnitude = abs(value)
        digits = 0 if float(value).is_integer() or magnitude >= 100 else 1 if magnitude >= 1 else 3
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def _ms(duration_ms: float) -> float:
    return round(duration_ms, 3)


def _ratio(numerator_ms: float, denominator_ms: float) -> float:
    return round(numerator_ms / max(denominator_ms, _SMALLEST_DURATION_MS), 1)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count > 1 else ''}"


# --- Chronologie ---------------------------------------------------------------------

class _Timeline:
    """Étapes horodatées d'un scénario, remises en direct à ``on_step``.

    Les étapes viennent de plusieurs fils — le scénario, mais aussi ceux du proxy quand il
    publie une panne : un verrou fixe leur ordre et garantit des ``t_ms`` croissants.
    ``on_step`` est appelé depuis un fil à part, dans ce même ordre : un observateur lent
    (affichage, WebSocket) ne s'ajoute donc jamais aux durées que le scénario mesure.
    """

    def __init__(self, on_step: StepCallback | None) -> None:
        self.steps: list[Step] = []
        self._origin = time.perf_counter()
        self._lock = threading.Lock()
        self._closed = False
        self._on_step = on_step
        self._outbox: queue.SimpleQueue[Step | None] = queue.SimpleQueue()
        self._failure: Exception | None = None
        self._notifier: threading.Thread | None = None
        if on_step is not None:
            self._notifier = threading.Thread(target=self._notify, name="failure-lab-steps", daemon=True)
            self._notifier.start()

    def elapsed_ms(self) -> float:
        return _ms((time.perf_counter() - self._origin) * 1000)

    def add(self, kind: str, label: str, status: str, detail: dict[str, Any]) -> None:
        """Inscrit une étape ; appelable depuis n'importe quel fil."""
        with self._lock:
            if self._closed:
                return      # évènement du bus arrivé après la fin : il n'appartient plus à ce scénario
            step: Step = {"t_ms": self.elapsed_ms(), "kind": kind, "label": label, "status": status, "detail": detail}
            self.steps.append(step)
            if self._notifier is not None:
                self._outbox.put(step)

    def close(self) -> None:
        """Clôt la chronologie et attend que l'observateur ait reçu toutes les étapes."""
        with self._lock:
            self._closed = True
            notifier, self._notifier = self._notifier, None
        if notifier is not None:
            self._outbox.put(None)
            notifier.join()

    def check(self) -> None:
        """Relance, dans le fil qui l'appelle, l'erreur qu'a levée l'observateur."""
        if self._failure is not None:
            raise self._failure

    def _notify(self) -> None:
        while (step := self._outbox.get()) is not None:
            if self._failure is not None:
                continue    # l'observateur a échoué : on vide la file sans plus l'appeler
            try:
                self._on_step(step)
            except Exception as exc:
                self._failure = exc


# --- Évènements du bus → étapes ------------------------------------------------------
#
# Le proxy de chaos et le client résilient publient déjà ce qu'ils font sur le bus de
# traces. Le scénario n'a pas à le deviner : il relaie ces évènements dans sa chronologie.

_StepDraft = tuple[str, str, str, dict[str, Any]]   # (kind, label, status, detail)

_BREAKER_STATUS: dict[str, str] = {"open": "open", "half_open": "info", "closed": "ok"}


def _attempt_step(event: TraceEvent) -> _StepDraft:
    detail = event.detail
    rank, code = detail["n"], detail["code"]
    if detail["outcome"] == "ok":
        label, status = f"Tentative n° {rank} : le serveur a répondu", "ok"
    else:
        label, status = f"Tentative n° {rank} : échec ({code})", "timeout" if code == TIMEOUT else "error"
    return "attempt", label, status, {
        "attempt": rank,
        "max_attempts": detail["max_attempts"],
        "code": code,
        "duration_ms": detail["duration_ms"],
        "call_id": event.call_id,
    }


def _backoff_step(event: TraceEvent) -> _StepDraft:
    detail = event.detail
    label = f"Attente de {_fr(detail['backoff_ms'])} ms avant la tentative n° {detail['next_attempt']}"
    return "backoff", label, "retry", {
        "attempt": detail["n"],
        "next_attempt": detail["next_attempt"],
        "backoff_ms": detail["backoff_ms"],
        "code": detail["code"],
    }


def _breaker_step(event: TraceEvent) -> _StepDraft:
    detail = event.detail
    kept = ("from", "to", "failures", "failure_threshold", "reset_timeout_s")
    return "breaker", detail["text"], _BREAKER_STATUS[detail["to"]], {name: detail[name] for name in kept}


def _dedup_step(event: TraceEvent) -> _StepDraft:
    detail = event.detail
    return "note", detail["text"], "dedup", {"attempt": detail["n"], "idempotency_key": detail["idempotency_key"]}


def _give_up_step(event: TraceEvent) -> _StepDraft | None:
    detail = event.detail
    if detail["reason"] == "circuit_open":
        return None     # l'étape de l'appel refusé le dit déjà
    return "note", detail["text"], "error", {
        "reason": detail["reason"], "attempts": detail["attempts"], "code": detail["code"],
    }


def _fault_step(event: TraceEvent) -> _StepDraft:
    facts = {name: value for name, value in event.detail.items() if name not in ("proxy", "text")}
    return "network", event.detail["text"], "error", {"fault": event.stage.removeprefix("network."), **facts}


def _delay_step(event: TraceEvent) -> _StepDraft | None:
    detail = event.detail
    if not detail["spike"]:
        return None     # la latence ordinaire retarde chaque morceau d'octets : la relayer noierait la chronologie
    label = f"Pic de latence : la requête est retenue {_fr(detail['delay_ms'])} ms par le réseau"
    return "network", label, "slow", {"delay_ms": detail["delay_ms"], "bytes": detail["bytes"]}


_RELAYS: dict[str, Callable[[TraceEvent], _StepDraft | None]] = {
    "resilience.attempt": _attempt_step,
    "resilience.backoff": _backoff_step,
    "resilience.breaker": _breaker_step,
    "resilience.dedup": _dedup_step,
    "resilience.give_up": _give_up_step,
    "network.reset": _fault_step,
    "network.refuse": _fault_step,
    "network.lost_reply": _fault_step,
    "network.blackhole": _fault_step,
    "network.delay": _delay_step,
}


# --- Séance d'un scénario ------------------------------------------------------------

@dataclass(frozen=True)
class _Call:
    """Issue d'un appel mesuré : un résultat ou une erreur, et la durée vue par l'appelant."""

    duration_ms: float
    result: Any = None
    error: RpcError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def code(self) -> str:
        return OK if self.error is None else self.error.code

    def status(self, slow: bool) -> str:
        if self.error is not None:
            return {TIMEOUT: "timeout", CIRCUIT_OPEN: "open"}.get(self.error.code, "error")
        if isinstance(self.result, dict) and self.result.get("applied") is False:
            return "dedup"
        return "slow" if slow else "ok"

    def checked(self) -> "_Call":
        """Pour un appel dont le scénario a besoin : son échec interrompt l'expérience."""
        if self.error is not None:
            raise self.error
        return self


class _Session:
    """Ce dont un scénario dispose : chronologie, réseau simulé, clients, vérité terrain.

    Tout ce qu'un scénario dérègle ou emprunte passe par cette classe, qui s'en souvient :
    ``restore()`` remet le laboratoire dans l'état initial, que le scénario aboutisse ou non.
    """

    def __init__(
        self, runtime: "LabRuntime", protocol: str, options: dict[str, float], timeline: _Timeline
    ) -> None:
        self.runtime = runtime
        self.protocol = protocol
        self.options = options
        self._timeline = timeline
        self._clients: list[InventoryClient] = []
        self._borrowed: dict[str, int] = {}     # produit → stock à rétablir en sortie
        self._settle_at = 0.0                   # instant où plus aucune requête retardée n'est en route
        self._conditions = runtime.conditions.snapshot()
        self._unsubscribe = runtime.bus.subscribe(self._relay)
        # Même point de départ pour tous les scénarios : réseau parfait, aucune panne en attente.
        runtime.proxies[protocol].disarm()
        runtime.conditions.reset()

    def restore(self) -> None:
        """Remise en état, dans l'ordre : écoute du bus, pannes armées, réseau, connexions, stocks."""
        runtime = self.runtime
        with ExitStack() as teardown:   # dépilé à l'envers ; chaque étape est tentée même si une autre échoue
            teardown.callback(self._restore_stocks)
            teardown.callback(self._close_clients)
            teardown.callback(runtime.conditions.update, **self._conditions)
            teardown.callback(runtime.proxies[self.protocol].disarm)
            teardown.callback(self._unsubscribe)

    # -- chronologie ----------------------------------------------------------------

    def step(self, kind: str, label: str, status: str = "info", /, **detail: Any) -> None:
        """Étape inscrite par le scénario lui-même : c'est là qu'une erreur de l'observateur l'interrompt."""
        self._timeline.check()
        self._timeline.add(kind, label, status, detail)

    def pause(self, seconds: float, label: str) -> None:
        self.step("note", label, "info", duration_ms=_ms(seconds * 1000))
        time.sleep(seconds)

    def _relay(self, event: TraceEvent) -> None:
        """Abonné du bus : pannes injectées par le proxy et décisions du client résilient."""
        if event.protocol != self.protocol:
            return
        translate = _RELAYS.get(event.stage)
        draft = translate(event) if translate is not None else None
        if draft is not None:
            self._timeline.add(*draft)

    # -- réseau simulé --------------------------------------------------------------

    def network(self, label: str, **conditions: Any) -> None:
        self.runtime.conditions.update(**conditions)
        self.step("network", label, "info", **conditions)

    def arm(self, kind: str, label: str) -> None:
        self.runtime.arm(kind, self.protocol)
        self.step("network", label, "info", armed=kind)

    def in_flight_until(self, instant: float) -> None:
        """Signale qu'une requête retardée peut encore atteindre le serveur jusqu'à ``instant``."""
        self._settle_at = max(self._settle_at, instant)

    def settle(self) -> None:
        """Attend que plus aucune requête retardée ne soit en route vers le serveur."""
        time.sleep(max(0.0, self._settle_at - time.perf_counter()))

    # -- clients --------------------------------------------------------------------

    def local_client(self) -> InventoryClient:
        return self._track(self.runtime.client("local"))

    def remote_client(self) -> InventoryClient:
        return self._track(self.runtime.client(self.protocol, via_proxy=True, timeout=_CALL_TIMEOUT_S))

    def resilient(self, inner: InventoryClient, **policy: Any) -> ResilientClient:
        """Le même client, enveloppé d'une politique de résilience (``inner`` reste fermé par la séance)."""
        return ResilientClient(inner, timeout=_CALL_TIMEOUT_S, bus=self.runtime.bus, **policy)

    def warm_up(self, client: InventoryClient) -> None:
        """Établit la connexion avant toute mesure : son coût n'appartient pas au premier appel mesuré."""
        waited_ms = self._until_ready(client)
        self.step("state", "Connexion établie avec le serveur, à travers le proxy de chaos", "ok",
                  duration_ms=waited_ms)

    def reconnect(self, client: InventoryClient) -> None:
        """Après une coupure : attend que le transport soit de nouveau utilisable.

        Les trois clients se reconnectent d'eux-mêmes à l'appel suivant, mais gRPC peut
        échouer encore un instant. Armer la panne suivante avant ce retour au calme la
        ferait consommer — ou manquer — par un appel qui n'est pas celui qu'on observe.
        """
        waited_ms = self._until_ready(client)
        self.step("state", f"Connexion rétablie en {_fr(waited_ms)} ms : le client s'est reconnecté seul", "info",
                  duration_ms=waited_ms)

    def call(
        self, label: str, client: InventoryClient, method: str, *args: Any,
        kind: str = "rpc_call", slow: bool = False, **kwargs: Any,
    ) -> _Call:
        """Exécute un appel, le mesure et l'inscrit ; une ``RpcError`` devient une issue, pas une exception."""
        procedure = getattr(client, method)
        started = time.perf_counter()
        try:
            result, error = procedure(*args, **kwargs), None
        except RpcError as exc:
            result, error = None, exc
        outcome = _Call((time.perf_counter() - started) * 1000, result, error)
        detail: dict[str, Any] = {"method": method, "duration_ms": _ms(outcome.duration_ms), "code": outcome.code}
        if error is not None:
            detail["error"] = type(error).__name__
            detail["message"] = error.message
        report = getattr(client, "last_report", None)
        if report:
            detail["attempts"] = len(report["attempts"])
        if client.last_call_id and outcome.code != CIRCUIT_OPEN:    # refus du disjoncteur : rien n'est parti
            detail["call_id"] = client.last_call_id
        self.step(kind, label, outcome.status(slow), **detail)
        return outcome

    # -- vérité terrain -------------------------------------------------------------

    def stock(self, product_id: str) -> int:
        """Stock lu directement dans le service, sans passer par le réseau qu'on est en train de dérégler."""
        return self.runtime.service.check_stock(product_id)["stock"]

    def set_stock(self, product_id: str, target: int) -> None:
        delta = target - self.stock(product_id)
        if delta:
            self.runtime.service.update_stock(product_id, delta)

    def borrow(self, product_id: str, minimum: int) -> int:
        """Réserve un produit au scénario et renvoie son stock ; ``restore()`` lui rendra sa valeur initiale."""
        initial = self.stock(product_id)
        self._borrowed.setdefault(product_id, initial)
        if initial < minimum:
            self.set_stock(product_id, minimum)
        return max(initial, minimum)

    # -- interne --------------------------------------------------------------------

    def _track(self, client: InventoryClient) -> InventoryClient:
        self._clients.append(client)
        return client

    def _until_ready(self, client: InventoryClient) -> float:
        """Appelle une procédure sans effet jusqu'à obtenir une réponse ; renvoie l'attente en ms."""
        started = time.perf_counter()
        while True:
            try:
                client.calculate_factorial(1, timeout=_CALL_TIMEOUT_S)
            except RpcTransportError:
                if time.perf_counter() - started > _READY_PATIENCE_S:
                    raise
                time.sleep(_READY_POLL_S)
            else:
                return _ms((time.perf_counter() - started) * 1000)

    def _close_clients(self) -> None:
        with ExitStack() as closing:
            for client in self._clients:
                closing.callback(client.close)

    def _restore_stocks(self) -> None:
        # Une requête encore retenue par le réseau débiterait le stock APRÈS sa remise en état.
        self.settle()
        for product_id, initial in self._borrowed.items():
            self.set_stock(product_id, initial)


# --- Les cinq scénarios --------------------------------------------------------------
#
# Chacun reçoit sa séance et renvoie (mesures, verdict). Le verdict est rédigé à partir des
# valeurs mesurées : il dit ce qui a été observé, pas ce qui était attendu.

_Outcome = tuple[dict[str, Any], str]


def _latency_trap(lab: _Session) -> _Outcome:
    latency_ms = lab.options["latency_ms"]
    product_ids = lab.runtime.service.product_ids()[: lab.options["calls"]]
    calls = len(product_ids)
    local, remote = lab.local_client(), lab.remote_client()
    lab.warm_up(remote)

    local_total_ms = sum(
        lab.call(f"Lecture locale {rank}/{calls} : get_product_details({product_id!r})",
                 local, "get_product_details", product_id, kind="local_call").checked().duration_ms
        for rank, product_id in enumerate(product_ids, start=1)
    )
    lab.network(f"Le serveur s'éloigne : {_fr(latency_ms)} ms de latence aller-retour", latency_ms=latency_ms)
    # Le code de la boucle est identique, seul le client change : c'est toute la « transparence ».
    rpc_total_ms = sum(
        lab.call(f"Lecture distante {rank}/{calls} : get_product_details({product_id!r})",
                 remote, "get_product_details", product_id, slow=True).checked().duration_ms
        for rank, product_id in enumerate(product_ids, start=1)
    )
    batched = lab.call(f"Le remède — un seul appel groupé : list_products(limit={calls})",
                       remote, "list_products", calls).checked()

    metrics = {
        "calls": calls,
        "latency_ms": latency_ms,
        "local_total_ms": _ms(local_total_ms),
        "rpc_total_ms": _ms(rpc_total_ms),
        "per_call_ms": _ms(rpc_total_ms / calls),
        "slowdown_x": _ratio(rpc_total_ms, local_total_ms),
        "batched_total_ms": _ms(batched.duration_ms),
        "batched_products": len(batched.result["products"]),
        "batched_speedup_x": _ratio(rpc_total_ms, batched.duration_ms),
    }
    verdict = (
        f"Les {calls} lectures prennent {_fr(local_total_ms)} ms en local et {_fr(rpc_total_ms)} ms à travers "
        f"un réseau à {_fr(latency_ms)} ms de latence, soit {_fr(metrics['slowdown_x'])} fois plus ; "
        f"un seul appel groupé rapporte les {metrics['batched_products']} fiches en {_fr(batched.duration_ms)} ms."
    )
    return metrics, verdict


def _timeout_spike(lab: _Session) -> _Outcome:
    spike_ms, deadline_ms = lab.options["spike_ms"], lab.options["deadline_ms"]
    product_id = _SPIKE_PRODUCT
    stock_before = lab.borrow(product_id, minimum=2)
    client = lab.remote_client()
    lab.warm_up(client)
    lab.step("state", f"Stock de {product_id} avant l'expérience : {stock_before}", "info", stock=stock_before)

    lab.network(f"Le réseau se dégrade : il retiendra chaque requête {_fr(spike_ms)} ms",
                spike_probability=1.0, spike_ms=spike_ms)
    in_flight_s = spike_ms / 1000 + _SETTLE_MARGIN_S
    lab.in_flight_until(time.perf_counter() + in_flight_s)
    patient = lab.call(
        "Sans échéance : update_stock(-1) — l'appelant attend le temps qu'il faut",
        client, "update_stock", product_id, -1, timeout=spike_ms / 1000 + DEFAULT_TIMEOUT_S, slow=True,
    ).checked()

    # La requête qui suit reste en route pendant tout le pic, même si l'appelant l'abandonne avant.
    lab.in_flight_until(time.perf_counter() + in_flight_s)
    hasty = lab.call(f"Avec une échéance de {_fr(deadline_ms)} ms : update_stock(-1)",
                     client, "update_stock", product_id, -1, timeout=deadline_ms / 1000)
    lab.step("note", "L'appelant est libéré, mais ne sait pas si le serveur a exécuté l'appel : issue inconnue", "info")
    lab.network("Fin du pic : le réseau redevient normal", spike_probability=0.0, spike_ms=0.0)
    lab.settle()
    stock_after = lab.stock(product_id)
    server_executed = stock_after == stock_before - 2
    lab.step(
        "state",
        f"Vérité terrain : le serveur a tout de même exécuté l'appel abandonné (stock à {stock_after})"
        if server_executed else
        f"Vérité terrain : le serveur n'a pas exécuté l'appel abandonné (stock à {stock_after})",
        "info", stock=stock_after, server_executed=server_executed,
    )

    metrics = {
        "spike_ms": spike_ms,
        "deadline_ms": deadline_ms,
        "no_deadline_ms": _ms(patient.duration_ms),
        "with_deadline_ms": _ms(hasty.duration_ms),
        "with_deadline_outcome": hasty.code,
        "time_saved_ms": _ms(patient.duration_ms - hasty.duration_ms),
        "server_executed": server_executed,
        "stock_before": stock_before,
        "stock_after": stock_after,
    }
    fate = "a tout de même exécuté" if server_executed else "n'a finalement pas exécuté"
    verdict = (
        f"Sans échéance, l'appelant reste bloqué {_fr(patient.duration_ms)} ms par le pic ; avec une échéance "
        f"de {_fr(deadline_ms)} ms, il est libéré au bout de {_fr(hasty.duration_ms)} ms ({hasty.code}), sans "
        f"pouvoir savoir que le serveur {fate} l'appel."
    )
    return metrics, verdict


def _check_spike_options(options: dict[str, float]) -> None:
    if options["deadline_ms"] >= options["spike_ms"]:
        raise ValueError(
            f"« deadline_ms » ({_fr(options['deadline_ms'])}) doit être inférieur à « spike_ms » "
            f"({_fr(options['spike_ms'])}) : sinon l'échéance n'expire jamais pendant le pic"
        )


def _connection_cut(lab: _Session) -> _Outcome:
    product_id = _CUT_PRODUCT
    local, naive = lab.local_client(), lab.remote_client()
    lab.warm_up(naive)
    lab.call(f"Référence locale : get_product_details({product_id!r}) — aucune erreur de transport possible",
             local, "get_product_details", product_id, kind="local_call").checked()

    lab.arm("reset", "Panne armée : la prochaine requête coupera la connexion avant d'atteindre le serveur")
    crashed = lab.call("Client naïf, écrit comme un appel local : get_product_details(…)",
                       naive, "get_product_details", product_id)
    lab.reconnect(naive)

    resilient = lab.resilient(
        naive, retry=RetryPolicy(max_attempts=_RETRY_ATTEMPTS, base_delay_ms=_RETRY_BASE_DELAY_MS, jitter=0.0)
    )
    lab.arm("reset", "Même panne armée, cette fois face à un client qui sait réessayer")
    saved = lab.call("Client résilient (nouvelles tentatives espacées) : get_product_details(…)",
                     resilient, "get_product_details", product_id)
    attempts = resilient.last_report["attempts"]
    backoff_ms = sum(attempt["backoff_ms"] for attempt in attempts)

    metrics = {
        "naive_outcome": crashed.code,
        "naive_error": "" if crashed.error is None else type(crashed.error).__name__,
        "naive_ms": _ms(crashed.duration_ms),
        "resilient_outcome": saved.code,
        "resilient_attempts": len(attempts),
        "resilient_backoff_ms": _ms(backoff_ms),
        "resilient_total_ms": _ms(saved.duration_ms),
    }
    naive_part = (
        f"La coupure fait échouer le client naïf en {_fr(crashed.duration_ms)} ms avec "
        f"{metrics['naive_error']} ({crashed.code}), une erreur qu'aucun appel local ne peut produire"
        if crashed.error is not None else
        "Le client naïf n'a pas subi la coupure armée"
    )
    resilient_part = (
        f"le client résilient aboutit à la tentative n° {len(attempts)}, en {_fr(saved.duration_ms)} ms "
        f"dont {_fr(backoff_ms)} ms d'attente"
        if saved.ok else
        f"le client résilient échoue lui aussi ({saved.code}) après {_plural(len(attempts), 'tentative')}"
    )
    return metrics, f"{naive_part} ; {resilient_part}."


def _server_outage(lab: _Session) -> _Outcome:
    product_id = _OUTAGE_PRODUCT
    reset_timeout_ms = lab.options["reset_timeout_ms"]
    breaker = CircuitBreaker(_BREAKER_THRESHOLD, reset_timeout_ms / 1000, name=f"{lab.protocol}-inventaire")
    inner = lab.remote_client()
    client = lab.resilient(
        inner,
        retry=RetryPolicy(max_attempts=_OUTAGE_ATTEMPTS, base_delay_ms=_RETRY_BASE_DELAY_MS, jitter=0.0),
        breaker=breaker,
    )
    lab.warm_up(inner)
    lab.call("Serveur en service : get_product_details(…)", client, "get_product_details", product_id).checked()

    lab.network("Panne : le serveur devient injoignable", down=True)
    exhausted = lab.call("Appel pendant la panne : le client réessaie, puis renonce",
                         client, "get_product_details", product_id)
    failures = len(client.last_report["attempts"])
    refused = [
        lab.call(f"Appel suivant n° {rank} : get_product_details(…)", client, "get_product_details", product_id)
        for rank in range(1, _FAST_FAIL_CALLS + 1)
    ]

    lab.network("Le serveur est de nouveau joignable", down=False)
    probes, recovered = 0, False
    while not recovered and probes < _MAX_RECOVERY_PROBES:
        rest_s = breaker.snapshot()["retry_in_s"] + _BREAKER_CLOCK_MARGIN_S
        lab.pause(rest_s, f"Disjoncteur ouvert : aucun appel pendant {_fr(rest_s * 1000, 0)} ms (délai de repos)")
        probes += 1
        recovered = lab.call(f"Appel d'essai n° {probes} : get_product_details(…)",
                             client, "get_product_details", product_id).ok

    state = breaker.snapshot()
    fast_fail_ms = statistics.median(call.duration_ms for call in refused)
    metrics = {
        "failures_before_open": failures,
        "slow_fail_ms": _ms(exhausted.duration_ms),
        "slow_fail_outcome": exhausted.code,
        "fast_fail_ms": _ms(fast_fail_ms),
        "fast_fail_outcome": refused[0].code,
        "fast_fail_calls": len(refused),
        "fast_fail_speedup_x": _ratio(exhausted.duration_ms, fast_fail_ms),
        "breaker_opened": state["opened_count"] > 0,
        "recovery_probes": probes,
        "recovered": recovered,
        "breaker_state": state["state"],
    }
    recovery_part = (
        f"au retour du serveur, l'appel d'essai n° {probes} réussit et le disjoncteur se referme"
        if recovered else
        f"après {_plural(probes, 'appel')} d'essai, le disjoncteur n'est toujours pas refermé"
    )
    refusal_part = (
        f"le disjoncteur refuse les {len(refused)} appels suivants en {_fr(fast_fail_ms)} ms chacun, "
        "sans toucher au réseau"
        if all(call.code == CIRCUIT_OPEN for call in refused) else
        f"les {len(refused)} appels suivants se terminent en {_fr(fast_fail_ms)} ms chacun ({refused[0].code})"
    )
    verdict = (
        f"Pendant la panne, un appel épuise ses {_plural(failures, 'tentative')} en {_fr(exhausted.duration_ms)} ms "
        f"({exhausted.code}) ; {refusal_part} ; {recovery_part}."
    )
    return metrics, verdict


def _duplicate_execution(lab: _Session) -> _Outcome:
    product_id = _DUPLICATE_PRODUCT
    stock_before = lab.borrow(product_id, minimum=2)
    expected_after = stock_before - 1
    inner = lab.remote_client()
    lab.warm_up(inner)
    lab.step("state", f"Stock de {product_id} : {stock_before}. Opération voulue : en retirer 1, une seule fois",
             "info", stock=stock_before, expected=expected_after)
    policy = RetryPolicy(max_attempts=_RETRY_ATTEMPTS, base_delay_ms=_RETRY_BASE_DELAY_MS, jitter=0.0)

    retrying = lab.resilient(inner, retry=policy)
    lab.arm("lost_reply", "Panne armée : la requête atteindra le serveur, mais sa réponse sera perdue")
    lab.call("update_stock(-1) avec nouvelles tentatives, sans clé d'idempotence",
             retrying, "update_stock", product_id, -1).checked()
    naive_after = lab.stock(product_id)
    naive_executions = stock_before - naive_after
    lab.step("state", f"Vérité terrain : stock {stock_before} → {naive_after}, "
                      f"le serveur a exécuté l'opération {naive_executions} fois",
             "ok" if naive_after == expected_after else "error",
             stock=naive_after, expected=expected_after, executions=naive_executions)

    lab.set_stock(product_id, stock_before)
    lab.step("state", f"Stock remis à {stock_before} pour refaire l'expérience à l'identique", "info",
             stock=stock_before)
    keyed = lab.resilient(inner, retry=policy, auto_idempotency_key=True)
    lab.arm("lost_reply", "Même panne armée, cette fois avec une clé d'idempotence jointe à l'appel")
    replay = lab.call("update_stock(-1) avec nouvelles tentatives ET clé d'idempotence",
                      keyed, "update_stock", product_id, -1).checked()
    idempotent_after = lab.stock(product_id)
    idempotent_executions = stock_before - idempotent_after
    lab.step("state", f"Vérité terrain : stock {stock_before} → {idempotent_after}, "
                      f"le serveur a exécuté l'opération {idempotent_executions} fois",
             "ok" if idempotent_after == expected_after else "error",
             stock=idempotent_after, expected=expected_after, executions=idempotent_executions)

    metrics = {
        "stock_before": stock_before,
        "expected_after": expected_after,
        "naive_after": naive_after,
        "naive_executions": naive_executions,
        "naive_attempts": len(retrying.last_report["attempts"]),
        "idempotent_after": idempotent_after,
        "idempotent_executions": idempotent_executions,
        "idempotent_attempts": len(keyed.last_report["attempts"]),
        "deduplicated": replay.result["applied"] is False,
    }
    replay_fate = "le serveur reconnaît le rejeu" if metrics["deduplicated"] else "le serveur ne reconnaît aucun rejeu"
    verdict = (
        f"Une seule opération était demandée, mais la réponse perdue puis la nouvelle tentative l'ont fait "
        f"exécuter {naive_executions} fois : le stock passe de {stock_before} à {naive_after} au lieu de "
        f"{expected_after} ; avec une clé d'idempotence, {replay_fate} : "
        f"{_plural(idempotent_executions, 'exécution')} pour {_plural(metrics['idempotent_attempts'], 'tentative')}, "
        f"et le stock s'établit à {idempotent_after}."
    )
    return metrics, verdict


# --- Catalogue des scénarios ---------------------------------------------------------

@dataclass(frozen=True)
class _Option:
    """Réglage d'un scénario : valeur par défaut et bornes acceptées."""

    default: float
    minimum: float
    maximum: float
    whole: bool = False     # un nombre d'appels est entier ; une durée peut être fractionnaire


@dataclass(frozen=True)
class _Scenario:
    id: str
    title: str
    icon: str                   # nom d'icône Lucide, embarquée par le dashboard
    summary: str
    concept: str
    lesson: str                 # la règle générale à retenir, indépendante des mesures
    duration_hint_s: float      # durée typique avec les réglages par défaut
    play: Callable[[_Session], _Outcome]
    options: dict[str, _Option] = field(default_factory=dict)
    check: Callable[[dict[str, float]], None] | None = None     # contrainte liant plusieurs options


_SCENARIOS: tuple[_Scenario, ...] = (
    _Scenario(
        id="latency_trap",
        title="Le piège de la boucle innocente",
        icon="hourglass",
        summary="La même boucle de 12 lectures, exécutée en local puis à travers un réseau à 200 ms de "
                "latence, et enfin remplacée par un seul appel groupé.",
        concept="Latence et interfaces bavardes",
        lesson="Chaque appel distant coûte un aller-retour réseau : une boucle anodine en local se paie N fois "
               "la latence. Une interface distante se conçoit à gros grain — un appel qui rapporte tout, "
               "plutôt que N appels bavards.",
        duration_hint_s=3.0,
        play=_latency_trap,
        options={"calls": _Option(12, 1, 24, whole=True), "latency_ms": _Option(200, 1, 1000)},
    ),
    _Scenario(
        id="timeout_spike",
        title="Le pic de latence et l'échéance",
        icon="timer",
        summary="Un pic de 1,5 s frappe un appel : sans échéance, l'appelant reste bloqué ; avec une échéance "
                "de 300 ms, il est libéré — sans savoir si le serveur a exécuté l'appel.",
        concept="Échéances et issue inconnue",
        lesson="Tout appel distant doit porter une échéance, sinon la lenteur du réseau devient celle de "
               "l'appelant. Mais un timeout n'est pas un refus : l'issue est inconnue, le serveur a peut-être "
               "exécuté l'appel.",
        duration_hint_s=3.5,
        play=_timeout_spike,
        options={"spike_ms": _Option(1500, 100, 5000), "deadline_ms": _Option(300, 20, 2000)},
        check=_check_spike_options,
    ),
    _Scenario(
        id="connection_cut",
        title="La coupure en plein appel",
        icon="unplug",
        summary="La connexion est coupée pendant l'appel : un client écrit comme pour un appel local échoue "
                "sur une erreur réseau ; un client résilient attend, réessaie et aboutit.",
        concept="Pannes partielles et nouvelles tentatives",
        lesson="Un appel distant peut échouer alors que le code et le serveur sont corrects : le réseau est un "
               "mode de panne à part entière. Il faut le prévoir — rattraper l'erreur, attendre, réessayer — "
               "ce qui n'est sans danger que pour une opération idempotente, comme cette lecture.",
        duration_hint_s=0.5,
        play=_connection_cut,
    ),
    _Scenario(
        id="server_outage",
        title="La panne du serveur et le disjoncteur",
        icon="power",
        summary="Le serveur tombe : les nouvelles tentatives s'épuisent, le disjoncteur s'ouvre et refuse les "
                "appels suivants sans attendre ; au retour du serveur, un appel d'essai le referme.",
        concept="Disjoncteur (circuit breaker)",
        lesson="Réessayer ne répare pas un serveur en panne : cela fait attendre l'appelant et accable le "
               "serveur. Un disjoncteur échoue vite tant que la panne dure, puis teste prudemment la reprise "
               "avant de rétablir le trafic.",
        duration_hint_s=1.5,
        play=_server_outage,
        options={"reset_timeout_ms": _Option(1000, 100, 5000)},
    ),
    _Scenario(
        id="duplicate_execution",
        title="La réponse perdue et la double exécution",
        icon="copy",
        summary="La réponse d'un update_stock(-1) se perd : le client réessaie et le serveur exécute "
                "l'opération deux fois. Avec une clé d'idempotence, le rejeu est reconnu et le stock ne baisse "
                "que d'une unité.",
        concept="Idempotence et nouvelles tentatives",
        lesson="Après une erreur réseau, l'appelant ignore si le serveur a exécuté l'appel : réessayer une "
               "opération non idempotente peut l'appliquer deux fois. Une nouvelle tentative n'est sûre que si "
               "l'opération est idempotente — par nature, ou grâce à une clé que le serveur sait reconnaître.",
        duration_hint_s=0.6,
        play=_duplicate_execution,
    ),
)

# Métadonnées publiques, dans l'ordre de jeu (``GET /api/failures/scenarios`` du dashboard).
SCENARIOS: list[dict[str, Any]] = [
    {
        "id": scenario.id,
        "title": scenario.title,
        "icon": scenario.icon,
        "summary": scenario.summary,
        "concept": scenario.concept,
        "duration_hint_s": scenario.duration_hint_s,
        "protocols": list(REMOTE_PROTOCOLS),
    }
    for scenario in _SCENARIOS
]

# Réglages de chaque scénario, tels que ``run_scenario(options=…)`` les accepte et les surcharge.
DEFAULT_OPTIONS: dict[str, dict[str, float]] = {
    scenario.id: {name: option.default for name, option in scenario.options.items()} for scenario in _SCENARIOS
}

# Libellé et unité de chaque mesure renvoyée dans ``metrics`` (affichage du CLI, légendes du dashboard).
METRIC_INFO: dict[str, dict[str, str]] = {
    name: {"label": label, "unit": unit}
    for name, label, unit in (
        ("calls", "Lectures dans la boucle", ""),
        ("latency_ms", "Latence aller-retour injectée", "ms"),
        ("local_total_ms", "Boucle locale", "ms"),
        ("rpc_total_ms", "Boucle distante", "ms"),
        ("per_call_ms", "Par appel distant", "ms"),
        ("slowdown_x", "Ralentissement de la boucle", "×"),
        ("batched_total_ms", "Un seul appel groupé", "ms"),
        ("batched_products", "Fiches rapportées par l'appel groupé", ""),
        ("batched_speedup_x", "Gain de l'appel groupé", "×"),
        ("spike_ms", "Durée du pic de latence", "ms"),
        ("deadline_ms", "Échéance fixée par l'appelant", "ms"),
        ("no_deadline_ms", "Attente sans échéance", "ms"),
        ("with_deadline_ms", "Attente avec échéance", "ms"),
        ("with_deadline_outcome", "Issue vue par l'appelant", ""),
        ("time_saved_ms", "Attente évitée par l'échéance", "ms"),
        ("server_executed", "Appel abandonné exécuté par le serveur", ""),
        ("stock_before", "Stock avant l'expérience", ""),
        ("stock_after", "Stock après l'expérience", ""),
        ("naive_outcome", "Issue du client naïf", ""),
        ("naive_error", "Exception levée chez le client naïf", ""),
        ("naive_ms", "Durée de l'appel naïf", "ms"),
        ("resilient_outcome", "Issue du client résilient", ""),
        ("resilient_attempts", "Tentatives du client résilient", ""),
        ("resilient_backoff_ms", "Attente entre les tentatives", "ms"),
        ("resilient_total_ms", "Durée totale du client résilient", "ms"),
        ("failures_before_open", "Échecs avant l'ouverture du disjoncteur", ""),
        ("slow_fail_ms", "Échec après épuisement des tentatives", "ms"),
        ("slow_fail_outcome", "Issue de l'appel qui réessaie", ""),
        ("fast_fail_ms", "Refus immédiat par le disjoncteur", "ms"),
        ("fast_fail_outcome", "Issue des appels refusés", ""),
        ("fast_fail_calls", "Appels refusés sans toucher au réseau", ""),
        ("fast_fail_speedup_x", "Échec rapide contre échec lent", "×"),
        ("breaker_opened", "Disjoncteur ouvert pendant la panne", ""),
        ("recovery_probes", "Appels d'essai avant la reprise", ""),
        ("recovered", "Service rétabli", ""),
        ("breaker_state", "État final du disjoncteur", ""),
        ("expected_after", "Stock attendu (une seule exécution)", ""),
        ("naive_after", "Stock après retry sans clé", ""),
        ("naive_executions", "Exécutions sans clé d'idempotence", ""),
        ("naive_attempts", "Tentatives sans clé d'idempotence", ""),
        ("idempotent_after", "Stock après retry avec clé", ""),
        ("idempotent_executions", "Exécutions avec clé d'idempotence", ""),
        ("idempotent_attempts", "Tentatives avec clé d'idempotence", ""),
        ("deduplicated", "Rejeu reconnu par le serveur", ""),
    )
}


def _find_scenario(scenario_id: str) -> _Scenario:
    for scenario in _SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    known = ", ".join(scenario.id for scenario in _SCENARIOS)
    raise ValueError(f"Scénario inconnu : {scenario_id!r} (attendu : {known})")


def _require_protocol(protocol: str) -> None:
    if protocol not in REMOTE_PROTOCOLS:
        raise ValueError(
            f"Protocole inconnu : {protocol!r} (attendu : {', '.join(REMOTE_PROTOCOLS)} — "
            "un scénario de panne a besoin d'un réseau à dérégler)"
        )


def _resolve_options(scenario: _Scenario, options: dict[str, Any] | None) -> dict[str, float]:
    """Fusionne ``options`` avec les valeurs par défaut du scénario, après contrôle."""
    resolved = {name: option.default for name, option in scenario.options.items()}
    for name, value in (options or {}).items():
        spec = scenario.options.get(name)
        if spec is None:
            accepted = ", ".join(scenario.options) or "aucune"
            raise ValueError(f"Option inconnue pour « {scenario.id} » : {name!r} (acceptées : {accepted})")
        accepted_types = int if spec.whole else (int, float)
        # bool est un sous-type d'int : « calls=True » passerait sans ce garde-fou.
        if isinstance(value, bool) or not isinstance(value, accepted_types) or not math.isfinite(value):
            kind = "un entier" if spec.whole else "un nombre"
            raise ValueError(f"« {name} » doit être {kind} (reçu : {value!r})")
        if not spec.minimum <= value <= spec.maximum:
            raise ValueError(
                f"« {name} » doit être compris entre {_fr(spec.minimum)} et {_fr(spec.maximum)} (reçu : {value!r})"
            )
        resolved[name] = value
    if scenario.check is not None:
        scenario.check(resolved)
    return resolved


# --- API publique --------------------------------------------------------------------

def run_scenario(
    runtime: "LabRuntime",
    scenario_id: str,
    *,
    protocol: str = "custom",
    on_step: StepCallback | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Joue un scénario de panne contre le laboratoire démarré ``runtime``.

    Renvoie ``{id, title, protocol, summary, verdict, lesson, metrics, steps, options,
    duration_ms}``. ``steps`` est la chronologie ; chaque étape est aussi remise à ``on_step``
    dès qu'elle survient, depuis un fil dédié et dans l'ordre. Une exception levée par
    ``on_step`` interrompt le scénario et en ressort. ``options`` surcharge les réglages de
    ``DEFAULT_OPTIONS[scenario_id]``.

    Les étapes ``attempt``, ``backoff``, ``breaker`` et les pannes du proxy sont relayées
    depuis le bus de traces du laboratoire : s'il est coupé (``bus.muted()``), elles manquent
    à la chronologie, mais les mesures et le verdict n'en dépendent pas.

    Lève ``ValueError`` pour un scénario, un protocole ou une option inconnus, ``RuntimeError``
    si le laboratoire n'est pas démarré, et l'``RpcError`` d'un appel dont l'expérience ne
    peut pas se passer. Dans tous les cas, conditions réseau, pannes armées et stocks sont
    remis dans leur état initial.
    """
    scenario = _find_scenario(scenario_id)
    _require_protocol(protocol)
    settings = _resolve_options(scenario, options)
    if not runtime.started:
        raise RuntimeError("Le laboratoire doit être démarré (runtime.start()) avant de jouer un scénario de panne.")
    with _RUN_LOCK, ExitStack() as cleanup:
        timeline = _Timeline(on_step)
        cleanup.callback(timeline.close)
        # Étape-repère inscrite avant toute écoute du bus : elle est toujours la première du scénario.
        timeline.add("note", f"{scenario.title} — {PROTOCOL_LABELS[protocol]}", "info",
                     {"scenario": scenario.id, "protocol": protocol})
        session = _Session(runtime, protocol, settings, timeline)
        cleanup.callback(session.restore)
        metrics, verdict = scenario.play(session)
    timeline.check()    # erreur de l'observateur survenue sur les toutes dernières étapes
    return {
        "id": scenario.id,
        "title": scenario.title,
        "protocol": protocol,
        "summary": scenario.summary,
        "verdict": verdict,
        "lesson": scenario.lesson,
        "metrics": metrics,
        "steps": timeline.steps,
        "options": settings,
        "duration_ms": timeline.elapsed_ms(),
    }


def run_all(
    runtime: "LabRuntime",
    protocol: str = "custom",
    on_step: StepCallback | None = None,
    options: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Joue les cinq scénarios dans l'ordre de ``SCENARIOS`` ; renvoie leurs résultats.

    ``options`` associe un identifiant de scénario à ses réglages ; tout est contrôlé avant
    de jouer le premier scénario. Chaque scénario commence par une étape ``note`` portant
    ``detail.scenario`` : c'est le repère qui sépare les scénarios dans le flux d'étapes
    remis à ``on_step``.
    """
    options = options or {}
    _require_protocol(protocol)
    for scenario_id, settings in options.items():
        _resolve_options(_find_scenario(scenario_id), settings)
    return [
        run_scenario(runtime, scenario.id, protocol=protocol, on_step=on_step, options=options.get(scenario.id))
        for scenario in _SCENARIOS
    ]


# --- Interface console ---------------------------------------------------------------

ACCENT = "#8B7CFF"
SUCCESS = "#4ADE80"
WARNING = "#FBBF24"
DANGER = "#F87171"
INFO = "#60A5FA"
MUTED = "grey58"

MAX_WIDTH = 118

_STATUS_MARKS: dict[str, tuple[str, str]] = {
    "ok": ("✓", SUCCESS),
    "slow": ("◷", WARNING),
    "timeout": ("✗", DANGER),
    "error": ("✗", DANGER),
    "retry": ("↻", WARNING),
    "info": ("·", MUTED),
    "open": ("⊘", DANGER),
    "dedup": ("≡", INFO),
}
_KIND_LABELS: dict[str, str] = {
    "local_call": "appel local",
    "rpc_call": "appel RPC",
    "attempt": "tentative",
    "backoff": "attente",
    "breaker": "disjoncteur",
    "network": "réseau",
    "state": "état",
    "note": "note",
}


def _format_metric(value: Any, unit: str) -> str:
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, str):
        return value or "—"
    if unit == "×":
        return f"×{_fr(value)}"
    return f"{_fr(value)} {unit}".rstrip()


class _ConsoleReport:
    """Rendu console d'un scénario : les étapes à mesure qu'elles surviennent, puis mesures et verdict."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def banner(self, protocol: str) -> None:
        title = Text.assemble((APP_NAME, f"bold {ACCENT}"), ("  ·  Laboratoire de pannes", "bold"))
        subtitle = Text(
            "Un appel distant n'est pas un appel local : latence, échéances, coupures, pannes, doublons.\n"
            f"Middleware mis à l'épreuve : {PROTOCOL_LABELS[protocol]} — chaque appel traverse le proxy de chaos.",
            style=MUTED,
        )
        self.console.print()
        self.console.print(Panel(Group(title, subtitle), border_style=ACCENT, box=box.ROUNDED, padding=(1, 2)))

    def heading(self, rank: int, scenario: _Scenario) -> None:
        self.console.print()
        self.console.print(Rule(Text(f" {rank} · {scenario.title} ", style=f"bold {ACCENT}"), style=ACCENT,
                                align="left"))
        self.console.print(Text.assemble((f"{scenario.concept} — ", "bold"), (scenario.summary, MUTED)))
        self.console.print()

    def step(self, step: Step) -> None:
        if "scenario" in step["detail"]:
            return      # étape-repère de début de scénario : l'en-tête vient de le dire
        mark, color = _STATUS_MARKS[step["status"]]
        duration_ms = step["detail"].get("duration_ms")
        row = Table.grid(padding=(0, 1), expand=True)
        row.add_column(width=11, justify="right", style=MUTED)
        row.add_column(width=1)
        row.add_column(width=11)
        row.add_column(ratio=1)
        row.add_column(width=11, justify="right", style=MUTED)
        row.add_row(
            f"+{_fr(step['t_ms'], 0)} ms",
            Text(mark, style=f"bold {color}"),
            Text(_KIND_LABELS[step["kind"]], style=color),
            Text(step["label"]),
            "" if duration_ms is None else f"{_fr(duration_ms)} ms",
        )
        self.console.print(row)

    def conclusion(self, result: dict[str, Any]) -> None:
        table = Table("Mesure", "Valeur", box=box.SIMPLE_HEAD, header_style="bold", border_style=MUTED,
                      pad_edge=False)
        for name, value in result["metrics"].items():
            info = METRIC_INFO[name]
            table.add_row(info["label"], Text(_format_metric(value, info["unit"]), justify="right"))
        self.console.print()
        self.console.print(table)
        body = Group(
            Text(result["verdict"], style="bold"),
            Text(),
            Text.assemble(("À retenir — ", f"bold {ACCENT}"), result["lesson"]),
        )
        self.console.print(Panel(body, title="Verdict", title_align="left", border_style=ACCENT, box=box.ROUNDED,
                                 padding=(1, 2)))

    def failure(self, error: Exception) -> None:
        self.console.print()
        self.console.print(Text.assemble(("  ✗ Scénario interrompu — ", f"bold {DANGER}"), str(error)))
        self.console.print(Text("    Réseau, stocks et proxy ont été remis dans leur état initial.", style=MUTED))


def main(argv: list[str] | None = None) -> int:
    # La console Windows n'est pas en UTF-8 par défaut : accents et filets seraient illisibles.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="python -m benchmark_lab.failure_simulation",
        description="Laboratoire de pannes : ce qui sépare, mesures à l'appui, un appel distant d'un appel local.",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="affiche cette aide et quitte")
    parser.add_argument("--scenario", metavar="ID",
                        help=f"scénario à jouer : {', '.join(scenario.id for scenario in _SCENARIOS)} "
                             "(par défaut : tous, dans l'ordre)")
    parser.add_argument("--protocol", metavar="P", default="custom",
                        help=f"middleware mis à l'épreuve : {', '.join(REMOTE_PROTOCOLS)} (par défaut : custom)")
    args = parser.parse_args(argv)
    try:
        chosen = _SCENARIOS if args.scenario is None else (_find_scenario(args.scenario),)
        _require_protocol(args.protocol)
    except ValueError as exc:
        parser.error(str(exc))

    width = min(shutil.get_terminal_size((MAX_WIDTH, 40)).columns, MAX_WIDTH)
    # markup=False : les libellés contiennent des crochets et des parenthèses qui ne sont pas du balisage.
    report = _ConsoleReport(Console(width=width, highlight=False, markup=False))

    # Importé ici : jouer un scénario n'exige aucun middleware, seulement un laboratoire déjà construit.
    from rpc_grpc.generate import ensure_generated

    ensure_generated()
    from lab import LabRuntime

    report.banner(args.protocol)
    started = time.perf_counter()
    try:
        # Ports éphémères et bus privé : la démonstration cohabite avec un laboratoire déjà ouvert.
        with LabRuntime.ephemeral(bus=EventBus()) as runtime:
            for rank, scenario in enumerate(chosen, start=1):
                report.heading(rank, scenario)
                report.conclusion(run_scenario(runtime, scenario.id, protocol=args.protocol, on_step=report.step))
    except (RpcError, RuntimeError) as error:
        report.failure(error)
        return 1
    except KeyboardInterrupt:
        report.failure(RuntimeError("arrêt demandé au clavier (Ctrl+C)"))
        return 130
    elapsed_s = time.perf_counter() - started
    report.console.print()
    report.console.print(Text(
        f"  {_plural(len(chosen), 'scénario')} en {_fr(elapsed_s, 1)} s — réseau, stocks et proxy "
        "remis dans leur état initial.",
        style=MUTED,
    ))
    report.console.print()
    return 0


__all__ = [
    "DEFAULT_OPTIONS",
    "METRIC_INFO",
    "SCENARIOS",
    "STEP_KINDS",
    "STEP_STATUSES",
    "Step",
    "StepCallback",
    "main",
    "run_all",
    "run_scenario",
]


if __name__ == "__main__":
    raise SystemExit(main())
