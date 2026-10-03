"""Interface cliente unique, implémentée par chaque middleware.

Que l'on parle à l'objet local, au stub JSON-RPC maison, à gRPC ou à l'API
REST, le code appelant est IDENTIQUE — c'est ce qui rend les comparaisons du
banc d'essai équitables, et c'est la « transparence de localisation » en acte.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Iterable, Iterator

from .catalog import method_spec
from .errors import (
    DomainError,
    MethodNotFoundError,
    RpcError,
    error_from_code,
)
from .inventory import InventoryService
from .telemetry import BUS, EventBus, new_call_id


class InventoryClient(ABC):
    """Contrat commun. Toutes les méthodes lèvent des ``common.errors.RpcError``.

    Convention : chaque implémentation affecte ``self.last_call_id`` au début
    de chaque appel (identifiant de corrélation publié sur le bus de traces).
    """

    protocol: str = ""
    last_call_id: str = ""
    _executor: ThreadPoolExecutor | None = None

    @abstractmethod
    def calculate_factorial(self, n: int, *, timeout: float | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def get_product_details(self, product_id: str, *, timeout: float | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def update_stock(
        self, product_id: str, delta: int, *, idempotency_key: str = "", timeout: float | None = None
    ) -> dict[str, Any]: ...

    @abstractmethod
    def list_products(
        self, limit: int = 20, category: str = "", *, timeout: float | None = None
    ) -> dict[str, Any]: ...

    @abstractmethod
    def stream_analytics(
        self, samples: int = 10, interval_ms: int = 200, *, timeout: float | None = None
    ) -> Iterator[dict[str, Any]]: ...

    # -- appels génériques ----------------------------------------------------

    def invoke(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        """Appelle une procédure par son nom (utilisé par le CLI et le dashboard)."""
        target = getattr(self, method, None) if method_spec(method) else None
        if target is None:
            raise MethodNotFoundError(
                f"Procédure inconnue ou non disponible via {self.protocol} : {method}",
                protocol=self.protocol,
                method=method,
            )
        return target(**(params or {}), timeout=timeout)

    def submit(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Future:
        """Appel asynchrone : renvoie immédiatement un ``Future``.

        Implémentation par défaut sur un pool de threads ; les stubs capables
        de multiplexer plusieurs appels sur une connexion la surchargent.
        """
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix=f"{self.protocol}-async")
        return self._executor.submit(self.invoke, method, params, timeout=timeout)

    # -- cycle de vie ---------------------------------------------------------

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def __enter__(self) -> "InventoryClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class LocalInventoryClient(InventoryClient):
    """Référence « appel local » : aucune sérialisation, aucun réseau.

    Sert de point zéro dans tous les benchmarks. Les erreurs métier sont
    converties en ``RpcError`` pour que le code appelant reste le même.
    """

    protocol = "local"

    def __init__(self, service: InventoryService, *, bus: EventBus = BUS) -> None:
        self.service = service
        self._bus = bus

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        bus = self._bus
        if not bus.enabled:
            try:
                return getattr(self.service, method)(*args, **kwargs)
            except DomainError as exc:
                raise error_from_code(exc.code, exc.message, protocol="local", method=method, detail=exc.data) from exc
        call_id = self.last_call_id = new_call_id("local")
        started = time.perf_counter_ns()
        bus.emit(call_id=call_id, protocol="local", side="client", stage="client.call", method=method,
                 detail={"args": list(args), "kwargs": kwargs})
        try:
            result = getattr(self.service, method)(*args, **kwargs)
        except DomainError as exc:
            error = error_from_code(exc.code, exc.message, protocol="local", method=method, detail=exc.data)
            bus.emit(call_id=call_id, protocol="local", side="client", stage="client.error", method=method,
                     duration_us=(time.perf_counter_ns() - started) / 1000,
                     detail={"code": error.code, "message": error.message})
            raise error from exc
        bus.emit(call_id=call_id, protocol="local", side="client", stage="client.return", method=method,
                 duration_us=(time.perf_counter_ns() - started) / 1000)
        return result

    def calculate_factorial(self, n: int, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call("calculate_factorial", n)

    def get_product_details(self, product_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call("get_product_details", product_id)

    def update_stock(
        self, product_id: str, delta: int, *, idempotency_key: str = "", timeout: float | None = None
    ) -> dict[str, Any]:
        return self._call("update_stock", product_id, delta, idempotency_key)

    def list_products(self, limit: int = 20, category: str = "", *, timeout: float | None = None) -> dict[str, Any]:
        return self._call("list_products", limit, category)

    def stream_analytics(
        self, samples: int = 10, interval_ms: int = 200, *, timeout: float | None = None
    ) -> Iterator[dict[str, Any]]:
        return self._call("stream_analytics", samples, interval_ms)

    def bulk_update_stock(self, updates: Iterable[dict[str, Any]], *, timeout: float | None = None) -> dict[str, Any]:
        return self._call("bulk_update_stock", list(updates))

    def check_stock(self, product_ids: Iterable[str], *, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        for product_id in product_ids:
            yield self._call("check_stock", product_id)


__all__ = ["InventoryClient", "LocalInventoryClient", "RpcError"]
