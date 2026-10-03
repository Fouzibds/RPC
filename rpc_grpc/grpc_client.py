"""Client gRPC : le stub généré par protoc, derrière l'interface commune ``InventoryClient``.

    python -m rpc_grpc.grpc_client [--host 127.0.0.1] [--port 50051]

Tout le travail de transport est fait par le code que protoc a produit à partir
du contrat : ``InventoryServiceStub`` expose une méthode par ``rpc`` du
``.proto``. Cette classe n'ajoute que ce que le contrat ne dit pas :

* la conversion messages ⇄ dictionnaires (``converters``), pour que le code
  appelant soit le même qu'avec un objet local, le stub maison ou l'API REST ;
* la traduction des statuts gRPC en erreurs canoniques (``common.errors``) ;
* l'échéance (*deadline*) de chaque appel, qui voyage jusqu'au serveur ;
* la corrélation des traces, par la métadonnée ``x-call-id``.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Callable, Iterable, Iterator

import grpc
from google.protobuf.message import Message

from common.client_api import InventoryClient
from common.config import (
    APP_NAME,
    CONNECT_TIMEOUT_S,
    DEFAULT_TIMEOUT_S,
    HOST,
    MAX_FRAME_BYTES,
    default_ports,
)
from common.errors import (
    GRPC_TO_CANONICAL,
    INTERNAL,
    InvalidArgumentError,
    RpcError,
    RpcTransportError,
    error_from_code,
)
from common.telemetry import BUS, EventBus, TraceEvent, hexdump, new_call_id

from . import converters as conv
from .generated import service_pb2 as pb
from .generated import service_pb2_grpc as pb_grpc
from .interceptors import CALL_ID_KEY, PROTOCOL, TracingChannel, rpc_status


def channel_options(connect_timeout: float = CONNECT_TIMEOUT_S) -> list[tuple[str, int]]:
    """Options du canal : reconnexion rapide, aucun retry caché, mêmes limites que le RPC maison."""
    return [
        # Après une coupure, gRPC retente entre 100 ms et 1 s (20 s et 2 min par défaut).
        ("grpc.initial_reconnect_backoff_ms", 100),
        ("grpc.max_reconnect_backoff_ms", 1000),
        # Malgré son nom, cette option est le délai accordé à UNE tentative de connexion
        # (TCP + échange des SETTINGS HTTP/2). Trop court, un lien simplement lent
        # serait déclaré injoignable.
        ("grpc.min_reconnect_backoff_ms", int(connect_timeout * 1000)),
        # Les retries sont gérés — et montrés — par netsim/resilience.py, pas par le canal.
        ("grpc.enable_retries", 0),
        ("grpc.max_send_message_length", MAX_FRAME_BYTES),
        ("grpc.max_receive_message_length", MAX_FRAME_BYTES),
    ]


def to_rpc_error(error: grpc.RpcError, method: str) -> RpcError:
    """``grpc.RpcError`` → erreur canonique ; le message du serveur est conservé tel quel."""
    status, message = rpc_status(error)
    trailers = error.trailing_metadata() if callable(getattr(error, "trailing_metadata", None)) else None
    return error_from_code(
        GRPC_TO_CANONICAL.get(status, INTERNAL),
        message,
        protocol=PROTOCOL,
        method=method,
        detail={"grpc_status": status, **conv.error_detail_from_metadata(trailers)},
    )


def _plain(value: Any) -> Any:
    """Valeur affichable dans une trace, même quand c'est justement elle qui est invalide."""
    return value if isinstance(value, (str, int, float, bool, type(None))) else repr(value)


class GrpcInventoryClient(InventoryClient):
    """``InventoryClient`` au-dessus de gRPC.

    Attributs publics : ``channel`` (canal HTTP/2), ``grpc_stub`` (stub généré,
    chemin nominal) et ``traced_stub`` (le même stub généré, branché sur un canal
    qui publie les étapes de l'appel quand le bus de traces est actif).
    """

    protocol = PROTOCOL

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        bus: EventBus = BUS,
    ) -> None:
        self.target = f"{host}:{port}"
        # La connexion est paresseuse : elle s'établit au premier appel, puis reste ouverte.
        self.channel = grpc.insecure_channel(self.target, options=channel_options(connect_timeout))
        self.grpc_stub = pb_grpc.InventoryServiceStub(self.channel)
        self.traced_stub = pb_grpc.InventoryServiceStub(TracingChannel(self.channel, authority=self.target, bus=bus))
        self._timeout = timeout
        self._bus = bus

    # -- appels unaires -------------------------------------------------------

    def calculate_factorial(self, n: int, *, timeout: float | None = None) -> dict[str, Any]:
        method = "calculate_factorial"
        request = self._build(method, pb.FactorialRequest, n=n)
        return conv.factorial_reply_from_proto(self._unary(method, "CalculateFactorial", request, timeout))

    def get_product_details(self, product_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        method = "get_product_details"
        request = self._build(method, pb.ProductRequest, product_id=product_id)
        return conv.product_from_proto(self._unary(method, "GetProductDetails", request, timeout))

    def update_stock(
        self, product_id: str, delta: int, *, idempotency_key: str = "", timeout: float | None = None
    ) -> dict[str, Any]:
        method = "update_stock"
        request = self._build(
            method, pb.UpdateStockRequest, product_id=product_id, delta=delta, idempotency_key=idempotency_key
        )
        return conv.update_stock_reply_from_proto(self._unary(method, "UpdateStock", request, timeout))

    def list_products(self, limit: int = 20, category: str = "", *, timeout: float | None = None) -> dict[str, Any]:
        method = "list_products"
        request = self._build(method, pb.ListProductsRequest, limit=limit, category=category)
        return conv.product_list_from_proto(self._unary(method, "ListProducts", request, timeout))

    # -- flux -----------------------------------------------------------------

    def stream_analytics(
        self, samples: int = 10, interval_ms: int = 200, *, timeout: float | None = None
    ) -> Iterator[dict[str, Any]]:
        """Flux serveur : une requête, puis les instantanés au fil de leur production.

        Une échéance gRPC porte sur l'appel ENTIER, flux compris. On lui ajoute donc
        la durée annoncée du flux : ``timeout`` garde ainsi le sens qu'il a avec les
        autres protocoles (l'attente tolérée), et un flux de 6 s lancé avec un délai
        de 5 s n'échoue pas alors que tout se passe bien.
        """
        method = "stream_analytics"
        request = self._build(method, pb.AnalyticsRequest, samples=samples, interval_ms=interval_ms)
        deadline = (self._timeout if timeout is None else timeout) + samples * interval_ms / 1000
        stub, options = self._prepare(deadline)
        return self._responses(method, stub.StreamAnalytics(request, **options), conv.analytics_snapshot_from_proto)

    def bulk_update_stock(self, updates: Iterable[dict[str, Any]], *, timeout: float | None = None) -> dict[str, Any]:
        """Flux client : un message par mouvement, puis un bilan unique en réponse."""
        method = "bulk_update_stock"
        # Messages construits d'avance : une valeur hors contrat est signalée ici,
        # clairement, et non au milieu du flux par le fil d'émission de gRPC.
        requests = [self._build(method, conv.update_stock_request_to_proto, update) for update in updates]
        stub, options = self._prepare(timeout)
        try:
            summary = stub.BulkUpdateStock(iter(requests), **options)
        except grpc.RpcError as error:
            raise to_rpc_error(error, method) from error
        return conv.bulk_update_summary_from_proto(summary)

    def check_stock(self, product_ids: Iterable[str], *, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        """Flux bidirectionnel : une référence envoyée, un niveau de stock reçu, sur le même flux."""
        method = "check_stock"
        requests = [self._build(method, pb.ProductRequest, product_id=product_id) for product_id in product_ids]
        stub, options = self._prepare(timeout)
        return self._responses(method, stub.CheckStock(iter(requests), **options), conv.stock_level_from_proto)

    # -- cycle de vie ---------------------------------------------------------

    def close(self) -> None:
        super().close()
        self.channel.close()

    # -- interne --------------------------------------------------------------

    def _build(self, method: str, factory: Callable[..., Message], *args: Any, **fields: Any) -> Message:
        """Construit un message de requête : le typage du contrat s'applique AVANT tout envoi.

        Un argument du mauvais type ou hors bornes (``n=-1`` pour un ``uint32``) est
        refusé ici par le code généré, sans qu'un seul octet ne parte sur le réseau ;
        un argument absent (``None``) l'est par le stub lui-même.
        """
        try:
            for name, value in fields.items():
                if value is None:
                    # Pour Protobuf, None veut dire « champ non renseigné » : il prendrait en silence sa
                    # valeur par défaut (0, ""), là où les autres protocoles refusent l'argument.
                    raise ValueError(f"« {name} » est obligatoire (None reçu)")
            return factory(*args, **fields)
        except (TypeError, ValueError, AttributeError) as exc:
            error = InvalidArgumentError(
                f"Requête refusée par le contrat Protobuf avant tout envoi : {exc}",
                protocol=PROTOCOL,
                method=method,
                detail={"rejected_by": "client"},
            )
            bus = self._bus
            if bus.enabled:
                call_id = self.last_call_id = new_call_id(PROTOCOL)
                bus.emit(call_id=call_id, protocol=PROTOCOL, side="client", stage="client.call", method=method,
                         detail={"args": [_plain(arg) for arg in args],
                                 "kwargs": {name: _plain(value) for name, value in fields.items()}})
                bus.emit(call_id=call_id, protocol=PROTOCOL, side="client", stage="client.error", method=method,
                         duration_us=0.0, detail={"code": error.code, "message": error.message})
            raise error from exc

    def _prepare(self, timeout: float | None) -> tuple[pb_grpc.InventoryServiceStub, dict[str, Any]]:
        """Choisit le stub et les options de l'appel.

        Bus coupé : le stub généré ordinaire, sans rien autour. Bus actif : le stub
        tracé, et l'identifiant de corrélation part en métadonnée ``x-call-id``.
        ``wait_for_ready=False`` : serveur injoignable = échec immédiat (``UNAVAILABLE``).
        """
        options: dict[str, Any] = {
            "timeout": self._timeout if timeout is None else timeout,
            "wait_for_ready": False,
        }
        if not self._bus.enabled:
            return self.grpc_stub, options
        call_id = self.last_call_id = new_call_id(PROTOCOL)
        options["metadata"] = ((CALL_ID_KEY, call_id),)
        return self.traced_stub, options

    def _unary(self, method: str, rpc: str, request: Message, timeout: float | None) -> Any:
        stub, options = self._prepare(timeout)
        try:
            return getattr(stub, rpc)(request, **options)
        except grpc.RpcError as error:
            raise to_rpc_error(error, method) from error

    @staticmethod
    def _responses(method: str, call: Any, convert: Callable[[Any], dict[str, Any]]) -> Iterator[dict[str, Any]]:
        """Parcourt un flux de réponses ; une erreur peut survenir après plusieurs éléments."""
        try:
            for message in call:
                yield convert(message)
        except grpc.RpcError as error:
            raise to_rpc_error(error, method) from error
        finally:
            call.cancel()   # sans effet si le flux est terminé ; libère le flux HTTP/2 si l'appelant s'arrête avant


# --- Démonstration autonome --------------------------------------------------

def _fr(value: float, decimals: int = 0) -> str:
    """Nombre à la française : virgule décimale, espace insécable entre les milliers."""
    return f"{value:,.{decimals}f}".replace(",", "\u00a0").replace(".", ",")


def _timed(call: Callable[[], Any]) -> tuple[Any, str]:
    started = time.perf_counter()
    result = call()
    return result, f"{_fr((time.perf_counter() - started) * 1000, 2)} ms"


def _demo(console: Any, client: GrpcInventoryClient) -> None:
    from rich.panel import Panel
    from rich.table import Table

    def section(title: str) -> None:
        console.print()
        console.rule(f"[bold cyan]{title}[/]", align="left")

    console.print(Panel(
        f"Client : stub [bold]InventoryServiceStub[/] généré par protoc\nServeur : [bold green]{client.target}[/]",
        title=f"[bold]{APP_NAME}[/] — démonstration gRPC",
        border_style="cyan",
        padding=(1, 2),
    ))

    section("1 · Appels unaires — une requête, une réponse")
    factorial, elapsed = _timed(lambda: client.calculate_factorial(20))
    console.print(f"  calculate_factorial(20) → [bold]{factorial['result']}[/]  [dim]{elapsed}[/]")
    product, elapsed = _timed(lambda: client.get_product_details("SKU-1001"))
    console.print(
        f"  get_product_details('SKU-1001') → [bold]{product['name']}[/], {_fr(product['price'], 2)} €, "
        f"stock {product['stock']}, entrepôt {product['warehouse']}  [dim]{elapsed}[/]"
    )

    events: list[TraceEvent] = []
    unsubscribe = BUS.subscribe(events.append)
    try:
        update, elapsed = _timed(lambda: client.update_stock("SKU-1001", -3))
    finally:
        unsubscribe()
    console.print(
        f"  update_stock('SKU-1001', -3) → stock {update['previous_stock']} → [bold]{update['new_stock']}[/]"
        f"  [dim]{elapsed}[/]"
    )
    try:
        client.get_product_details("SKU-9999")
    except RpcError as error:
        console.print(
            f"  get_product_details('SKU-9999') → [red]{type(error).__name__}[/] "
            f"(statut gRPC {error.detail['grpc_status']}) : {error.message}"
        )

    sent = next((event for event in events if event.stage == "client.send"), None)
    if sent is not None and sent.payload is not None:
        section("Sous le capot — la requête update_stock telle qu'elle part sur le fil")
        console.print(f"[dim]{hexdump(sent.payload)}[/]")
        wire = Table(box=None, padding=(0, 2), header_style="bold cyan")
        wire.add_column("Octets")
        wire.add_column("Rôle")
        wire.add_column("Valeur", style="green")
        for segment in sent.detail["segments"]:
            wire.add_row(
                sent.payload[segment["start"]:segment["end"]].hex(" "),
                segment["label"],
                str(segment.get("value", "")),
            )
        console.print(wire)
        console.print(f"  [dim]{sent.size} octets : aucun nom de champ ne voyage, seulement des numéros.[/]")

    section("2 · Flux serveur — une requête, plusieurs réponses")
    for snapshot in client.stream_analytics(samples=4, interval_ms=150):
        console.print(
            f"  instantané {snapshot['seq']} : {_fr(snapshot['total_units'])} unités, "
            f"{_fr(snapshot['inventory_value'], 2)} € en stock, {_fr(snapshot['orders_per_min'], 1)} commandes/min"
        )

    section("3 · Flux client — plusieurs requêtes, une réponse")
    # Le lot compense le retrait précédent : la démonstration laisse l'inventaire dans son état initial.
    movements = [
        {"product_id": "SKU-1001", "delta": 3},
        {"product_id": "SKU-1005", "delta": 10},
        {"product_id": "SKU-1005", "delta": -10},
        {"product_id": "SKU-9999", "delta": 1},
    ]
    summary, elapsed = _timed(lambda: client.bulk_update_stock(movements))
    console.print(
        f"  bulk_update_stock({len(movements)} mouvements) → {summary['applied']} appliqués, "
        f"{summary['rejected']} rejeté(s), variation totale {summary['total_delta']:+d}  [dim]{elapsed}[/]"
    )
    for message in summary["errors"]:
        console.print(f"    [yellow]rejet[/] : {message}")

    section("4 · Flux bidirectionnel — requêtes et réponses entrelacées")
    for level in client.check_stock(["SKU-1001", "SKU-1012", "SKU-9999"]):
        state = "[green]disponible[/]" if level["available"] else "[red]indisponible[/]"
        console.print(f"  {level['product_id']} : {level['stock']} en stock, {state}")
    console.print()


def main(argv: list[str] | None = None) -> int:
    from rich.console import Console

    parser = argparse.ArgumentParser(
        prog="python -m rpc_grpc.grpc_client",
        description="Démonstration des quatre formes d'appel gRPC contre le serveur du contrat v1.",
    )
    parser.add_argument("--host", default=HOST, help="adresse du serveur")
    parser.add_argument("--port", type=int, default=default_ports().grpc, help="port du serveur")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):   # console Windows : accents et cadres en UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    console = Console()
    client = GrpcInventoryClient(args.host, args.port)
    try:
        # Le canal se connecte paresseusement : on vérifie d'abord qu'un serveur répond.
        grpc.channel_ready_future(client.channel).result(timeout=CONNECT_TIMEOUT_S + 0.5)
        _demo(console, client)
    except (grpc.FutureTimeoutError, RpcTransportError):
        console.print(
            f"[bold red]Serveur gRPC injoignable sur {client.target}.[/]\n"
            "Démarrez-le dans un autre terminal :  [bold]python -m rpc_grpc.grpc_server[/]"
        )
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
