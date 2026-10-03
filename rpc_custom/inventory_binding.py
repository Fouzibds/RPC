"""Branchement du service d'inventaire sur le middleware RPC maison.

Le middleware (``client_stub.py``, ``server_skeleton.py``) ne sait rien de
l'inventaire, et ``InventoryService`` ne sait rien du réseau. Ce module les
relie, des deux côtés :

* serveur : ``build_inventory_skeleton`` enregistre les procédures du service ;
* client  : ``CustomInventoryClient`` habille le stub de l'interface commune
  ``InventoryClient``, celle que partagent l'appel local, gRPC et REST.

``build_inventory_skeleton_v2`` sert le même service après une évolution de
contrat NON coordonnée avec les clients. JSON-RPC n'a pas d'IDL : rien ne
signale la rupture à la compilation, elle n'apparaît qu'à l'exécution.
"""
from __future__ import annotations

from concurrent.futures import Future
from typing import Any, Iterator

from common.catalog import METHODS, method_spec
from common.client_api import InventoryClient
from common.config import DEFAULT_TIMEOUT_S, HOST
from common.errors import InvalidArgument
from common.inventory import InventoryService
from common.telemetry import BUS, EventBus

from .client_stub import RpcClientStub
from .server_skeleton import RpcServerSkeleton

# Les sept procédures du catalogue : cinq communes à tous les protocoles, deux
# réservées aux flux gRPC mais appelables ici de façon unaire (via ``client.stub``).
INVENTORY_PROCEDURES: tuple[str, ...] = tuple(spec.name for spec in METHODS)


# --- Côté serveur ------------------------------------------------------------------

def build_inventory_skeleton(
    service: InventoryService, host: str = HOST, port: int = 0, *, bus: EventBus = BUS
) -> RpcServerSkeleton:
    """Squelette exposant ``service`` (contrat v1) ; reste à appeler ``start()``."""
    skeleton = RpcServerSkeleton(host, port, name="custom", bus=bus)
    skeleton.register_instance(service, INVENTORY_PROCEDURES)
    return skeleton


def build_inventory_skeleton_v2(
    service: InventoryService, host: str = HOST, port: int = 0, *, bus: EventBus = BUS
) -> RpcServerSkeleton:
    """Squelette « contrat v2 » : le service redéployé sans prévenir les clients v1."""
    skeleton = RpcServerSkeleton(host, port, name="custom", bus=bus)
    skeleton.register_instance(_InventoryV2(service), _InventoryV2.PROCEDURES)
    skeleton.register_instance(service, ("stream_analytics", "bulk_update_stock", "check_stock"))
    return skeleton


class _InventoryV2:
    """Le contrat v2 : quatre évolutions, dont trois cassent les clients v1."""

    PROCEDURES = ("calculate_factorial", "get_product", "update_stock", "list_products")

    def __init__(self, service: InventoryService) -> None:
        self._service = service

    def calculate_factorial(self, n: int) -> dict[str, Any]:
        """[COMPATIBLE] Une clé de plus dans le résultat : un client v1 l'ignore."""
        return {**self._service.calculate_factorial(n), "algorithm": "binary_split"}

    def get_product(self, product_id: str) -> dict[str, Any]:
        """[RUPTURE] Remplace ``get_product_details`` : l'ancien nom n'existe plus (-32601)."""
        return _to_v2_product(self._service.get_product_details(product_id))

    def update_stock(self, product_id: str, delta: int, warehouse: str, idempotency_key: str = "") -> dict[str, Any]:
        """[RUPTURE] ``warehouse`` devient obligatoire : un appel v1 ne se lie plus à la signature (-32602)."""
        if not isinstance(warehouse, str) or not warehouse:
            raise InvalidArgument("« warehouse » est obligatoire", param="warehouse")
        return {**self._service.update_stock(product_id, delta, idempotency_key), "warehouse": warehouse}

    def list_products(self, limit: int = 20, category: str = "") -> dict[str, Any]:
        """[RUPTURE] Clés renommées dans le résultat : l'appel réussit, c'est le code client qui casse."""
        listing = self._service.list_products(limit, category)
        return {**listing, "products": [_to_v2_product(product) for product in listing["products"]]}


def _to_v2_product(product: dict[str, Any]) -> dict[str, Any]:
    """Fiche produit v2 : ``price`` → ``price_cents`` (entier), ``stock`` → ``quantity``."""
    converted = dict(product)
    converted["price_cents"] = round(converted.pop("price") * 100)
    converted["quantity"] = converted.pop("stock")
    return converted


# --- Côté client -------------------------------------------------------------------

class CustomInventoryClient(InventoryClient):
    """``InventoryClient`` au-dessus du stub maison.

    Les paramètres voyagent NOMMÉS : le message est plus long que s'ils étaient
    positionnels, mais il se lit sans connaître la signature et ne dépend pas
    de l'ordre des arguments.
    """

    protocol = "custom"

    def __init__(self, host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT_S, bus: EventBus = BUS) -> None:
        self.stub = RpcClientStub(host, port, timeout=timeout, bus=bus, protocol=self.protocol)

    def calculate_factorial(self, n: int, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call("calculate_factorial", timeout, n=n)

    def get_product_details(self, product_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call("get_product_details", timeout, product_id=product_id)

    def update_stock(
        self, product_id: str, delta: int, *, idempotency_key: str = "", timeout: float | None = None
    ) -> dict[str, Any]:
        return self._call(
            "update_stock", timeout, product_id=product_id, delta=delta, idempotency_key=idempotency_key
        )

    def list_products(self, limit: int = 20, category: str = "", *, timeout: float | None = None) -> dict[str, Any]:
        return self._call("list_products", timeout, limit=limit, category=category)

    def stream_analytics(
        self, samples: int = 10, interval_ms: int = 200, *, timeout: float | None = None
    ) -> Iterator[dict[str, Any]]:
        """Flux serveur : ``timeout`` borne l'attente entre deux instantanés."""
        stream = self.stub.stream("stream_analytics", timeout=timeout, samples=samples, interval_ms=interval_ms)
        self.last_call_id = self.stub.last_call_id
        return stream

    def submit(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Future:
        """Appel asynchrone réellement multiplexé : la requête part tout de suite, sur la connexion partagée.

        Aucun thread n'est mobilisé par appel en attente, contrairement à
        l'implémentation par défaut (un pool de threads).
        """
        spec = method_spec(method)
        if spec is None or spec.kind != "unary" or self.protocol not in spec.protocols:
            return super().submit(method, params, timeout=timeout)
        future = self.stub.call_async(method, timeout=timeout, **(params or {}))
        self.last_call_id = future.call_id
        return future

    def close(self) -> None:
        self.stub.close()
        super().close()

    def _call(self, method: str, timeout: float | None, **params: Any) -> Any:
        future = self.stub.call_async(method, timeout=timeout, **params)
        self.last_call_id = future.call_id
        return future.result()
