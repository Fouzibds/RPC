"""Serveur gRPC du contrat v1 : le squelette généré par protoc, branché sur le service métier.

    python -m rpc_grpc.grpc_server [--host 127.0.0.1] [--port 50051]

Là où le RPC maison (``rpc_custom``) écrit lui-même son dispatcher, gRPC le
génère à partir du contrat : ``InventoryServicer`` n'a plus qu'à remplir les
méthodes annoncées par le ``.proto``. Chaque méthode fait trois choses, et
seulement trois : message → arguments Python, appel de la procédure métier,
dictionnaire → message.

Les quatre formes d'appel de gRPC sont représentées : unaire, flux serveur
(``StreamAnalytics``), flux client (``BulkUpdateStock``) et flux bidirectionnel
(``CheckStock``).
"""
from __future__ import annotations

import argparse
import functools
import logging
import sys
import threading
import time
from concurrent import futures
from typing import Any, Callable, Iterable, Iterator

import grpc

from common.config import APP_NAME, HOST, MAX_FRAME_BYTES, default_ports
from common.errors import GRPC_STATUS, DomainError, ProductNotFound
from common.inventory import InventoryService
from common.telemetry import BUS, EventBus

from . import converters as conv
from .generated import service_pb2 as pb
from .generated import service_pb2_grpc as pb_grpc
from .interceptors import TracingServerInterceptor

INTERNAL_MESSAGE = "Erreur interne du serveur"

# so_reuseport=0 : deux serveurs ne doivent jamais se partager un port en silence.
# Tailles maximales alignées sur celles du RPC maison, pour comparer à armes égales.
SERVER_OPTIONS: tuple[tuple[str, int], ...] = (
    ("grpc.so_reuseport", 0),
    ("grpc.max_send_message_length", MAX_FRAME_BYTES),
    ("grpc.max_receive_message_length", MAX_FRAME_BYTES),
)

# Message et détail d'une erreur voyagent dans les trailers HTTP/2, que gRPC plafonne à 8 Kio.
# Au-delà, le client ne reçoit plus le statut du serveur mais UNAVAILABLE : un message qui
# recopie une entrée démesurée (référence produit de plusieurs Kio) doit donc être borné.
_MAX_MESSAGE_BYTES = 1024
_MAX_DETAIL_BYTES = 2048

_LOGGER = logging.getLogger(__name__)


# --- Traduction des erreurs --------------------------------------------------

def _clipped(message: str) -> str:
    """Message d'erreur ramené à ``_MAX_MESSAGE_BYTES`` octets UTF-8, sans couper un caractère."""
    data = message.encode("utf-8")
    if len(data) <= _MAX_MESSAGE_BYTES:
        return message
    return data[:_MAX_MESSAGE_BYTES].decode("utf-8", errors="ignore") + "…"


def _abort(context: grpc.ServicerContext, error: Exception) -> None:
    """Termine l'appel par un statut gRPC ; ``context.abort`` lève toujours une exception.

    Une erreur métier devient le statut correspondant, avec son détail structuré
    en métadonnée de fin d'appel. Toute autre exception devient ``INTERNAL`` avec
    un message générique : la pile d'appels ne quitte jamais le serveur.

    Exception : ``grpc.RpcError``, que gRPC lève lui-même pendant la lecture d'un
    flux de requêtes quand le client a annulé l'appel ou que son échéance est
    passée. Ce n'est pas une panne du serveur : elle remonte telle quelle, gRPC
    la reconnaît et clôt l'appel sans rien journaliser.
    """
    if isinstance(error, grpc.RpcError):
        raise error
    if isinstance(error, DomainError):
        detail = conv.error_detail_to_metadata(error.data)
        if sum(len(value) for _, value in detail) <= _MAX_DETAIL_BYTES:
            context.set_trailing_metadata(detail)   # sinon omis : le statut et son message suffisent
        context.abort(grpc.StatusCode[GRPC_STATUS[error.code]], _clipped(error.message))
    _LOGGER.error("Erreur inattendue dans une procédure gRPC", exc_info=error)
    context.abort(grpc.StatusCode.INTERNAL, INTERNAL_MESSAGE)


def unary_rpc(behavior: Callable) -> Callable:
    """Décorateur d'une méthode à réponse unique : exceptions → statut gRPC."""

    @functools.wraps(behavior)
    def guarded(self: Any, request: Any, context: grpc.ServicerContext) -> Any:
        try:
            return behavior(self, request, context)
        except Exception as error:
            _abort(context, error)

    return guarded


def streaming_rpc(behavior: Callable) -> Callable:
    """Décorateur d'une méthode à flux de réponses : l'erreur peut survenir en plein flux."""

    @functools.wraps(behavior)
    def guarded(self: Any, request: Any, context: grpc.ServicerContext) -> Iterator[Any]:
        try:
            yield from behavior(self, request, context)
        except Exception as error:
            _abort(context, error)

    return guarded


# --- Squelette ---------------------------------------------------------------

class InventoryServicer(pb_grpc.InventoryServiceServicer):
    """Implémentation du service déclaré dans ``service.proto``."""

    def __init__(self, service: InventoryService) -> None:
        self._service = service

    @unary_rpc
    def CalculateFactorial(self, request: pb.FactorialRequest, context: grpc.ServicerContext) -> pb.FactorialReply:
        return conv.factorial_reply_to_proto(self._service.calculate_factorial(request.n))

    @unary_rpc
    def GetProductDetails(self, request: pb.ProductRequest, context: grpc.ServicerContext) -> pb.Product:
        return conv.product_to_proto(self._service.get_product_details(request.product_id))

    @unary_rpc
    def UpdateStock(self, request: pb.UpdateStockRequest, context: grpc.ServicerContext) -> pb.UpdateStockReply:
        result = self._service.update_stock(request.product_id, request.delta, request.idempotency_key)
        return conv.update_stock_reply_to_proto(result)

    @unary_rpc
    def ListProducts(self, request: pb.ListProductsRequest, context: grpc.ServicerContext) -> pb.ProductList:
        return conv.product_list_to_proto(self._service.list_products(request.limit, request.category))

    @streaming_rpc
    def StreamAnalytics(
        self, request: pb.AnalyticsRequest, context: grpc.ServicerContext
    ) -> Iterator[pb.AnalyticsSnapshot]:
        """Flux serveur : une requête, puis un message par instantané."""
        for snapshot in self._service.stream_analytics(request.samples, request.interval_ms):
            if not context.is_active():
                return   # le client est parti (annulation, échéance) : inutile de continuer à produire
            yield conv.analytics_snapshot_to_proto(snapshot)

    @unary_rpc
    def BulkUpdateStock(
        self, request_iterator: Iterable[pb.UpdateStockRequest], context: grpc.ServicerContext
    ) -> pb.BulkUpdateSummary:
        """Flux client : les mouvements sont appliqués au fil de leur arrivée, le bilan part à la fin."""
        updates = (conv.update_stock_request_from_proto(request) for request in request_iterator)
        return conv.bulk_update_summary_to_proto(self._service.bulk_update_stock(updates))

    @streaming_rpc
    def CheckStock(
        self, request_iterator: Iterable[pb.ProductRequest], context: grpc.ServicerContext
    ) -> Iterator[pb.StockLevel]:
        """Flux bidirectionnel : une réponse par référence reçue, sur le même flux HTTP/2.

        Une référence inconnue ne produit PAS d'erreur gRPC : un statut d'erreur
        met fin à l'appel entier, donc aux vérifications suivantes. Elle donne un
        ``StockLevel`` avec ``available=false`` et le flux continue.
        """
        for request in request_iterator:
            try:
                level = self._service.check_stock(request.product_id)
            except ProductNotFound:
                level = {"product_id": request.product_id, "stock": 0, "available": False, "warehouse": ""}
            yield conv.stock_level_to_proto(level)


# --- Cycle de vie ------------------------------------------------------------

class GrpcServerHandle:
    """Poignée d'un serveur gRPC : ``start()``, ``stop()`` idempotent, ``port`` réel.

    Le serveur gRPC est construit dans ``start()`` : la poignée peut donc être
    arrêtée puis relancée, sur le même port.
    """

    def __init__(
        self,
        register: Callable[[grpc.Server], None],
        host: str,
        port: int,
        *,
        max_workers: int,
        bus: EventBus,
        name: str = "grpc",
    ) -> None:
        self.host = host
        self.port = port
        self.name = name
        self._register = register
        self._max_workers = max_workers
        self._bus = bus
        self._server: grpc.Server | None = None
        self._executor: futures.ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> "GrpcServerHandle":
        """Démarre le serveur ; au retour il écoute réellement et ``port`` est le port effectif."""
        with self._lock:
            if self._server is not None:
                return self
            executor = futures.ThreadPoolExecutor(self._max_workers, thread_name_prefix=f"{self.name}-rpc")
            server = grpc.server(
                executor,
                interceptors=(TracingServerInterceptor(self._bus),),
                options=SERVER_OPTIONS,
            )
            self._register(server)
            try:
                bound = server.add_insecure_port(self.address)
            except RuntimeError:
                bound = 0
            if not bound:
                executor.shutdown(wait=False)
                raise OSError(
                    f"Impossible d’écouter sur {self.address} (serveur {self.name}) : le port est déjà utilisé."
                )
            server.start()
            self.port, self._server, self._executor = bound, server, executor
        return self

    def stop(self, grace: float = 0.2) -> None:
        """Arrête le serveur et libère le port ; les appels en cours ont ``grace`` secondes pour finir."""
        with self._lock:
            server, self._server = self._server, None
            executor, self._executor = self._executor, None
        if server is None or executor is None:
            return
        server.stop(grace).wait(grace + 2.0)
        executor.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "GrpcServerHandle":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def create_grpc_server(
    service: InventoryService,
    host: str = HOST,
    port: int = 0,
    *,
    max_workers: int = 16,
    bus: EventBus = BUS,
) -> GrpcServerHandle:
    """Prépare le serveur du contrat v1 (``port=0`` : port attribué par le système au démarrage)."""
    register = functools.partial(pb_grpc.add_InventoryServiceServicer_to_server, InventoryServicer(service))
    return GrpcServerHandle(register, host, port, max_workers=max_workers, bus=bus)


# --- Exécution autonome ------------------------------------------------------

def _banner(handle: GrpcServerHandle) -> Any:
    from rich.panel import Panel
    from rich.table import Table

    kinds = {
        (False, False): "unaire",
        (False, True): "flux serveur",
        (True, False): "flux client",
        (True, True): "flux bidirectionnel",
    }
    service = pb.DESCRIPTOR.services_by_name["InventoryService"]
    table = Table(box=None, padding=(0, 2), header_style="bold cyan")
    table.add_column("Procédure")
    table.add_column("Forme")
    table.add_column("Requête → réponse", style="dim")
    for method in service.methods:
        table.add_row(
            method.name,
            kinds[method.client_streaming, method.server_streaming],
            f"{method.input_type.name} → {method.output_type.name}",
        )
    table.add_section()
    table.add_row("[dim]Service[/]", f"[dim]{service.full_name}[/]", "[dim]HTTP/2 + Protobuf[/]")
    return Panel(
        table,
        title=f"[bold]{APP_NAME}[/] — serveur gRPC en écoute sur [bold green]{handle.address}[/]",
        subtitle="Ctrl+C pour arrêter",
        border_style="cyan",
        padding=(1, 2),
    )


def main(argv: list[str] | None = None) -> int:
    from rich.console import Console

    parser = argparse.ArgumentParser(
        prog="python -m rpc_grpc.grpc_server",
        description="Serveur gRPC du contrat v1 (service de calcul et de gestion d’inventaire).",
    )
    parser.add_argument("--host", default=HOST, help="adresse d’écoute")
    parser.add_argument(
        "--port", type=int, default=default_ports().grpc, help="port d’écoute (0 = attribué par le système)"
    )
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):   # console Windows : accents et cadres en UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    console = Console()
    handle = create_grpc_server(InventoryService(), args.host, args.port)
    try:
        handle.start()
    except OSError as error:
        console.print(f"[bold red]Démarrage impossible[/] — {error}")
        return 1
    console.print(_banner(handle))
    try:
        while True:   # une attente bloquante sans délai n'est pas interruptible par Ctrl+C sous Windows
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("[dim]Arrêt demandé…[/]")
    finally:
        handle.stop()
    console.print("[green]Serveur gRPC arrêté.[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
