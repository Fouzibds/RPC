"""Configuration centrale du laboratoire RPC Explorer.

Tous les ports sont dérivés d'une base + ``RPCX_PORT_OFFSET`` afin de pouvoir
lancer plusieurs instances du laboratoire en parallèle (tests, démos).
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

APP_NAME = "RPC Explorer"
APP_TAGLINE = "Benchmark Lab"
VERSION = "1.0.0"

ROOT_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT_DIR / "reports"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


HOST = os.environ.get("RPCX_HOST", "127.0.0.1")
PORT_OFFSET = _env_int("RPCX_PORT_OFFSET", 0)


@dataclass(frozen=True)
class Ports:
    """Plan d'adressage du laboratoire."""

    custom: int = 9101        # serveur JSON-RPC maison (accès direct)
    custom_proxy: int = 9201  # même serveur, à travers le proxy de chaos
    custom_v2: int = 9102     # serveur JSON-RPC « contrat v2 » (breaking changes)
    grpc: int = 50051         # serveur gRPC (accès direct)
    grpc_proxy: int = 50151   # même serveur, à travers le proxy de chaos
    grpc_v2: int = 50052      # serveur gRPC « contrat v2 » (breaking changes)
    rest: int = 8081          # API REST de référence (accès direct)
    rest_proxy: int = 8181    # même API, à travers le proxy de chaos
    dashboard: int = 8000     # dashboard web

    def shifted(self, offset: int) -> "Ports":
        return Ports(**{name: port + offset for name, port in asdict(self).items()})

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def default_ports(offset: int | None = None) -> Ports:
    """Ports par défaut, décalés de ``RPCX_PORT_OFFSET`` (ou de ``offset``)."""
    return Ports().shifted(PORT_OFFSET if offset is None else offset)


# --- Protocoles comparés -----------------------------------------------------

PROTOCOLS: tuple[str, ...] = ("local", "custom", "grpc", "rest")
REMOTE_PROTOCOLS: tuple[str, ...] = ("custom", "grpc", "rest")

PROTOCOL_LABELS: dict[str, str] = {
    "local": "Appel local",
    "custom": "JSON-RPC maison",
    "grpc": "gRPC / Protobuf",
    "rest": "REST / JSON",
}

PROTOCOL_TRANSPORTS: dict[str, str] = {
    "local": "Aucun — appel de fonction en mémoire",
    "custom": "TCP + trame préfixée par sa longueur + JSON-RPC 2.0",
    "grpc": "HTTP/2 + Protobuf binaire (contrat IDL)",
    "rest": "HTTP/1.1 + JSON (ressources & verbes)",
}

# --- Valeurs par défaut ------------------------------------------------------

DEFAULT_TIMEOUT_S = 5.0          # délai maximal d'un appel distant
CONNECT_TIMEOUT_S = 2.0          # délai maximal d'établissement de connexion
MAX_FRAME_BYTES = 16 * 1024 * 1024  # taille maximale d'une trame JSON-RPC maison
MAX_PAYLOAD_CAPTURE = 64 * 1024  # octets conservés par évènement de trace
MAX_FACTORIAL_N = 5000
LOW_STOCK_THRESHOLD = 10
