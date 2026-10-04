"""Résilience côté client : échéance, nouvelles tentatives, disjoncteur, clé d'idempotence.

Un appel distant peut échouer d'une façon qu'un appel local ignore : sans réponse, l'appelant
ne sait pas si le serveur a exécuté la procédure. Ce module rend explicites les parades
classiques — et leurs pièges :

* ``RetryPolicy``    — réessayer, en espaçant les tentatives pour ne pas accabler un serveur fragile ;
* ``CircuitBreaker`` — cesser d'appeler un serveur manifestement en panne, puis le tester prudemment ;
* clé d'idempotence  — rendre un rejeu inoffensif quand la procédure, elle, ne l'est pas.

``ResilientClient`` enveloppe n'importe quel ``InventoryClient`` et publie chacune de ses
décisions sur le bus de traces (étapes ``resilience.*``).
"""
from __future__ import annotations

import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from common.catalog import method_spec
from common.client_api import InventoryClient
from common.errors import (
    CIRCUIT_OPEN,
    OK,
    TIMEOUT,
    UNAVAILABLE,
    CircuitOpenError,
    MethodNotFoundError,
    RpcError,
)
from common.telemetry import BUS, EventBus

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

# Seules ces erreurs renseignent sur la santé du serveur : il n'a pas répondu. Une erreur
# métier (produit inconnu…) prouve au contraire qu'il est bien vivant.
_TRANSPORT_FAILURES: frozenset[str] = frozenset({TIMEOUT, UNAVAILABLE})

_BREAKER_TEXT: dict[str, str] = {
    OPEN: "Disjoncteur ouvert : les appels sont refusés sans toucher au réseau.",
    HALF_OPEN: "Disjoncteur semi-ouvert : un seul appel d’essai est autorisé.",
    CLOSED: "Disjoncteur refermé : le serveur répond de nouveau.",
}
_GIVE_UP_TEXT: dict[str, str] = {
    "max_attempts": "Nombre maximal de tentatives atteint : l’erreur est remontée à l’appelant.",
    "non_idempotent": "Procédure non idempotente sans clé d’idempotence : la rejouer risquerait un doublon.",
    "stream_started": "Le flux avait commencé : le rejouer livrerait des éléments en double.",
    "circuit_open": "Disjoncteur ouvert : appel refusé sans toucher au réseau.",
}
_DEDUP_TEXT = "Le serveur a reconnu la clé d’idempotence : l’effet n’a pas été appliqué une seconde fois."


@dataclass
class RetryPolicy:
    """Quoi rejouer, combien de fois, et après quelle attente."""

    max_attempts: int = 3             # tentatives au total, la première comprise
    base_delay_ms: float = 100        # attente après le premier échec
    multiplier: float = 2.0           # facteur appliqué à chaque échec suivant
    max_delay_ms: float = 2000        # plafond de l'attente
    jitter: float = 0.2               # dispersion aléatoire (0.2 = ± 20 %)
    retry_on: tuple[str, ...] = (TIMEOUT, UNAVAILABLE)
    retry_non_idempotent: bool = True  # rejouer aussi les écritures — au risque du doublon

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError(f"« max_attempts » doit être un entier ≥ 1 (reçu : {self.max_attempts!r})")
        if self.base_delay_ms < 0 or self.max_delay_ms < 0:
            raise ValueError("« base_delay_ms » et « max_delay_ms » doivent être ≥ 0")
        if self.multiplier < 1:
            raise ValueError(f"« multiplier » doit être ≥ 1 (reçu : {self.multiplier!r})")
        if not 0 <= self.jitter <= 1:
            raise ValueError(f"« jitter » doit être compris entre 0 et 1 (reçu : {self.jitter!r})")
        self.retry_on = tuple(self.retry_on)

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Attente (ms) après l'échec de la tentative n° ``attempt`` (numérotée à partir de 1).

        Exponentielle et plafonnée, puis dispersée de ± ``jitter`` : sans cette dispersion,
        tous les clients d'un serveur qui redémarre reviendraient à la charge au même instant.
        """
        try:
            delay = min(self.base_delay_ms * self.multiplier ** (max(attempt, 1) - 1), self.max_delay_ms)
        except OverflowError:
            delay = self.max_delay_ms
        if self.jitter:
            delay *= 1 + (rng or random).uniform(-self.jitter, self.jitter)
        return delay


class CircuitBreaker:
    """Disjoncteur à trois états, sûr vis-à-vis des threads.

    ``closed`` : les appels passent ; ``failure_threshold`` échecs de transport consécutifs
    l'ouvrent. ``open`` : les appels sont refusés sans toucher au réseau. Après
    ``reset_timeout_s`` il devient ``half_open`` et laisse passer UN appel d'essai : son
    succès referme le disjoncteur, son échec le rouvre.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_timeout_s: float = 2.0,
        name: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(f"« failure_threshold » doit être ≥ 1 (reçu : {failure_threshold!r})")
        if reset_timeout_s < 0:
            raise ValueError(f"« reset_timeout_s » doit être ≥ 0 (reçu : {reset_timeout_s!r})")
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_at: float | None = None   # départ de l'appel d'essai en cours
        self._opened_count = 0
        self._rejected = 0
        self._transitions: deque[dict[str, Any]] = deque(maxlen=32)

    @property
    def state(self) -> str:
        with self._lock:
            self._refresh()
            return self._state

    def allow(self) -> bool:
        """L'appel peut-il partir ? En ``half_open``, un seul appel d'essai à la fois."""
        with self._lock:
            now = self._refresh()
            if self._state == CLOSED:
                return True
            # Un essai dont le résultat n'est jamais revenu ne doit pas bloquer le disjoncteur.
            probe_pending = self._probe_at is not None and now - self._probe_at < self.reset_timeout_s
            if self._state == HALF_OPEN and not probe_pending:
                self._probe_at = now
                return True
            self._rejected += 1
            return False

    def record_success(self) -> None:
        """Le serveur a répondu (résultat ou erreur métier)."""
        with self._lock:
            self._refresh()
            if self._state == HALF_OPEN:
                self._opened_at = self._probe_at = None
                self._failures = 0
                self._move(CLOSED)
            elif self._state == CLOSED:
                self._failures = 0

    def record_failure(self) -> None:
        """Le serveur n'a pas répondu (timeout ou connexion impossible)."""
        with self._lock:
            now = self._refresh()
            if self._state == OPEN:
                return  # échec d'un appel parti avant l'ouverture : rien de nouveau à en déduire
            self._failures += 1
            if self._state == HALF_OPEN or self._failures >= self.failure_threshold:
                self._opened_at, self._probe_at = now, None
                self._opened_count += 1
                self._move(OPEN)

    def reset(self) -> None:
        """Referme le disjoncteur et oublie les échecs."""
        with self._lock:
            self._opened_at = self._probe_at = None
            self._failures = 0
            if self._state != CLOSED:
                self._move(CLOSED)

    def snapshot(self) -> dict[str, Any]:
        """État sérialisable, pour les rapports et le dashboard."""
        with self._lock:
            now = self._refresh()
            age = None if self._opened_at is None else now - self._opened_at
            return {
                "name": self.name,
                "state": self._state,
                "failures": self._failures,
                "failure_threshold": self.failure_threshold,
                "reset_timeout_s": self.reset_timeout_s,
                "opened_at_age_s": None if age is None else round(age, 3),
                "retry_in_s": round(max(0.0, self.reset_timeout_s - age), 3) if self._state == OPEN else 0.0,
                "opened_count": self._opened_count,
                "rejected_calls": self._rejected,
            }

    def drain_transitions(self) -> list[dict[str, Any]]:
        """Changements d'état survenus depuis le dernier appel, chacun rendu une seule fois.

        Un disjoncteur peut être partagé entre plusieurs clients : celui qui relève les
        transitions les publie, ce qui garantit un seul évènement par changement d'état.
        """
        with self._lock:
            self._refresh()
            transitions = list(self._transitions)
            self._transitions.clear()
            return transitions

    # -- interne (verrou tenu par l'appelant) ---------------------------------

    def _refresh(self) -> float:
        """Passe de ``open`` à ``half_open`` une fois le délai écoulé ; renvoie l'heure courante."""
        now = self._clock()
        if self._state == OPEN and now - self._opened_at >= self.reset_timeout_s:
            self._probe_at = None
            self._move(HALF_OPEN)
        return now

    def _move(self, state: str) -> None:
        self._transitions.append({
            "name": self.name,
            "from": self._state,
            "to": state,
            "failures": self._failures,
            "failure_threshold": self.failure_threshold,
            "reset_timeout_s": self.reset_timeout_s,
        })
        self._state = state


@dataclass
class _Call:
    """Suivi d'un appel logique, toutes tentatives confondues."""

    method: str
    idempotency_key: str
    started: float
    attempts: list[dict[str, Any]] = field(default_factory=list)
    last_error: RpcError | None = None


class ResilientClient(InventoryClient):
    """``InventoryClient`` qui ajoute échéance, retries et disjoncteur à un autre client.

    Le code appelant ne change pas : c'est le même contrat, donc la même « transparence »,
    mais les pannes du réseau y sont enfin traitées.

    * ``timeout`` — échéance de chaque tentative (``None`` : celle du client enveloppé) ;
      un ``timeout=`` passé à l'appel a priorité ;
    * ``retry`` — politique de nouvelles tentatives ; sans elle, exactement une tentative ;
    * ``breaker`` — disjoncteur consulté avant chaque tentative (partageable entre clients) ;
    * ``auto_idempotency_key`` — attribue une clé d'idempotence à chaque ``update_stock`` ;
    * ``sleep`` / ``rng`` — attente et hasard injectables, pour des tests instantanés.

    ``last_report`` décrit le dernier appel terminé :
    ``{"method", "outcome", "total_ms", "breaker", "attempts": [{"n", "call_id", "outcome",
    "code", "duration_ms", "backoff_ms"}]}`` avec ``outcome`` parmi ``ok`` (succès), ``error``
    (erreur non rejouable), ``gave_up`` (abandon des tentatives) et ``circuit_open`` (refus du
    disjoncteur).
    """

    def __init__(
        self,
        inner: InventoryClient,
        *,
        timeout: float | None = None,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        auto_idempotency_key: bool = False,
        bus: EventBus = BUS,
        sleep: Callable[[float], Any] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.inner = inner
        self.protocol = inner.protocol
        self.timeout = timeout
        self.retry = retry
        self.breaker = breaker
        self.auto_idempotency_key = auto_idempotency_key
        self.last_report: dict[str, Any] = {}
        self._bus = bus
        self._sleep = sleep
        self._rng = rng or random.Random()

    # -- procédures -----------------------------------------------------------

    def calculate_factorial(self, n: int, *, timeout: float | None = None) -> dict[str, Any]:
        return self._unary("calculate_factorial", lambda t: self.inner.calculate_factorial(n, timeout=t), timeout)

    def get_product_details(self, product_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        return self._unary(
            "get_product_details", lambda t: self.inner.get_product_details(product_id, timeout=t), timeout
        )

    def update_stock(
        self, product_id: str, delta: int, *, idempotency_key: str = "", timeout: float | None = None
    ) -> dict[str, Any]:
        key = idempotency_key
        if not key and self.auto_idempotency_key:
            # UNE clé par appel logique, réutilisée à chaque tentative : c'est elle qui permet
            # au serveur de reconnaître un rejeu et de ne pas appliquer l'effet deux fois.
            key = uuid.uuid4().hex
        return self._unary(
            "update_stock",
            lambda t: self.inner.update_stock(product_id, delta, idempotency_key=key, timeout=t),
            timeout,
            idempotency_key=key,
        )

    def list_products(self, limit: int = 20, category: str = "", *, timeout: float | None = None) -> dict[str, Any]:
        return self._unary("list_products", lambda t: self.inner.list_products(limit, category, timeout=t), timeout)

    def stream_analytics(
        self, samples: int = 10, interval_ms: int = 200, *, timeout: float | None = None
    ) -> Iterator[dict[str, Any]]:
        return self._stream(
            "stream_analytics", lambda t: self.inner.stream_analytics(samples, interval_ms, timeout=t), timeout
        )

    def bulk_update_stock(self, updates: Iterable[dict[str, Any]], *, timeout: float | None = None) -> dict[str, Any]:
        target = self._optional("bulk_update_stock")
        batch = list(updates)  # un flux client n'est rejouable que s'il a été mémorisé
        return self._unary("bulk_update_stock", lambda t: target(batch, timeout=t), timeout)

    def check_stock(self, product_ids: Iterable[str], *, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        target = self._optional("check_stock")
        references = list(product_ids)
        return self._stream("check_stock", lambda t: target(references, timeout=t), timeout)

    def connect(self, timeout: float | None = None) -> bool:
        return self.inner.connect(timeout)

    def close(self) -> None:
        self.inner.close()
        super().close()

    # -- boucles de tentatives ------------------------------------------------

    def _unary(
        self, method: str, invoke: Callable[[float | None], Any], timeout: float | None, *, idempotency_key: str = ""
    ) -> Any:
        call = _Call(method, idempotency_key, time.perf_counter())
        timeout = self.timeout if timeout is None else timeout
        while True:
            self._pass_breaker(call)
            mark = self._mark()
            try:
                result = invoke(timeout)
            except RpcError as error:
                if self._retry_after(call, mark, error, replayable=True):
                    continue
                raise
            attempt = self._attempt_done(call, mark, "ok", OK)
            if isinstance(result, dict) and result.get("applied") is False:
                self._emit("resilience.dedup", call, attempt["call_id"], {
                    "n": attempt["n"],
                    "idempotency_key": idempotency_key,
                    "text": _DEDUP_TEXT,
                })
            self._finish(call, "ok")
            return result

    def _stream(
        self, method: str, open_stream: Callable[[float | None], Iterable[dict[str, Any]]], timeout: float | None
    ) -> Iterator[dict[str, Any]]:
        call = _Call(method, "", time.perf_counter())
        timeout = self.timeout if timeout is None else timeout
        while True:
            self._pass_breaker(call)
            mark = self._mark()
            delivered = 0
            stream: Iterable[dict[str, Any]] = ()
            try:
                stream = open_stream(timeout)
                for item in stream:
                    delivered += 1
                    yield item
            except RpcError as error:
                # Un flux déjà entamé ne se rejoue pas : l'appelant recevrait des doublons.
                if self._retry_after(call, mark, error, replayable=delivered == 0):
                    continue
                raise
            except GeneratorExit:
                # L'appelant s'arrête avant la fin : on libère le flux sous-jacent.
                close = getattr(stream, "close", None)
                if close is not None:
                    close()
                self._attempt_done(call, mark, "ok", OK)
                self._finish(call, "ok")
                raise
            self._attempt_done(call, mark, "ok", OK)
            self._finish(call, "ok")
            return

    # -- décisions ------------------------------------------------------------

    def _pass_breaker(self, call: _Call) -> None:
        """Consulte le disjoncteur AVANT de toucher au réseau ; lève ``CircuitOpenError`` s'il refuse."""
        breaker = self.breaker
        if breaker is None:
            return
        allowed = breaker.allow()
        self._publish_breaker(call, "")
        if allowed:
            return
        self._emit("resilience.give_up", call, "", {
            "reason": "circuit_open",
            "attempts": len(call.attempts),
            "code": CIRCUIT_OPEN,
            "text": _GIVE_UP_TEXT["circuit_open"],
        })
        self._finish(call, "circuit_open")
        raise CircuitOpenError(
            f"Disjoncteur ouvert : l’appel à « {call.method} » est refusé sans toucher au réseau",
            protocol=self.protocol,
            method=call.method,
            detail=breaker.snapshot(),
        ) from call.last_error

    def _retry_after(self, call: _Call, mark: tuple[float, str], error: RpcError, *, replayable: bool) -> bool:
        """Consigne l'échec ; vrai si une nouvelle tentative doit suivre (l'attente est alors déjà faite)."""
        attempt = self._attempt_done(call, mark, "error", error.code, error.message)
        call.last_error = error
        reason = self._stop_reason(call, error.code, replayable)
        if reason is None:
            backoff_ms = round(self.retry.delay_for(attempt["n"], self._rng), 3)
            attempt["backoff_ms"] = backoff_ms
            self._emit("resilience.backoff", call, attempt["call_id"], {
                "n": attempt["n"],
                "next_attempt": attempt["n"] + 1,
                "backoff_ms": backoff_ms,
                "code": error.code,
            }, duration_us=backoff_ms * 1000)
            self._sleep(backoff_ms / 1000)
            return True
        if reason == "not_retryable":
            self._finish(call, "error")
            return False
        self._emit("resilience.give_up", call, attempt["call_id"], {
            "reason": reason,
            "attempts": len(call.attempts),
            "code": error.code,
            "text": _GIVE_UP_TEXT[reason],
        })
        self._finish(call, "gave_up")
        return False

    def _stop_reason(self, call: _Call, code: str, replayable: bool) -> str | None:
        """Pourquoi ne pas réessayer ; ``None`` si une nouvelle tentative est permise."""
        policy = self.retry
        if policy is None or code not in policy.retry_on:
            return "not_retryable"
        if not replayable:
            return "stream_started"
        spec = method_spec(call.method)
        idempotent = spec is not None and spec.idempotent
        # Rejouer une écriture n'est sûr que si le serveur sait reconnaître le rejeu (clé
        # d'idempotence) ; sinon c'est un choix explicite de la politique, doublon compris.
        if not (idempotent or policy.retry_non_idempotent or call.idempotency_key):
            return "non_idempotent"
        if len(call.attempts) >= policy.max_attempts:
            return "max_attempts"
        return None

    # -- journal & publication ------------------------------------------------

    def _mark(self) -> tuple[float, str]:
        """Repère de début de tentative : heure et dernier identifiant d'appel connu."""
        return time.perf_counter(), self.inner.last_call_id

    def _attempt_done(
        self, call: _Call, mark: tuple[float, str], outcome: str, code: str, message: str = ""
    ) -> dict[str, Any]:
        """Consigne une tentative terminée et en informe le disjoncteur."""
        started, previous_id = mark
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        self.last_call_id = self.inner.last_call_id
        attempt = {
            "n": len(call.attempts) + 1,
            # Identifiant inchangé : le client enveloppé n'a pas tracé cette tentative.
            "call_id": self.last_call_id if self.last_call_id != previous_id else "",
            "outcome": outcome,
            "code": code,
            "duration_ms": duration_ms,
            "backoff_ms": 0.0,
        }
        call.attempts.append(attempt)
        if self._bus.enabled:  # chemin de chaque appel : rien n'est construit quand le bus est coupé
            self._emit("resilience.attempt", call, attempt["call_id"], {
                "n": attempt["n"],
                "max_attempts": self.retry.max_attempts if self.retry else 1,
                "outcome": outcome,
                "code": code,
                "duration_ms": duration_ms,
                "message": message,
            }, duration_us=duration_ms * 1000)
        if self.breaker is not None:
            if code in _TRANSPORT_FAILURES:
                self.breaker.record_failure()
            else:
                self.breaker.record_success()
            self._publish_breaker(call, attempt["call_id"])
        return attempt

    def _publish_breaker(self, call: _Call, call_id: str) -> None:
        for transition in self.breaker.drain_transitions():
            self._emit("resilience.breaker", call, call_id, {**transition, "text": _BREAKER_TEXT[transition["to"]]})

    def _finish(self, call: _Call, outcome: str) -> None:
        self.last_report = {
            "method": call.method,
            "outcome": outcome,
            "total_ms": round((time.perf_counter() - call.started) * 1000, 3),
            "breaker": self.breaker.snapshot() if self.breaker is not None else None,
            "attempts": call.attempts,
        }

    def _emit(
        self, stage: str, call: _Call, call_id: str, detail: dict[str, Any], duration_us: float | None = None
    ) -> None:
        if self._bus.enabled:
            self._bus.emit(call_id=call_id, protocol=self.protocol, side="resilience", stage=stage,
                           method=call.method, duration_us=duration_us, detail=detail)

    def _optional(self, name: str) -> Callable[..., Any]:
        """Procédure propre à certains middlewares (flux gRPC) : présente ou non chez le client enveloppé."""
        target = getattr(self.inner, name, None)
        if target is None:
            raise MethodNotFoundError(
                f"Procédure non disponible via {self.protocol} : {name}", protocol=self.protocol, method=name
            )
        return target
