"""Orchestrateur du laboratoire : un seul objet démarre tout et fabrique les clients.

Le CLI, le dashboard, les bancs d'essai et les tests ne construisent jamais un
serveur eux-mêmes : ils passent par ``LabRuntime``. C'est lui qui garantit que
les trois middlewares sont comparés à armes égales ::

    client ───────────────────────────▶ serveur     accès direct (boucle locale idéale)
    client ──▶ proxy de chaos ────────▶ serveur     accès à travers le réseau simulé

* un seul ``InventoryService`` derrière les trois serveurs : la « procédure
  distante » est la même, seul le middleware change ;
* un seul ``NetworkConditions`` derrière les trois proxys : régler une latence
  ou une panne soumet les trois protocoles exactement au même réseau ;
* un second inventaire, isolé, derrière les deux serveurs « contrat v2 » : les
  démonstrations de rupture de contrat ne faussent ni les stocks ni les mesures.

``LabRuntime.ephemeral()`` laisse le système choisir tous les ports : plusieurs
laboratoires cohabitent alors sans se gêner (c'est ce que font les tests).
"""
from __future__ import annotations

import dataclasses
import socket
import threading
import time
from contextlib import ExitStack
from typing import Any, Callable, Protocol

from common.client_api import InventoryClient, LocalInventoryClient
from common.config import (
    APP_NAME,
    DEFAULT_TIMEOUT_S,
    HOST,
    PROTOCOL_LABELS,
    PROTOCOL_TRANSPORTS,
    PROTOCOLS,
    REMOTE_PROTOCOLS,
    VERSION,
    Ports,
    default_ports,
)
from common.inventory import InventoryService
from common.telemetry import BUS, EventBus, TraceCollector
from netsim import ChaosProxy, NetworkConditions
from rest_api.rest_client import RestInventoryClient
from rest_api.rest_server import create_rest_server
from rpc_custom import CustomInventoryClient, build_inventory_skeleton, build_inventory_skeleton_v2
from rpc_grpc.grpc_client import GrpcInventoryClient
from rpc_grpc.grpc_server import create_grpc_server
from rpc_grpc.grpc_server_v2 import GrpcServerV2Handle, create_grpc_server_v2

CONTRACT_PROTOCOLS: tuple[str, ...] = ("custom", "grpc")  # protocoles dotés d'un serveur « contrat v2 »

# Décalage conseillé entre deux laboratoires sur la même machine. Surtout pas 100 : chaque proxy
# écoute 100 ports au-dessus de son serveur, les serveurs du second tomberaient sur les proxys du premier.
PORT_OFFSET_STEP = 200

_PROXY_SUFFIX = "_proxy"
_V2_SUFFIX = "_v2"
_PROBE_TIMEOUT_S = 0.15   # sonde d'état : un serveur local vivant accepte la connexion en quelques ms
_PROBE_TTL_S = 1.0        # durée de validité d'une sonde (le dashboard interroge l'état chaque seconde)

# Ordre de démarrage = ordre des dépendances. Chaque nom est aussi un champ de ``Ports``.
_BOOT_ORDER: tuple[str, ...] = (
    "custom", "grpc", "rest",                      # les serveurs d'abord…
    "custom_proxy", "grpc_proxy", "rest_proxy",    # …puis leurs proxys, qui visent leurs ports réels…
    "custom_v2", "grpc_v2",                        # …enfin les serveurs « contrat v2 ».
)

_COMPONENT_LABELS: dict[str, str] = {
    "custom": "le serveur JSON-RPC maison",
    "grpc": "le serveur gRPC",
    "rest": "le serveur REST",
    "custom_proxy": "le proxy de chaos JSON-RPC",
    "grpc_proxy": "le proxy de chaos gRPC",
    "rest_proxy": "le proxy de chaos REST",
    "custom_v2": "le serveur JSON-RPC « contrat v2 »",
    "grpc_v2": "le serveur gRPC « contrat v2 »",
}

_REMOTE_CLIENTS: dict[str, Callable[..., InventoryClient]] = {
    "custom": CustomInventoryClient,
    "grpc": GrpcInventoryClient,
    "rest": RestInventoryClient,
}


class _Component(Protocol):
    """Ce que le laboratoire attend de tout composant réseau, serveur ou proxy (plan §4)."""

    @property
    def port(self) -> int: ...

    @property
    def running(self) -> bool: ...

    def start(self) -> Any: ...

    def stop(self) -> None: ...


class LabRuntime:
    """Le laboratoire complet dans un seul processus.

    Attributs publics : ``host``, ``ports`` (ports réels après ``start()``),
    ``bus``, ``service`` et ``service_v2`` (inventaires distincts),
    ``conditions`` (réseau simulé, commun aux trois proxys), ``servers``
    (``custom``, ``grpc``, ``rest``, ``custom_v2``, ``grpc_v2``), ``proxies``
    (``custom``, ``grpc``, ``rest``), ``grpc_v2`` (pour son interrupteur
    ``strict``) et ``collector`` (traces regroupées par appel).

    Les composants sont construits une fois pour toutes : un laboratoire arrêté
    peut être relancé — il reprend les mêmes ports, et ses réglages (conditions
    réseau, ``strict``) survivent.
    """

    def __init__(self, ports: Ports | None = None, *, host: str = HOST, bus: EventBus = BUS) -> None:
        self.host = host
        self.ports = ports if ports is not None else default_ports()
        self.bus = bus
        self.service = InventoryService()
        # Inventaire séparé : un client v1 face au serveur v2 ne doit pas toucher aux stocks mesurés.
        self.service_v2 = InventoryService()
        self.conditions = NetworkConditions()
        self.collector = TraceCollector(bus)
        self.grpc_v2: GrpcServerV2Handle = create_grpc_server_v2(self.service_v2, host, self.ports.grpc_v2, bus=bus)
        self.servers: dict[str, _Component] = {
            "custom": build_inventory_skeleton(self.service, host, self.ports.custom, bus=bus),
            "grpc": create_grpc_server(self.service, host, self.ports.grpc, bus=bus),
            "rest": create_rest_server(self.service, host, self.ports.rest, bus=bus),
            "custom_v2": build_inventory_skeleton_v2(self.service_v2, host, self.ports.custom_v2, bus=bus),
            "grpc_v2": self.grpc_v2,
        }
        self.proxies: dict[str, ChaosProxy] = {
            protocol: ChaosProxy(
                protocol,
                getattr(self.ports, protocol + _PROXY_SUFFIX),
                getattr(self.ports, protocol),
                listen_host=host,
                target_host=host,
                conditions=self.conditions,   # le MÊME objet pour les trois : un seul réseau simulé
                bus=bus,
            )
            for protocol in REMOTE_PROTOCOLS
        }
        self._lifecycle = threading.Lock()
        self._started = False
        self._started_at = 0.0
        self._probe_lock = threading.Lock()
        self._probes: dict[str, tuple[float, bool]] = {}   # serveur -> (instant de la sonde, résultat)

    @classmethod
    def ephemeral(cls, *, host: str = HOST, bus: EventBus = BUS) -> "LabRuntime":
        """Laboratoire dont tous les ports sont attribués par le système au démarrage.

        Après ``start()``, ``ports`` contient les ports réellement obtenus (sauf
        ``dashboard``, qui n'appartient pas au laboratoire et reste à 0).
        """
        return cls(Ports(**dict.fromkeys(Ports().as_dict(), 0)), host=host, bus=bus)

    # -- cycle de vie ---------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> "LabRuntime":
        """Démarre serveurs, proxys, serveurs « contrat v2 » puis la collecte des traces. Idempotent.

        Tout ou rien : si un composant ne peut pas écouter (port déjà pris),
        ceux qui avaient démarré sont arrêtés et une ``RuntimeError`` explique
        quel port pose problème et comment en changer.
        """
        with self._lifecycle:
            if self._started:
                return self
            # En cas d'échec, ExitStack arrête ce qui avait démarré, dans l'ordre inverse.
            with ExitStack() as rollback:
                for name in _BOOT_ORDER:
                    component = self._component(name)
                    if isinstance(component, ChaosProxy):
                        # Un port éphémère n'est connu qu'une fois son serveur démarré : le proxy vise le
                        # port RÉEL, et reprend son propre port d'écoute d'un démarrage à l'autre.
                        component.target_port = self.servers[name.removesuffix(_PROXY_SUFFIX)].port
                        component.listen_port = getattr(self.ports, name)
                    try:
                        component.start()
                    except OSError as exc:
                        raise RuntimeError(self._port_failure(name, component.port)) from exc
                    rollback.callback(component.stop)
                rollback.pop_all()   # tout écoute : plus rien à défaire
            self.ports = dataclasses.replace(
                self.ports, **{name: self._component(name).port for name in _BOOT_ORDER}
            )
            self.collector.start()
            self._started_at = time.perf_counter()
            self._started = True
        return self

    def stop(self) -> None:
        """Arrête tout dans l'ordre inverse du démarrage et libère les ports. Idempotent.

        Les proxys s'arrêtent avant les serveurs : les connexions des clients
        sont coupées d'abord, aucun appel ne reste suspendu à un serveur disparu.
        """
        with self._lifecycle:
            if not self._started:
                return
            self._started = False
            # ExitStack défait dans l'ordre inverse de l'empilement, et va au bout même si un arrêt échoue.
            with ExitStack() as teardown:
                for name in _BOOT_ORDER:
                    teardown.callback(self._component(name).stop)
                teardown.callback(self.collector.stop)

    def __enter__(self) -> "LabRuntime":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- clients --------------------------------------------------------------

    def endpoint(self, protocol: str, *, via_proxy: bool = False) -> tuple[str, int]:
        """Adresse ``(hôte, port)`` d'un serveur distant : en direct, ou derrière son proxy de chaos."""
        if protocol not in REMOTE_PROTOCOLS:
            raise ValueError(_unknown_protocol(protocol, REMOTE_PROTOCOLS))
        return self.host, self._resolved_port(protocol + _PROXY_SUFFIX if via_proxy else protocol)

    def client(self, protocol: str, *, via_proxy: bool = False, timeout: float | None = None) -> InventoryClient:
        """Nouveau client du contrat v1 ; c'est à l'appelant de le fermer (``close()`` ou ``with``).

        ``via_proxy=True`` fait traverser le réseau simulé ; sans effet pour
        ``local``, qui n'emprunte aucun réseau. ``timeout`` devient le délai par
        défaut des appels de ce client (``DEFAULT_TIMEOUT_S`` si ``None``).
        """
        if protocol not in PROTOCOLS:
            raise ValueError(_unknown_protocol(protocol, PROTOCOLS))
        if protocol == "local":
            return LocalInventoryClient(self.service, bus=self.bus)
        _, port = self.endpoint(protocol, via_proxy=via_proxy)
        return self._remote_client(protocol, port, timeout)

    def client_v2(self, protocol: str, *, timeout: float | None = None) -> InventoryClient:
        """Client resté au contrat v1, branché sur le serveur « contrat v2 » du même protocole.

        C'est tout l'objet du laboratoire « Contrat » : le client n'a pas
        changé, le serveur si — et chaque middleware révèle la rupture à sa façon.
        """
        if protocol not in CONTRACT_PROTOCOLS:
            raise ValueError(_unknown_protocol(protocol, CONTRACT_PROTOCOLS))
        return self._remote_client(protocol, self._resolved_port(protocol + _V2_SUFFIX), timeout)

    # -- réseau simulé --------------------------------------------------------

    def arm(self, kind: str, protocol: str, count: int = 1) -> None:
        """Arme une panne ponctuelle (``reset`` ou ``lost_reply``) sur le proxy d'un protocole."""
        if protocol not in self.proxies:
            raise ValueError(_unknown_protocol(protocol, tuple(self.proxies)))
        self.proxies[protocol].arm(kind, count)

    # -- observation ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """État du laboratoire, sérialisable en JSON (``GET /api/status`` du dashboard)."""
        ports = self.ports
        servers = []
        for server_id in self.servers:
            protocol = server_id.removesuffix(_V2_SUFFIX)
            is_v2 = server_id != protocol
            servers.append({
                "id": server_id,
                "label": PROTOCOL_LABELS[protocol] + (" — contrat v2" if is_v2 else ""),
                "transport": PROTOCOL_TRANSPORTS[protocol],
                "host": self.host,
                "port": getattr(ports, server_id),
                "proxy_port": None if is_v2 else getattr(ports, protocol + _PROXY_SUFFIX),
                "up": self._is_up(server_id),
            })
        uptime_s = time.perf_counter() - self._started_at if self._started else 0.0
        return {
            "app": {"name": APP_NAME, "version": VERSION, "uptime_s": round(uptime_s, 3)},
            "servers": servers,
            "network": self.conditions.snapshot(),
            "proxies": {protocol: proxy.stats() for protocol, proxy in self.proxies.items()},
            "totals": self._totals(),
            "inventory": self.service.stats(),
        }

    def reset(self) -> None:
        """Remet le laboratoire dans son état initial, sans rien redémarrer.

        Inventaires, conditions réseau, pannes armées, compteurs des proxys,
        interrupteur ``strict`` du serveur v2 et traces collectées.
        """
        self.service.reset()
        self.service_v2.reset()
        self.conditions.reset()
        for proxy in self.proxies.values():
            proxy.disarm()   # une panne armée mais jamais déclenchée frapperait le premier appel suivant
            proxy.reset_stats()
        self.grpc_v2.strict = False
        self.collector.clear()

    # -- interne --------------------------------------------------------------

    def _component(self, name: str) -> _Component:
        """Composant qui occupe le port ``name`` (un champ de ``Ports``)."""
        if name.endswith(_PROXY_SUFFIX):
            return self.proxies[name.removesuffix(_PROXY_SUFFIX)]
        return self.servers[name]

    def _port_failure(self, name: str, port: int) -> str:
        return (
            f"Démarrage du laboratoire impossible : {_COMPONENT_LABELS[name]} ne peut pas écouter sur "
            f"{self.host}:{port}. Le port {port} est déjà utilisé par un autre programme (peut-être une autre "
            f"instance du laboratoire) ou réservé par le système. Libérez-le, ou décalez tous les ports du "
            f"laboratoire avec la variable d’environnement RPCX_PORT_OFFSET (par exemple "
            f"RPCX_PORT_OFFSET={PORT_OFFSET_STEP})."
        )

    def _resolved_port(self, name: str) -> int:
        port = getattr(self.ports, name)
        if not port:
            raise RuntimeError(
                "Le laboratoire n’est pas démarré : ses ports éphémères ne sont attribués que par start()."
            )
        return port

    def _remote_client(self, protocol: str, port: int, timeout: float | None) -> InventoryClient:
        timeout = DEFAULT_TIMEOUT_S if timeout is None else timeout
        return _REMOTE_CLIENTS[protocol](self.host, port, timeout=timeout, bus=self.bus)

    def _is_up(self, server_id: str) -> bool:
        """Le serveur accepte-t-il réellement une connexion TCP ? (résultat gardé ``_PROBE_TTL_S``)

        On sonde le port direct, jamais celui du proxy : la sonde ne doit ni subir
        les pannes simulées ni apparaître dans les compteurs du réseau.
        """
        server = self.servers[server_id]
        if not server.running:
            # Sous Windows, une connexion vers un port fermé n'échoue qu'au bout du délai : inutile de l'attendre.
            return False
        with self._probe_lock:
            probe = self._probes.get(server_id)
            if probe is not None and time.monotonic() - probe[0] < _PROBE_TTL_S:
                return probe[1]
            try:
                socket.create_connection((self.host, server.port), timeout=_PROBE_TIMEOUT_S).close()
            except OSError:
                up = False
            else:
                up = True
            self._probes[server_id] = (time.monotonic(), up)
            return up

    def _totals(self) -> dict[str, dict[str, int | float]]:
        """Compteurs du collecteur par protocole ; la durée cumulée devient une moyenne par appel."""
        recorded = dict(self.collector.totals)
        totals: dict[str, dict[str, int | float]] = {}
        # Les quatre protocoles sont toujours présents (à zéro au besoin) : la forme ne dépend pas du trafic.
        for protocol in dict.fromkeys((*PROTOCOLS, *recorded)):
            counters = dict(recorded.get(protocol, {}))
            calls = int(counters.get("calls", 0))
            totals[protocol] = {
                "calls": calls,
                "errors": int(counters.get("errors", 0)),
                "bytes_out": int(counters.get("bytes_out", 0)),
                "bytes_in": int(counters.get("bytes_in", 0)),
                "avg_ms": round(counters["total_us"] / calls / 1000, 3) if calls else 0.0,
            }
        return totals


def _unknown_protocol(protocol: object, expected: tuple[str, ...]) -> str:
    return f"Protocole inconnu : {protocol!r} (attendu : {', '.join(expected)})"
