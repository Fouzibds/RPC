"""Service métier : calcul & gestion d'inventaire.

Cette classe ne sait RIEN du réseau. C'est « la procédure distante » que les
trois middlewares (JSON-RPC maison, gRPC, REST) exposent à l'identique : les
dictionnaires renvoyés ici ont exactement les mêmes clés que les messages
Protobuf de ``rpc_grpc/protos/service.proto``.
"""
from __future__ import annotations

import math
import random
import threading
import time
from collections import Counter, deque
from copy import deepcopy
from typing import Any, Iterable, Iterator

from .config import LOW_STOCK_THRESHOLD, MAX_FACTORIAL_N
from .errors import InsufficientStock, InvalidArgument, ProductNotFound

_SUPPLIERS: tuple[dict[str, Any], ...] = (
    {"name": "Nordik Components", "country": "SE", "lead_time_days": 6},
    {"name": "Atlas Électronique", "country": "FR", "lead_time_days": 3},
    {"name": "Shenzhen Brightway", "country": "CN", "lead_time_days": 21},
    {"name": "Rhein Logistik", "country": "DE", "lead_time_days": 5},
    {"name": "Maghreb Tech Supply", "country": "DZ", "lead_time_days": 8},
)

# (id, nom, catégorie, prix, stock, entrepôt, tags, fournisseur, (l, h, p, kg), note)
_CATALOG: tuple[tuple, ...] = (
    ("SKU-1001", "Clavier mécanique Orion K7", "Périphériques", 129.90, 84, "PAR-01", ("clavier", "rgb", "usb-c"), 0, (44.0, 4.1, 13.5, 0.98), 4.7),
    ("SKU-1002", "Souris sans fil Vector M3", "Périphériques", 59.90, 140, "PAR-01", ("souris", "bluetooth"), 2, (12.4, 4.0, 6.8, 0.09), 4.5),
    ("SKU-1003", "Écran 27\" QHD Lumen 27Q", "Écrans", 349.00, 32, "LYS-02", ("écran", "qhd", "165hz"), 3, (61.4, 45.8, 21.0, 5.60), 4.6),
    ("SKU-1004", "Écran 34\" ultrawide Lumen 34W", "Écrans", 599.00, 11, "LYS-02", ("écran", "ultrawide", "hdr"), 3, (81.7, 46.2, 25.0, 8.10), 4.8),
    ("SKU-1005", "SSD NVMe 1 To Flux P5", "Stockage", 94.50, 210, "PAR-01", ("ssd", "nvme", "m.2"), 2, (8.0, 0.3, 2.2, 0.01), 4.9),
    ("SKU-1006", "SSD NVMe 2 To Flux P5", "Stockage", 169.00, 96, "PAR-01", ("ssd", "nvme", "m.2"), 2, (8.0, 0.3, 2.2, 0.01), 4.8),
    ("SKU-1007", "Disque dur 8 To Vault H8", "Stockage", 189.00, 45, "MRS-03", ("hdd", "nas"), 0, (14.7, 2.6, 10.2, 0.72), 4.3),
    ("SKU-1008", "NAS 4 baies Vault N4", "Stockage", 479.00, 9, "MRS-03", ("nas", "raid", "10gbe"), 0, (19.9, 16.6, 22.3, 2.30), 4.6),
    ("SKU-1009", "Routeur Wi-Fi 7 Halo AX", "Réseau", 279.00, 38, "LYS-02", ("wifi7", "mesh"), 1, (24.0, 5.5, 16.0, 0.82), 4.4),
    ("SKU-1010", "Switch 8 ports 2.5G Link S8", "Réseau", 119.00, 72, "LYS-02", ("switch", "2.5gbe"), 1, (21.0, 2.8, 10.4, 0.61), 4.5),
    ("SKU-1011", "Câble fibre OM4 10 m", "Réseau", 24.90, 320, "PAR-01", ("fibre", "lc-lc"), 2, (20.0, 3.0, 20.0, 0.12), 4.2),
    ("SKU-1012", "Carte réseau 10G SFP+ Link X1", "Réseau", 139.00, 6, "MRS-03", ("sfp+", "pcie"), 3, (12.1, 1.9, 6.9, 0.11), 4.1),
    ("SKU-1013", "Casque studio Aria H1", "Audio", 219.00, 54, "PAR-01", ("casque", "anc"), 0, (19.0, 21.0, 8.5, 0.29), 4.7),
    ("SKU-1014", "Micro USB Aria Cast", "Audio", 149.00, 27, "PAR-01", ("micro", "cardioïde"), 0, (9.0, 23.0, 9.0, 0.55), 4.6),
    ("SKU-1015", "Enceintes monitoring Aria M5", "Audio", 329.00, 14, "LYS-02", ("enceintes", "studio"), 3, (18.5, 28.5, 24.1, 5.20), 4.5),
    ("SKU-1016", "Processeur Zenith 9 7950", "Composants", 549.00, 22, "PAR-01", ("cpu", "16-coeurs"), 2, (4.0, 0.6, 4.0, 0.05), 4.9),
    ("SKU-1017", "Carte graphique Nova RX 16G", "Composants", 899.00, 7, "PAR-01", ("gpu", "16go"), 2, (32.0, 5.6, 13.6, 1.48), 4.8),
    ("SKU-1018", "Mémoire DDR5 32 Go (2x16)", "Composants", 124.00, 118, "PAR-01", ("ram", "ddr5"), 2, (13.3, 0.7, 3.5, 0.08), 4.7),
    ("SKU-1019", "Alimentation 850 W Gold Core P8", "Composants", 139.00, 41, "MRS-03", ("psu", "modulaire"), 3, (15.0, 8.6, 16.0, 1.62), 4.6),
    ("SKU-1020", "Boîtier moyen tour Core C5", "Composants", 109.00, 19, "MRS-03", ("boîtier", "atx"), 3, (21.5, 47.0, 44.5, 7.30), 4.4),
    ("SKU-1021", "Webcam 4K Iris Pro", "Périphériques", 159.00, 63, "LYS-02", ("webcam", "4k"), 1, (10.0, 4.5, 6.0, 0.17), 4.3),
    ("SKU-1022", "Station d'accueil USB-C Hub 12", "Périphériques", 189.00, 3, "LYS-02", ("dock", "thunderbolt"), 1, (19.0, 2.4, 8.2, 0.42), 4.2),
    ("SKU-1023", "Onduleur 1500 VA Guard U15", "Énergie", 249.00, 16, "MRS-03", ("onduleur", "line-interactive"), 4, (14.5, 22.0, 39.0, 11.40), 4.5),
    ("SKU-1024", "Multiprise parafoudre Guard S8", "Énergie", 34.90, 260, "MRS-03", ("multiprise", "parafoudre"), 4, (38.0, 4.5, 6.0, 0.52), 4.4),
)

_SEED_TIME_MS = 1_760_000_000_000  # horodatage fixe : catalogue initial déterministe

# CPython refuse de convertir en texte un entier de plus de 4300 chiffres (garde-fou contre le
# déni de service), or 5000! en compte 16 326. L'entier est donc écrit par tranches, chacune
# sous le plancher de cette limite (640 chiffres) : le réglage global de l'interpréteur reste intact.
_DECIMAL_CHUNK_DIGITS = 600
_DECIMAL_CHUNK = 10 ** _DECIMAL_CHUNK_DIGITS


def _build_catalog() -> dict[str, dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    for pid, name, category, price, stock, warehouse, tags, supplier, dims, rating in _CATALOG:
        width, height, depth, weight = dims
        products[pid] = {
            "id": pid,
            "name": name,
            "category": category,
            "price": price,
            "stock": stock,
            "warehouse": warehouse,
            "tags": list(tags),
            "dimensions": {"width_cm": width, "height_cm": height, "depth_cm": depth, "weight_kg": weight},
            "supplier": dict(_SUPPLIERS[supplier]),
            "rating": rating,
            "active": True,
            "updated_at_ms": _SEED_TIME_MS,
            "version": 1,
            "description": f"{name} — {category.lower()}, expédié depuis l'entrepôt {warehouse}.",
        }
    return products


def _decimal(value: int) -> str:
    """Écriture décimale d'un entier positif, quelle que soit sa taille."""
    chunks: list[str] = []
    while value >= _DECIMAL_CHUNK:
        value, low = divmod(value, _DECIMAL_CHUNK)
        chunks.append(f"{low:0{_DECIMAL_CHUNK_DIGITS}d}")
    chunks.append(str(value))
    return "".join(reversed(chunks))


def _require_int(name: str, value: Any, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgument(f"« {name} » doit être un entier (reçu : {type(value).__name__})", param=name)
    if minimum is not None and value < minimum:
        raise InvalidArgument(f"« {name} » doit être ≥ {minimum} (reçu : {value})", param=name)
    if maximum is not None and value > maximum:
        raise InvalidArgument(f"« {name} » doit être ≤ {maximum} (reçu : {value})", param=name)
    return value


def _require_str(name: str, value: Any, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidArgument(f"« {name} » doit être une chaîne (reçu : {type(value).__name__})", param=name)
    if not value and not allow_empty:
        raise InvalidArgument(f"« {name} » est obligatoire", param=name)
    return value


class InventoryService:
    """Implémentation en mémoire, sûre vis-à-vis des threads."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    # -- cycle de vie ---------------------------------------------------------

    def reset(self) -> None:
        """Restaure le catalogue initial et oublie tout l'historique."""
        with self._lock:
            self._products = _build_catalog()
            self._idempotency: dict[str, dict[str, Any]] = {}
            self._movements: deque[dict[str, Any]] = deque(maxlen=500)
            self._calls: Counter[str] = Counter()
            self._operations = 0
            self._deduplicated = 0

    # -- procédures exposées --------------------------------------------------

    def calculate_factorial(self, n: int) -> dict[str, Any]:
        """Calcule n! — le résultat est renvoyé en chaîne (il dépasse vite 64 bits)."""
        _require_int("n", n, 0, MAX_FACTORIAL_N)
        started = time.perf_counter_ns()
        result = _decimal(math.factorial(n))
        compute_us = (time.perf_counter_ns() - started) / 1000
        self._count("calculate_factorial")
        return {"n": n, "result": result, "digits": len(result), "compute_us": compute_us}

    def get_product_details(self, product_id: str) -> dict[str, Any]:
        """Fiche complète d'un produit."""
        _require_str("product_id", product_id)
        with self._lock:
            self._count("get_product_details")
            return deepcopy(self._get(product_id))

    def update_stock(self, product_id: str, delta: int, idempotency_key: str = "") -> dict[str, Any]:
        """Ajoute ``delta`` (positif ou négatif) au stock.

        Opération NON idempotente : la rejouer applique l'effet une seconde fois.
        Avec une ``idempotency_key``, un rejeu renvoie le premier résultat sans
        rien modifier (``applied`` vaut alors ``False``).
        """
        _require_str("product_id", product_id)
        _require_int("delta", delta, -1_000_000, 1_000_000)
        _require_str("idempotency_key", idempotency_key, allow_empty=True)
        with self._lock:
            self._count("update_stock")
            if idempotency_key and idempotency_key in self._idempotency:
                self._deduplicated += 1
                return {**self._idempotency[idempotency_key], "applied": False}
            product = self._get(product_id)
            previous = product["stock"]
            new_stock = previous + delta
            if new_stock < 0:
                raise InsufficientStock(
                    f"Stock insuffisant pour {product_id} : {previous} en stock, {-delta} demandés",
                    product_id=product_id,
                    stock=previous,
                    requested=-delta,
                )
            now_ms = int(time.time() * 1000)
            product["stock"] = new_stock
            product["version"] += 1
            product["updated_at_ms"] = now_ms
            self._operations += 1
            self._movements.append({"product_id": product_id, "delta": delta, "at_ms": now_ms})
            result = {
                "product_id": product_id,
                "previous_stock": previous,
                "new_stock": new_stock,
                "delta": delta,
                "version": product["version"],
                "applied": True,
                "updated_at_ms": now_ms,
            }
            if idempotency_key:
                self._idempotency[idempotency_key] = result
            return dict(result)

    def list_products(self, limit: int = 20, category: str = "") -> dict[str, Any]:
        """Liste paginée du catalogue, filtrable par catégorie."""
        _require_int("limit", limit, 0, 5000)
        _require_str("category", category, allow_empty=True)
        with self._lock:
            self._count("list_products")
            matching = [p for p in self._products.values() if not category or p["category"] == category]
            # Au-delà du catalogue réel, on cycle : utile pour faire grossir le payload en benchmark.
            products = [deepcopy(matching[i % len(matching)]) for i in range(limit)] if matching else []
            return {"products": products, "total": len(matching)}

    def stream_analytics(self, samples: int = 10, interval_ms: int = 200) -> Iterator[dict[str, Any]]:
        """Flux d'indicateurs d'inventaire : un instantané toutes les ``interval_ms``."""
        _require_int("samples", samples, 1, 500)
        _require_int("interval_ms", interval_ms, 0, 5000)
        self._count("stream_analytics")
        return self._analytics(samples, interval_ms)

    def bulk_update_stock(self, updates: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Applique une série de mouvements de stock et renvoie un bilan unique."""
        applied = rejected = total_delta = 0
        errors: list[str] = []
        for update in updates:
            try:
                result = self.update_stock(
                    update.get("product_id", ""),
                    update.get("delta", 0),
                    update.get("idempotency_key", ""),
                )
            except (InvalidArgument, ProductNotFound, InsufficientStock) as exc:
                rejected += 1
                errors.append(exc.message)
            else:
                applied += 1
                total_delta += result["delta"] if result["applied"] else 0
        self._count("bulk_update_stock")
        return {"applied": applied, "rejected": rejected, "total_delta": total_delta, "errors": errors}

    def check_stock(self, product_id: str) -> dict[str, Any]:
        """Niveau de stock d'un produit (utilisé par le flux bidirectionnel gRPC)."""
        _require_str("product_id", product_id)
        with self._lock:
            self._count("check_stock")
            product = self._get(product_id)
            return {
                "product_id": product_id,
                "stock": product["stock"],
                "available": product["stock"] > 0,
                "warehouse": product["warehouse"],
            }

    # -- observation ----------------------------------------------------------

    def product_ids(self) -> list[str]:
        with self._lock:
            return list(self._products)

    def categories(self) -> list[str]:
        with self._lock:
            return sorted({p["category"] for p in self._products.values()})

    def stats(self) -> dict[str, Any]:
        """Photographie de l'état du service (pour le dashboard et les démos)."""
        with self._lock:
            products = self._products.values()
            return {
                "products": len(self._products),
                "total_units": sum(p["stock"] for p in products),
                "inventory_value": round(sum(p["stock"] * p["price"] for p in products), 2),
                "low_stock_count": sum(p["stock"] < LOW_STOCK_THRESHOLD for p in products),
                "operations": self._operations,
                "deduplicated": self._deduplicated,
                "calls": dict(self._calls),
                "recent_movements": list(self._movements)[-20:],
            }

    # -- interne --------------------------------------------------------------

    def _get(self, product_id: str) -> dict[str, Any]:
        try:
            return self._products[product_id]
        except KeyError:
            raise ProductNotFound(f"Produit inconnu : {product_id}", product_id=product_id) from None

    def _count(self, method: str) -> None:
        with self._lock:
            self._calls[method] += 1

    def _analytics(self, samples: int, interval_ms: int) -> Iterator[dict[str, Any]]:
        rng = random.Random(samples * 7919 + interval_ms)
        product_ids = self.product_ids()
        for seq in range(1, samples + 1):
            if seq > 1 and interval_ms:
                time.sleep(interval_ms / 1000)
            state = self.stats()
            movements = state["recent_movements"]
            hot = movements[-1]["product_id"] if movements else rng.choice(product_ids)
            yield {
                "seq": seq,
                "timestamp_ms": int(time.time() * 1000),
                "total_units": state["total_units"],
                "inventory_value": state["inventory_value"],
                "low_stock_count": state["low_stock_count"],
                "orders_per_min": round(120 + 45 * math.sin(seq / 2.5) + rng.uniform(-12, 12), 2),
                "hot_product_id": hot,
                "operations": state["operations"],
            }
