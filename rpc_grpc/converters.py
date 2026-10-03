"""Conversions explicites entre dictionnaires métier et messages Protobuf.

Le service métier (``common.inventory``) manipule des dictionnaires ; gRPC
transporte des messages typés. Ce module est la frontière entre les deux, et
elle est écrite à la main, champ par champ :

* ``json_format.MessageToDict`` est volontairement écarté : il suit la
  convention JSON de Protobuf, qui transforme les ``int64`` en chaînes et
  omet les champs à leur valeur par défaut. Les dictionnaires produits ici
  ont au contraire exactement la forme et les types de ``InventoryService``
  (les entiers restent des entiers, ``tags`` est une liste…) ;
* ces fonctions rendent visible le prix du typage fort : chaque champ du
  contrat doit être relié explicitement à une clé du modèle métier.

Convention de nommage : ``<message>_to_proto(dict) -> message`` et
``<message>_from_proto(message) -> dict``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from google.protobuf.message import Message

from .generated import service_pb2 as pb
from .generated import service_v2_pb2 as pb_v2

# Métadonnée de fin d'appel (« trailer ») portant le détail structuré d'une erreur
# métier. Le suffixe « -bin » autorise des octets quelconques, donc du JSON UTF-8.
ERROR_DETAIL_KEY = "x-error-detail-bin"

ToProto = Callable[[dict[str, Any]], Message]
FromProto = Callable[[Any], dict[str, Any]]


# --- CalculateFactorial ------------------------------------------------------

def factorial_request_to_proto(data: dict[str, Any]) -> pb.FactorialRequest:
    return pb.FactorialRequest(n=data["n"])


def factorial_request_from_proto(message: pb.FactorialRequest) -> dict[str, Any]:
    return {"n": message.n}


def factorial_reply_to_proto(data: dict[str, Any]) -> pb.FactorialReply:
    return pb.FactorialReply(
        n=data["n"],
        result=data["result"],
        digits=data["digits"],
        compute_us=data["compute_us"],
    )


def factorial_reply_from_proto(message: pb.FactorialReply) -> dict[str, Any]:
    return {
        "n": message.n,
        "result": message.result,
        "digits": message.digits,
        "compute_us": message.compute_us,
    }


# --- GetProductDetails -------------------------------------------------------

def product_request_to_proto(data: dict[str, Any]) -> pb.ProductRequest:
    return pb.ProductRequest(product_id=data["product_id"])


def product_request_from_proto(message: pb.ProductRequest) -> dict[str, Any]:
    return {"product_id": message.product_id}


def dimensions_to_proto(data: dict[str, Any]) -> pb.Dimensions:
    return pb.Dimensions(
        width_cm=data["width_cm"],
        height_cm=data["height_cm"],
        depth_cm=data["depth_cm"],
        weight_kg=data["weight_kg"],
    )


def dimensions_from_proto(message: pb.Dimensions) -> dict[str, Any]:
    return {
        "width_cm": message.width_cm,
        "height_cm": message.height_cm,
        "depth_cm": message.depth_cm,
        "weight_kg": message.weight_kg,
    }


def supplier_to_proto(data: dict[str, Any]) -> pb.Supplier:
    return pb.Supplier(
        name=data["name"],
        country=data["country"],
        lead_time_days=data["lead_time_days"],
    )


def supplier_from_proto(message: pb.Supplier) -> dict[str, Any]:
    return {
        "name": message.name,
        "country": message.country,
        "lead_time_days": message.lead_time_days,
    }


def product_to_proto(data: dict[str, Any]) -> pb.Product:
    return pb.Product(
        id=data["id"],
        name=data["name"],
        category=data["category"],
        price=data["price"],
        stock=data["stock"],
        warehouse=data["warehouse"],
        tags=data["tags"],
        dimensions=dimensions_to_proto(data["dimensions"]),
        supplier=supplier_to_proto(data["supplier"]),
        rating=data["rating"],
        active=data["active"],
        updated_at_ms=data["updated_at_ms"],
        version=data["version"],
        description=data["description"],
    )


def product_from_proto(message: pb.Product) -> dict[str, Any]:
    return {
        "id": message.id,
        "name": message.name,
        "category": message.category,
        "price": message.price,
        "stock": message.stock,
        "warehouse": message.warehouse,
        "tags": list(message.tags),
        "dimensions": dimensions_from_proto(message.dimensions),
        "supplier": supplier_from_proto(message.supplier),
        "rating": message.rating,
        "active": message.active,
        "updated_at_ms": message.updated_at_ms,
        "version": message.version,
        "description": message.description,
    }


# --- UpdateStock / BulkUpdateStock -------------------------------------------

def update_stock_request_to_proto(data: dict[str, Any]) -> pb.UpdateStockRequest:
    """Tolère les clés absentes, comme ``InventoryService.bulk_update_stock``.

    Un mouvement incomplet part donc tel quel : c'est le serveur qui le rejette
    et le compte dans le bilan, exactement comme lors d'un appel local.
    """
    return pb.UpdateStockRequest(
        product_id=data.get("product_id", ""),
        delta=data.get("delta", 0),
        idempotency_key=data.get("idempotency_key", ""),
    )


def update_stock_request_from_proto(message: pb.UpdateStockRequest) -> dict[str, Any]:
    return {
        "product_id": message.product_id,
        "delta": message.delta,
        "idempotency_key": message.idempotency_key,
    }


def update_stock_reply_to_proto(data: dict[str, Any]) -> pb.UpdateStockReply:
    return pb.UpdateStockReply(
        product_id=data["product_id"],
        previous_stock=data["previous_stock"],
        new_stock=data["new_stock"],
        delta=data["delta"],
        version=data["version"],
        applied=data["applied"],
        updated_at_ms=data["updated_at_ms"],
    )


def update_stock_reply_from_proto(message: pb.UpdateStockReply) -> dict[str, Any]:
    return {
        "product_id": message.product_id,
        "previous_stock": message.previous_stock,
        "new_stock": message.new_stock,
        "delta": message.delta,
        "version": message.version,
        "applied": message.applied,
        "updated_at_ms": message.updated_at_ms,
    }


def bulk_update_summary_to_proto(data: dict[str, Any]) -> pb.BulkUpdateSummary:
    return pb.BulkUpdateSummary(
        applied=data["applied"],
        rejected=data["rejected"],
        total_delta=data["total_delta"],
        errors=data["errors"],
    )


def bulk_update_summary_from_proto(message: pb.BulkUpdateSummary) -> dict[str, Any]:
    return {
        "applied": message.applied,
        "rejected": message.rejected,
        "total_delta": message.total_delta,
        "errors": list(message.errors),
    }


# --- ListProducts ------------------------------------------------------------

def list_products_request_to_proto(data: dict[str, Any]) -> pb.ListProductsRequest:
    return pb.ListProductsRequest(limit=data.get("limit", 20), category=data.get("category", ""))


def list_products_request_from_proto(message: pb.ListProductsRequest) -> dict[str, Any]:
    return {"limit": message.limit, "category": message.category}


def product_list_to_proto(data: dict[str, Any]) -> pb.ProductList:
    return pb.ProductList(
        products=[product_to_proto(product) for product in data["products"]],
        total=data["total"],
    )


def product_list_from_proto(message: pb.ProductList) -> dict[str, Any]:
    return {
        "products": [product_from_proto(product) for product in message.products],
        "total": message.total,
    }


# --- StreamAnalytics ---------------------------------------------------------

def analytics_request_to_proto(data: dict[str, Any]) -> pb.AnalyticsRequest:
    return pb.AnalyticsRequest(samples=data.get("samples", 10), interval_ms=data.get("interval_ms", 200))


def analytics_request_from_proto(message: pb.AnalyticsRequest) -> dict[str, Any]:
    return {"samples": message.samples, "interval_ms": message.interval_ms}


def analytics_snapshot_to_proto(data: dict[str, Any]) -> pb.AnalyticsSnapshot:
    return pb.AnalyticsSnapshot(
        seq=data["seq"],
        timestamp_ms=data["timestamp_ms"],
        total_units=data["total_units"],
        inventory_value=data["inventory_value"],
        low_stock_count=data["low_stock_count"],
        orders_per_min=data["orders_per_min"],
        hot_product_id=data["hot_product_id"],
        operations=data["operations"],
    )


def analytics_snapshot_from_proto(message: pb.AnalyticsSnapshot) -> dict[str, Any]:
    return {
        "seq": message.seq,
        "timestamp_ms": message.timestamp_ms,
        "total_units": message.total_units,
        "inventory_value": message.inventory_value,
        "low_stock_count": message.low_stock_count,
        "orders_per_min": message.orders_per_min,
        "hot_product_id": message.hot_product_id,
        "operations": message.operations,
    }


# --- CheckStock --------------------------------------------------------------

def stock_level_to_proto(data: dict[str, Any]) -> pb.StockLevel:
    return pb.StockLevel(
        product_id=data["product_id"],
        stock=data["stock"],
        available=data["available"],
        warehouse=data["warehouse"],
    )


def stock_level_from_proto(message: pb.StockLevel) -> dict[str, Any]:
    return {
        "product_id": message.product_id,
        "stock": message.stock,
        "available": message.available,
        "warehouse": message.warehouse,
    }


# --- Contrat v2 (serveur « mal évolué ») -------------------------------------
# Seules les réponses qui diffèrent de la v1 ont leur propre conversion : les
# autres messages v2 ont la même forme et se lisent directement champ par champ.

V2_FACTORIAL_ALGORITHM = "math.factorial"


def factorial_reply_v2_to_proto(data: dict[str, Any]) -> pb_v2.FactorialReply:
    """Ajoute ``algorithm`` (n°5) : un ancien client ignore ce champ inconnu."""
    return pb_v2.FactorialReply(
        n=data["n"],
        result=data["result"],
        digits=data["digits"],
        compute_us=data["compute_us"],
        algorithm=V2_FACTORIAL_ALGORITHM,
    )


def product_v2_to_proto(data: dict[str, Any]) -> pb_v2.Product:
    """Fiche produit v2 : prix en centimes au n°4, « reserved » au n°5, stock au n°15.

    Un client v1 qui décode ces octets attend un ``double`` au n°4 (il trouve un
    varint : champ ignoré, prix à 0) et lit ``reserved`` en croyant lire le stock.
    """
    return pb_v2.Product(
        id=data["id"],
        name=data["name"],
        category=data["category"],
        price_cents=round(data["price"] * 100),
        reserved=data["stock"] // 10,
        warehouse=data["warehouse"],
        tags=data["tags"],
        dimensions=pb_v2.Dimensions(**data["dimensions"]),   # sous-messages inchangés depuis la v1
        supplier=pb_v2.Supplier(**data["supplier"]),
        rating=data["rating"],
        active=data["active"],
        updated_at_ms=data["updated_at_ms"],
        version=data["version"],
        description=data["description"],
        stock=data["stock"],
    )


def product_list_v2_to_proto(data: dict[str, Any]) -> pb_v2.ProductList:
    return pb_v2.ProductList(
        products=[product_v2_to_proto(product) for product in data["products"]],
        total=data["total"],
    )


def update_stock_reply_v2_to_proto(data: dict[str, Any]) -> pb_v2.UpdateStockReply:
    return pb_v2.UpdateStockReply(
        product_id=data["product_id"],
        previous_stock=data["previous_stock"],
        new_stock=data["new_stock"],
        delta=data["delta"],
        version=data["version"],
        applied=data["applied"],
        updated_at_ms=data["updated_at_ms"],
    )


# --- Détail structuré des erreurs métier -------------------------------------

def error_detail_to_metadata(data: dict[str, Any]) -> tuple[tuple[str, bytes], ...]:
    """Encode ``DomainError.data`` en métadonnée de fin d'appel (vide s'il n'y a rien à dire)."""
    if not data:
        return ()
    return ((ERROR_DETAIL_KEY, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")),)


def error_detail_from_metadata(metadata: Iterable[tuple[str, Any]] | None) -> dict[str, Any]:
    """Opération inverse, côté client ; tolère une métadonnée absente ou illisible."""
    for key, value in metadata or ():
        if key == ERROR_DETAIL_KEY:
            try:
                detail = json.loads(value)
            except (TypeError, ValueError):
                return {}
            return detail if isinstance(detail, dict) else {}
    return {}


# --- Registre par procédure --------------------------------------------------

@dataclass(frozen=True)
class MethodCodec:
    """Les quatre conversions d'une procédure du contrat v1."""

    rpc: str                       # nom dans le contrat IDL, ex. « UpdateStock »
    request_to_proto: ToProto
    request_from_proto: FromProto
    reply_to_proto: ToProto
    reply_from_proto: FromProto


# Indexé par le nom Python de la procédure. Pour les flux, la conversion porte
# sur UN élément (un mouvement, un instantané, un niveau de stock).
CODECS: dict[str, MethodCodec] = {
    "calculate_factorial": MethodCodec(
        "CalculateFactorial",
        factorial_request_to_proto, factorial_request_from_proto,
        factorial_reply_to_proto, factorial_reply_from_proto,
    ),
    "get_product_details": MethodCodec(
        "GetProductDetails",
        product_request_to_proto, product_request_from_proto,
        product_to_proto, product_from_proto,
    ),
    "update_stock": MethodCodec(
        "UpdateStock",
        update_stock_request_to_proto, update_stock_request_from_proto,
        update_stock_reply_to_proto, update_stock_reply_from_proto,
    ),
    "list_products": MethodCodec(
        "ListProducts",
        list_products_request_to_proto, list_products_request_from_proto,
        product_list_to_proto, product_list_from_proto,
    ),
    "stream_analytics": MethodCodec(
        "StreamAnalytics",
        analytics_request_to_proto, analytics_request_from_proto,
        analytics_snapshot_to_proto, analytics_snapshot_from_proto,
    ),
    "bulk_update_stock": MethodCodec(
        "BulkUpdateStock",
        update_stock_request_to_proto, update_stock_request_from_proto,
        bulk_update_summary_to_proto, bulk_update_summary_from_proto,
    ),
    "check_stock": MethodCodec(
        "CheckStock",
        product_request_to_proto, product_request_from_proto,
        stock_level_to_proto, stock_level_from_proto,
    ),
}
