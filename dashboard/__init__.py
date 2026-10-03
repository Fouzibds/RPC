"""Dashboard web du laboratoire : API HTTP + WebSocket (``server``) et interface statique (``static/``).

    from dashboard import create_app, serve
"""
from __future__ import annotations

from typing import Any

__all__ = ["create_app", "serve"]


def __getattr__(name: str) -> Any:
    # Import paresseux : importer le paquet ne charge ni FastAPI ni les trois middlewares, et
    # « python -m dashboard.server » n'exécute pas le module une première fois par ce biais.
    if name in __all__:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
