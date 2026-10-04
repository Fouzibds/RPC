"""Catalogue des procédures exposées : la même description sert au CLI, au
dashboard (formulaires générés) et à la documentation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config import MAX_FACTORIAL_N


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str                     # "int" | "str" | "product_id" | "category" | "updates" | "product_ids"
    default: Any
    description: str
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class MethodSpec:
    name: str                     # nom Python / JSON-RPC
    title: str
    description: str
    kind: str                     # unary | server_stream | client_stream | bidi_stream
    params: tuple[ParamSpec, ...]
    idempotent: bool
    grpc_method: str              # nom dans le contrat IDL
    rest_route: str               # équivalent REST
    protocols: tuple[str, ...] = ("local", "custom", "grpc", "rest")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


METHODS: tuple[MethodSpec, ...] = (
    MethodSpec(
        name="calculate_factorial",
        title="Calculer une factorielle",
        description="Calcul pur, sans état : n! renvoyé sous forme de chaîne (grand entier).",
        kind="unary",
        params=(ParamSpec("n", "int", 20, "Entier dont on veut la factorielle", 0, MAX_FACTORIAL_N),),
        idempotent=True,
        grpc_method="CalculateFactorial",
        rest_route="GET /api/factorial/{n}",
    ),
    MethodSpec(
        name="get_product_details",
        title="Consulter une fiche produit",
        description="Lecture seule : renvoie la fiche complète d’un produit (structure imbriquée).",
        kind="unary",
        params=(ParamSpec("product_id", "product_id", "SKU-1001", "Référence du produit"),),
        idempotent=True,
        grpc_method="GetProductDetails",
        rest_route="GET /api/products/{product_id}",
    ),
    MethodSpec(
        name="update_stock",
        title="Mettre à jour un stock",
        description="Écriture NON idempotente : ajoute « delta » au stock. Rejouer l’appel applique l’effet deux fois.",
        kind="unary",
        params=(
            ParamSpec("product_id", "product_id", "SKU-1001", "Référence du produit"),
            ParamSpec("delta", "int", -1, "Variation de stock (négative = sortie)", -1_000_000, 1_000_000),
            ParamSpec("idempotency_key", "str", "", "Clé d’idempotence (facultative) : rend le rejeu sans effet"),
        ),
        idempotent=False,
        grpc_method="UpdateStock",
        rest_route="POST /api/stock/{product_id}",
    ),
    MethodSpec(
        name="list_products",
        title="Lister le catalogue",
        description="Renvoie « limit » produits : idéal pour observer l’effet de la taille du payload.",
        kind="unary",
        params=(
            ParamSpec("limit", "int", 20, "Nombre de produits à renvoyer", 0, 5000),
            ParamSpec("category", "category", "", "Filtre par catégorie (vide = toutes)"),
        ),
        idempotent=True,
        grpc_method="ListProducts",
        rest_route="GET /api/products?limit={limit}&category={category}",
    ),
    MethodSpec(
        name="stream_analytics",
        title="Flux d’indicateurs (streaming serveur)",
        description="Le serveur pousse un instantané d’inventaire à intervalle régulier sur une seule requête.",
        kind="server_stream",
        params=(
            ParamSpec("samples", "int", 10, "Nombre d’instantanés à recevoir", 1, 500),
            ParamSpec("interval_ms", "int", 200, "Intervalle entre deux instantanés (ms)", 0, 5000),
        ),
        idempotent=True,
        grpc_method="StreamAnalytics",
        rest_route="GET /api/analytics/stream?samples={samples}&interval_ms={interval_ms}",
    ),
    MethodSpec(
        name="bulk_update_stock",
        title="Mouvements en lot (streaming client)",
        description="Le client envoie un flux de mouvements de stock ; le serveur répond par un bilan unique.",
        kind="client_stream",
        params=(
            ParamSpec(
                "updates",
                "updates",
                [
                    {"product_id": "SKU-1001", "delta": -2},
                    {"product_id": "SKU-1005", "delta": 10},
                    {"product_id": "SKU-1017", "delta": -1},
                ],
                "Liste de mouvements {product_id, delta}",
            ),
        ),
        idempotent=False,
        grpc_method="BulkUpdateStock",
        rest_route="—",
        protocols=("local", "grpc"),
    ),
    MethodSpec(
        name="check_stock",
        title="Vérification en direct (streaming bidirectionnel)",
        description="Le client envoie des références au fil de l’eau ; le serveur répond pour chacune, sur le même flux.",
        kind="bidi_stream",
        params=(
            ParamSpec(
                "product_ids",
                "product_ids",
                ["SKU-1001", "SKU-1012", "SKU-1022"],
                "Références à vérifier",
            ),
        ),
        idempotent=True,
        grpc_method="CheckStock",
        rest_route="—",
        protocols=("local", "grpc"),
    ),
)

_BY_NAME = {spec.name: spec for spec in METHODS}

# Les cinq procédures communes aux quatre « protocoles » (interface InventoryClient).
CORE_METHODS: tuple[str, ...] = (
    "calculate_factorial",
    "get_product_details",
    "update_stock",
    "list_products",
    "stream_analytics",
)


def method_spec(name: str) -> MethodSpec | None:
    return _BY_NAME.get(name)


def catalog_dict() -> list[dict[str, Any]]:
    return [spec.to_dict() for spec in METHODS]
