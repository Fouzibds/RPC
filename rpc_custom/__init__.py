"""Phase 1 — RPC « fait maison » : JSON-RPC 2.0 sur des sockets TCP, sans aucun framework.

Lecture conseillée, dans l'ordre du trajet d'un appel :

1. ``protocol.py``          ce qui circule sur le fil (messages JSON-RPC, tramage) ;
2. ``client_stub.py``       le stub : appel local → octets → résultat ;
3. ``server_skeleton.py``   le squelette : octets → procédure → octets ;
4. ``inventory_binding.py`` le branchement du service métier sur ce middleware ;
5. ``custom_rpc_demo.py``   la démonstration (``python -m rpc_custom.custom_rpc_demo``).
"""
from .client_stub import RpcClientStub, RpcFuture
from .inventory_binding import (
    CustomInventoryClient,
    build_inventory_skeleton,
    build_inventory_skeleton_v2,
)
from .server_skeleton import RpcServerSkeleton

__all__ = [
    "CustomInventoryClient",
    "RpcClientStub",
    "RpcFuture",
    "RpcServerSkeleton",
    "build_inventory_skeleton",
    "build_inventory_skeleton_v2",
]
