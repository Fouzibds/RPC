"""Serveur gRPC « contrat v2 » : le même service, redéployé sans prévenir les clients.

Il sert les messages de ``service_v2.proto`` sous le NOM DE SERVICE DE LA V1
(``rpcexplorer.v1.InventoryService``). Pour un client resté en v1, rien ne
signale le changement : même adresse, mêmes chemins HTTP/2. Les ruptures
n'apparaissent qu'à l'appel, chacune à sa manière :

* ``GetProductDetails`` a été renommée ``GetProduct`` → ``UNIMPLEMENTED`` (rejet franc) ;
* ``UpdateStock`` lit ``delta`` au n°4 ; le client v1 l'envoie au n°2, que la v2
  réserve à une chaîne (``warehouse``). Le type de fil ne correspond pas : le
  champ est ignoré, ``delta`` vaut 0 et l'appel RÉUSSIT sans rien modifier —
  **corruption silencieuse**, le pire des cas ;
* avec ``strict=True``, le serveur exige ``warehouse`` → ``INVALID_ARGUMENT`` :
  une validation applicative transforme la corruption en rejet visible ;
* ``ListProducts`` renvoie des ``Product`` v2 : le client v1 lit un prix à 0 et
  prend ``reserved`` pour le stock ;
* ``CalculateFactorial`` n'a fait qu'ajouter des champs sous de nouveaux
  numéros : elle reste compatible.

Aucun code généré ne sait enregistrer des messages v2 sous un nom de service
v1 : on déclare donc les gestionnaires à la main, avec
``grpc.method_handlers_generic_handler``.
"""
from __future__ import annotations

import grpc

from common.config import HOST
from common.errors import InvalidArgument
from common.inventory import InventoryService
from common.telemetry import BUS, EventBus

from . import converters as conv
from .generated import service_v2_pb2 as pb_v2
from .grpc_server import GrpcServerHandle, unary_rpc

V1_SERVICE_NAME = "rpcexplorer.v1.InventoryService"


class InventoryServicerV2:
    """Implémentation du contrat v2 ; ``strict`` est modifiable à chaud."""

    def __init__(self, service: InventoryService, *, strict: bool = False) -> None:
        self._service = service
        self.strict = strict

    @unary_rpc
    def CalculateFactorial(
        self, request: pb_v2.FactorialRequest, context: grpc.ServicerContext
    ) -> pb_v2.FactorialReply:
        return conv.factorial_reply_v2_to_proto(self._service.calculate_factorial(request.n))

    @unary_rpc
    def GetProduct(self, request: pb_v2.ProductRequest, context: grpc.ServicerContext) -> pb_v2.Product:
        return conv.product_v2_to_proto(self._service.get_product_details(request.product_id))

    @unary_rpc
    def UpdateStock(
        self, request: pb_v2.UpdateStockRequest, context: grpc.ServicerContext
    ) -> pb_v2.UpdateStockReply:
        if self.strict and not request.warehouse:
            raise InvalidArgument(
                "« warehouse » est obligatoire depuis la version 2 du contrat", param="warehouse"
            )
        # Venant d'un client v1, « request.delta » (n°4) est absent du message : il vaut 0.
        result = self._service.update_stock(request.product_id, request.delta, request.idempotency_key)
        return conv.update_stock_reply_v2_to_proto(result)

    @unary_rpc
    def ListProducts(
        self, request: pb_v2.ListProductsRequest, context: grpc.ServicerContext
    ) -> pb_v2.ProductList:
        return conv.product_list_v2_to_proto(self._service.list_products(request.limit, request.category))

    def register(self, server: grpc.Server) -> None:
        """Enregistre les quatre méthodes v2 sous le nom de service v1."""
        unary = grpc.unary_unary_rpc_method_handler
        handlers = {
            "CalculateFactorial": unary(
                self.CalculateFactorial,
                request_deserializer=pb_v2.FactorialRequest.FromString,
                response_serializer=pb_v2.FactorialReply.SerializeToString,
            ),
            "GetProduct": unary(
                self.GetProduct,
                request_deserializer=pb_v2.ProductRequest.FromString,
                response_serializer=pb_v2.Product.SerializeToString,
            ),
            "UpdateStock": unary(
                self.UpdateStock,
                request_deserializer=pb_v2.UpdateStockRequest.FromString,
                response_serializer=pb_v2.UpdateStockReply.SerializeToString,
            ),
            "ListProducts": unary(
                self.ListProducts,
                request_deserializer=pb_v2.ListProductsRequest.FromString,
                response_serializer=pb_v2.ProductList.SerializeToString,
            ),
        }
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(V1_SERVICE_NAME, handlers),))


class GrpcServerV2Handle(GrpcServerHandle):
    """Poignée du serveur v2 : expose en plus l'interrupteur ``strict``."""

    def __init__(self, servicer: InventoryServicerV2, host: str, port: int, *, max_workers: int, bus: EventBus) -> None:
        super().__init__(servicer.register, host, port, max_workers=max_workers, bus=bus, name="grpc-v2")
        self._servicer = servicer

    @property
    def strict(self) -> bool:
        """``True`` : une requête sans ``warehouse`` est rejetée au lieu d'être mal interprétée."""
        return self._servicer.strict

    @strict.setter
    def strict(self, value: bool) -> None:
        self._servicer.strict = bool(value)


def create_grpc_server_v2(
    service: InventoryService,
    host: str = HOST,
    port: int = 0,
    *,
    strict: bool = False,
    max_workers: int = 8,
    bus: EventBus = BUS,
) -> GrpcServerV2Handle:
    """Prépare le serveur du contrat v2 (``port=0`` : port attribué par le système au démarrage)."""
    servicer = InventoryServicerV2(service, strict=strict)
    return GrpcServerV2Handle(servicer, host, port, max_workers=max_workers, bus=bus)
