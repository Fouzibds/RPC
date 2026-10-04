"""Proxy TCP de chaos : une vraie connexion sur laquelle on injecte les défauts du réseau.

Le proxy s'intercale entre un client et son serveur, à la manière de Toxiproxy. Il ne
comprend pas les messages qu'il transporte : il retarde, avale ou coupe des octets. JSON-RPC
maison, gRPC et REST subissent ainsi exactement les mêmes conditions, et c'est le vrai code
réseau de chaque client (timeouts, reconnexion, erreurs) qui est mis à l'épreuve.

Organisation : un fil accepte les connexions ; chaque connexion a une pompe par sens, qui lit
les octets et décide de leur sort, et un écrivain par sens, qui les remet à leur heure.
"""
from __future__ import annotations

import os
import random
import socket
import struct
import threading
import time
from collections import deque
from contextlib import suppress
from typing import Any, Callable

from common.config import CONNECT_TIMEOUT_S, HOST
from common.telemetry import BUS, EventBus

from .conditions import NetworkConditions

ARM_KINDS: tuple[str, ...] = ("reset", "lost_reply")

_COUNTERS: tuple[str, ...] = (
    "connections_total",
    "bytes_up",
    "bytes_down",
    "chunks_up",
    "chunks_down",
    "resets",
    "refused",
    "blackholed",
    "lost_replies",
)

_RECV_BYTES = 64 * 1024
_HIGH_WATER_BYTES = 8 * 1024 * 1024   # au-delà, la pompe attend : la mémoire reste bornée
_SWEEP_INTERVAL_S = 0.05              # délai maximal pour appliquer une panne à une connexion inactive
_NAP_S = 0.05                         # sommeil maximal d'un écrivain entre deux contrôles de fermeture
_SLICE_S = 0.02                       # débit limité : tranches d'environ 20 ms de transmission
_MIN_SLICE_BYTES = 256
_JOIN_TIMEOUT_S = 2.0
# time.sleep() dépasse sa cible d'une à deux millisecondes sous Windows (bien moins ailleurs).
_SLEEP_OVERSHOOT_S = 0.002 if os.name == "nt" else 0.0002
# SO_LINGER actif avec un délai nul : close() émet un RST au lieu du FIN d'une fermeture polie.
_LINGER_RESET = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
_HTTP2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

_RESET_TEXT: dict[str, str] = {
    "armed": "Coupure armée : la connexion est réinitialisée et la requête n’atteint pas le serveur.",
    "probability": "Coupure aléatoire : la connexion est réinitialisée à l’arrivée de la requête.",
    "down": "Serveur en panne : la connexion en cours est coupée.",
    "blackhole_end": "Fin du trou noir : des octets ont été perdus, la connexion est réinitialisée.",
}
_REFUSE_TEXT: dict[str, str] = {
    "down": "Serveur en panne : la connexion est coupée dès son ouverture.",
    "upstream_unreachable": "Le serveur ne répond pas derrière le proxy : la connexion est coupée.",
}


def _close_socket(sock: socket.socket, *, reset: bool) -> None:
    """Ferme ``sock`` ; avec ``reset``, le pair voit une coupure (RST) et non une fin de flux (FIN)."""
    if reset:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RD)  # réveille un recv() bloqué sans rien émettre vers le pair
        with suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_RESET)
    with suppress(OSError):
        sock.close()


class _Http2CallDetector:
    """Repère, dans un flux HTTP/2 client → serveur, les morceaux qui ouvrent un appel.

    Une connexion gRPC transporte aussi des trames de service (SETTINGS, PING, WINDOW_UPDATE…).
    Sans ce tri, une panne « armée pour la prochaine requête » serait consommée par un simple
    accusé de réception. Seul un morceau contenant une trame HEADERS — l'ouverture d'un flux,
    donc d'un appel — compte comme une requête.
    """

    _FRAME_HEADER_BYTES = 9
    _HEADERS_FRAME = 0x01

    def __init__(self) -> None:
        self._skip = len(_HTTP2_PREFACE)  # octets restants avant le prochain en-tête de trame
        self._header = bytearray()

    def feed(self, data: bytes) -> bool:
        """Avance dans le flux ; vrai si une trame HEADERS commence dans ``data``."""
        opens_call = False
        position, size = 0, len(data)
        while position < size:
            if self._skip:
                step = min(self._skip, size - position)
                self._skip -= step
                position += step
                continue
            take = min(self._FRAME_HEADER_BYTES - len(self._header), size - position)
            self._header += data[position:position + take]
            position += take
            if len(self._header) == self._FRAME_HEADER_BYTES:
                self._skip = int.from_bytes(self._header[:3], "big")
                opens_call = opens_call or self._header[3] == self._HEADERS_FRAME
                self._header.clear()
        return opens_call


class _DelayLine:
    """Ligne à retard d'un sens de circulation.

    Chaque morceau reçoit une heure de sortie (arrivée + délai) puis attend son tour dans une
    file. Le délai se comporte donc comme une distance à parcourir, pas comme un verrou : dix
    requêtes envoyées d'affilée arrivent toutes après UN délai, et non dix.
    """

    def __init__(self, destination: socket.socket, on_broken: Callable[[], Any]) -> None:
        self._destination = destination
        self._on_broken = on_broken
        self._cond = threading.Condition()
        self._queue: deque[tuple[float, bytes | None, Callable[[], Any] | None]] = deque()
        self._queued_bytes = 0
        self._writing = False
        self._closed = False
        self._last_release = 0.0
        self._wire_free_at = 0.0

    def send(self, data: bytes, delay_s: float, bandwidth_kbps: float) -> float:
        """Remet ``data`` à la destination après ``delay_s`` ; renvoie l'heure de sortie prévue.

        Lève ``OSError`` si la destination ne répond plus.
        """
        now = time.perf_counter()
        with self._cond:
            while self._queued_bytes > _HIGH_WATER_BYTES and not self._closed:
                self._cond.wait(0.1)
            if self._closed:
                return now
            if bandwidth_kbps > 0:
                return self._enqueue_throttled(data, now, delay_s, bandwidth_kbps * 125.0)
            if delay_s > 0 or self._queue or self._writing:
                return self._enqueue(now + delay_s, data, None)
        # Aucun délai et rien en attente : écriture directe, sans passer par l'écrivain.
        # Une seule pompe alimente la ligne, l'ordre des octets ne peut donc pas être troublé.
        self._destination.sendall(data)
        return now

    def schedule(self, action: Callable[[], Any], release: float) -> None:
        """Place ``action`` (fin de flux, coupure) dans la file, derrière les octets déjà partis."""
        with self._cond:
            self._enqueue(release, None, action)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._queue.clear()
            self._queued_bytes = 0
            self._cond.notify_all()

    def run(self) -> None:
        """Boucle de l'écrivain : remet les morceaux dans l'ordre d'arrivée, chacun à son heure."""
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                release = self._queue[0][0]
            self._wait_for(release)
            with self._cond:
                if self._closed:
                    return
                _, data, action = self._queue.popleft()
                self._queued_bytes -= len(data) if data else 0
                self._writing = True
            try:
                if action is not None:
                    action()
                else:
                    self._destination.sendall(data)
            except OSError:
                self._on_broken()
                return
            with self._cond:
                self._writing = False
                self._cond.notify_all()

    def _enqueue(self, release: float, data: bytes | None, action: Callable[[], Any] | None) -> float:
        # TCP livre dans l'ordre : un morceau ne peut pas doubler celui qui le précède.
        release = max(release, self._last_release)
        self._last_release = release
        self._queue.append((release, data, action))
        self._queued_bytes += len(data) if data else 0
        self._cond.notify_all()
        return release

    def _enqueue_throttled(self, data: bytes, now: float, delay_s: float, bytes_per_s: float) -> float:
        """Débit limité : chaque tranche occupe le lien le temps de sa transmission, puis voyage."""
        slice_bytes = max(_MIN_SLICE_BYTES, int(bytes_per_s * _SLICE_S))
        release = now
        for offset in range(0, len(data), slice_bytes):
            piece = data[offset:offset + slice_bytes]
            self._wire_free_at = max(now, self._wire_free_at) + len(piece) / bytes_per_s
            release = self._enqueue(self._wire_free_at + delay_s, piece, None)
        return release

    def _wait_for(self, release: float) -> None:
        """Dort jusqu'à ``release``, ou jusqu'à la fermeture de la ligne.

        ``Condition.wait(timeout)`` a une granularité d'environ 15 ms sous Windows, ce qui
        fausserait les petites latences : on dort avec ``time.sleep()``, plus fin, et on
        parcourt le dernier millième en cédant simplement la main.
        """
        while not self._closed:
            remaining = release - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(remaining - _SLEEP_OVERSHOOT_S, _NAP_S) if remaining > _SLEEP_OVERSHOOT_S else 0)


class _Link:
    """Une connexion cliente et sa jumelle vers le serveur."""

    def __init__(self, client: socket.socket, upstream: socket.socket, on_closed: Callable[["_Link"], None]) -> None:
        self.client = client
        self.upstream = upstream
        self.up = _DelayLine(upstream, self.abort)    # client → serveur
        self.down = _DelayLine(client, self.abort)    # serveur → client
        self.closed = False
        self.tainted = False      # des octets ont été avalés : le flux n'est plus cohérent
        self.reply_lost = False   # panne « réponse perdue » en cours sur cette connexion
        self.connect_deadline: float | None = None  # heure limite de la connexion au serveur
        self.settled = False      # admission tranchée : reliée au serveur, ou abandonnée (voir ChaosProxy.links)
        self._on_closed = on_closed
        self._lock = threading.Lock()
        self._open_directions = 2
        self._sniffed = False
        self._http2: _Http2CallDetector | None = None

    def opens_call(self, data: bytes) -> bool:
        """Vrai si ce morceau client → serveur porte le début d'une requête."""
        if not self._sniffed:
            self._sniffed = True
            if data.startswith(_HTTP2_PREFACE):
                self._http2 = _Http2CallDetector()
        return self._http2.feed(data) if self._http2 else True

    def end_of_stream(self, destination: socket.socket) -> None:
        """Propage une fin de flux ; la connexion se ferme quand les deux sens sont terminés."""
        with suppress(OSError):
            destination.shutdown(socket.SHUT_WR)
        with self._lock:
            self._open_directions -= 1
            finished = self._open_directions == 0
        if finished:
            self._shutdown(reset=False)

    def abort(self, announce: Callable[[], Any] | None = None) -> bool:
        """Coupe net les deux côtés ; vrai si c'est cet appel qui a fermé la connexion.

        ``announce`` n'est appelé que dans ce cas, et AVANT la coupure : quand le client la
        constate, compteurs et évènements sont déjà à jour.
        """
        return self._shutdown(reset=True, announce=announce)

    def _shutdown(self, *, reset: bool, announce: Callable[[], Any] | None = None) -> bool:
        with self._lock:
            if self.closed:
                return False
            self.closed = True
        self._on_closed(self)
        if announce is not None:
            announce()
        self.up.close()
        self.down.close()
        _close_socket(self.client, reset=reset)
        _close_socket(self.upstream, reset=reset)
        return True


class ChaosProxy:
    """Relais TCP ``listen_port`` → ``target_port`` qui applique des ``NetworkConditions``.

    L'objet ``conditions`` peut être partagé entre plusieurs proxys ; il est relu à chaque
    morceau d'octets. ``seed`` rend les tirages aléatoires reproductibles ; ``hold_ms`` est le
    sursis laissé au serveur avant la coupure d'une panne « réponse perdue ».

    Une « requête » est un morceau d'octets client → serveur : c'est sur lui que portent les
    pics, les coupures aléatoires et les pannes armées. Sur une connexion HTTP/2 (gRPC), seuls
    les morceaux qui ouvrent un appel comptent, pas les trames de service.

    Compteurs (``stats()``) : ``connections_total`` compte toutes les connexions reçues, y
    compris celles refusées ; ``bytes_*`` / ``chunks_*`` ne comptent que ce qui est réellement
    relayé ; ``resets`` compte les coupures injectées (armées, aléatoires, panne, sortie de
    trou noir) ; ``refused`` les connexions coupées dès l'ouverture (panne ou serveur muet) ;
    ``blackholed`` les morceaux avalés ; ``lost_replies`` les pannes « réponse perdue ».
    Ils sont mis à jour AVANT que le client ne puisse constater l'effet correspondant.
    """

    def __init__(
        self,
        name: str,
        listen_port: int,
        target_port: int,
        *,
        listen_host: str = HOST,
        target_host: str = HOST,
        conditions: NetworkConditions | None = None,
        bus: EventBus = BUS,
        seed: int | None = None,
        hold_ms: float = 150,
    ) -> None:
        self.name = name
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.conditions = conditions if conditions is not None else NetworkConditions()
        self.bus = bus
        self.hold_ms = hold_ms
        self._rng = random.Random(seed)
        self._port = listen_port
        self._lifecycle = threading.Lock()
        self._lock = threading.Lock()   # protège liens, fils, compteurs et pannes armées
        self._stopping = threading.Event()
        self._listener: socket.socket | None = None
        self._acceptor: threading.Thread | None = None
        self._links: set[_Link] = set()
        self._threads: list[threading.Thread] = []
        self._counters: dict[str, int] = dict.fromkeys(_COUNTERS, 0)
        self._armed: dict[str, int] = dict.fromkeys(ARM_KINDS, 0)
        # Admissions depuis le démarrage, jamais remises à zéro : reliées au serveur, et tranchées (reliées ou abandonnées).
        self._joined = 0
        self._settled = 0
        self._settled_changed = threading.Condition(self._lock)

    # -- cycle de vie ---------------------------------------------------------

    @property
    def port(self) -> int:
        """Port d'écoute réel (celui attribué par le système si ``listen_port=0``)."""
        return self._port

    @property
    def running(self) -> bool:
        return self._listener is not None

    def start(self) -> "ChaosProxy":
        """Ouvre le port d'écoute ; au retour, le proxy accepte déjà les connexions."""
        with self._lifecycle:
            if self._listener is not None:
                return self
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if os.name == "nt":
                    # Sous Windows, SO_REUSEADDR laisserait un second processus écouter le même
                    # port : on exige l'exclusivité pour qu'un port occupé soit bien détecté.
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self.listen_host, self.listen_port))
                listener.listen(128)
            except OSError as exc:
                listener.close()
                raise OSError(
                    exc.errno,
                    f"Proxy « {self.name} » : impossible d’écouter sur "
                    f"{self.listen_host}:{self.listen_port} ({exc.strerror or exc})",
                ) from exc
            listener.settimeout(_SWEEP_INTERVAL_S)
            self._port = listener.getsockname()[1]
            self._stopping.clear()
            self._listener = listener
            self._acceptor = threading.Thread(
                target=self._accept_loop, args=(listener,), name=f"chaos-{self.name}-accept", daemon=True
            )
            self._acceptor.start()
        return self

    def stop(self) -> None:
        """Arrête tout : port libéré, connexions en cours coupées, fils terminés. Idempotent."""
        with self._lifecycle:
            listener, self._listener = self._listener, None
            if listener is None:
                return
            self._stopping.set()
            _close_socket(listener, reset=False)
            if self._acceptor is not None:
                self._acceptor.join(_JOIN_TIMEOUT_S)
                self._acceptor = None
            with self._lock:
                links = list(self._links)
                threads, self._threads = self._threads, []
            for link in links:
                link.abort()
            for thread in threads:
                thread.join(_JOIN_TIMEOUT_S)

    def __enter__(self) -> "ChaosProxy":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- pannes ponctuelles ---------------------------------------------------

    def arm(self, kind: str, count: int = 1) -> None:
        """Arme une panne pour les ``count`` prochaines requêtes (``count=0`` désarme).

        * ``reset`` : la requête coupe la connexion SANS atteindre le serveur ;
        * ``lost_reply`` : la requête atteint le serveur, qui l'exécute, mais la réponse est
          avalée et la connexion coupée après ``hold_ms`` — l'appelant ne peut pas savoir si
          l'effet a eu lieu.
        """
        if kind not in ARM_KINDS:
            raise ValueError(f"Panne inconnue : {kind!r} (attendu : {' ou '.join(ARM_KINDS)})")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"« count » doit être un entier ≥ 0 (reçu : {count!r})")
        with self._lock:
            self._armed[kind] = count

    def disarm(self) -> None:
        """Annule toutes les pannes armées qui n'ont pas encore été déclenchées."""
        with self._lock:
            self._armed = dict.fromkeys(ARM_KINDS, 0)

    def armed(self) -> dict[str, int]:
        """Nombre de pannes armées restant à déclencher, par type."""
        with self._lock:
            return dict(self._armed)

    # -- observation ----------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Compteurs du proxy, dans l'ordre du plan (§9)."""
        with self._lock:
            counters = dict(self._counters)
            active = len(self._links)
        return {"connections_total": counters.pop("connections_total"), "connections_active": active, **counters}

    def reset_stats(self) -> None:
        """Remet les compteurs à zéro (``connections_active`` reflète toujours l'instant présent)."""
        with self._lock:
            self._counters = dict.fromkeys(_COUNTERS, 0)

    def links(self) -> tuple[int, int]:
        """``(reliées, tranchées)`` : connexions admises depuis le démarrage, puis reliées au serveur,
        et toutes celles dont l'admission est tranchée (reliées, refusées, serveur injoignable).

        Le client voit sa connexion ouverte dès la poignée de main TCP avec le proxy ; le
        proxy, lui, ne l'accepte et ne joint le serveur qu'ensuite. Avec ``wait_links``, cela
        permet d'attendre que le trajet entier soit ouvert avant de mesurer un appel.
        """
        with self._lock:
            return self._joined, self._settled

    def wait_links(self, settled: int, timeout: float) -> tuple[int, int]:
        """Attend que ``settled`` admissions soient tranchées, au plus ``timeout`` secondes ; renvoie ``links()``."""
        with self._settled_changed:
            self._settled_changed.wait_for(lambda: self._settled >= settled, timeout)
            return self._joined, self._settled

    # -- acceptation ----------------------------------------------------------

    def _accept_loop(self, listener: socket.socket) -> None:
        while not self._stopping.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                client = None
            except OSError:
                # Socket d'écoute fermée par stop(), ou client reparti avant d'être accepté.
                if self._stopping.wait(_SWEEP_INTERVAL_S):
                    return
                continue
            self._sweep()
            if client is not None:
                self._admit(client)

    def _sweep(self) -> None:
        """Ronde périodique des connexions silencieuses.

        Leur applique ce que leurs pompes ne verraient qu'au prochain octet : panne, sortie de
        trou noir, serveur qui ne répond pas.
        """
        conditions = self.conditions.snapshot()
        now = time.perf_counter()
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            links = list(self._links)
        for link in links:
            deadline = link.connect_deadline
            if deadline is not None and now >= deadline:
                self._unreachable(link)
            elif conditions["down"]:
                self._cut(link, "down")
            elif link.tainted and not conditions["blackhole"]:
                self._cut(link, "blackhole_end")

    def _admit(self, client: socket.socket) -> None:
        self._count("connections_total")
        if self.conditions.snapshot()["down"]:
            # Refuser en fermant le port serait plus fidèle, mais Windows met alors ~2 s à
            # signaler l'échec au client : accepter puis couper échoue tout de suite.
            self._refuse(client, "down")
            self._settle(None, joined=False)
            return
        try:
            client.settimeout(None)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            self._refuse(client, "upstream_unreachable")
            self._settle(None, joined=False)
            return
        link = _Link(client, upstream, self._forget)
        with self._lock:
            self._links.add(link)
        if not self._spawn(lambda: self._serve(link), "up"):
            link.abort()
            self._settle(link, joined=False)

    def _serve(self, link: _Link) -> None:
        """Fil d'une connexion : joint le serveur, lance les autres fils, puis pompe client → serveur."""
        try:
            # Connexion faite ici et non dans le fil d'acceptation : un serveur muet ne doit
            # pas empêcher le proxy d'accueillir (ou de refuser) les connexions suivantes.
            self._connect_upstream(link)
        except OSError:
            self._unreachable(link)
            return
        workers = (
            (link.up.run, "up-writer"),
            (link.down.run, "down-writer"),
            (lambda: self._pump(link, upward=False), "down"),
        )
        if not all(self._spawn(target, role) for target, role in workers):
            link.abort()
            self._settle(link, joined=False)
            return
        self._settle(link, joined=True)
        self._pump(link, upward=True)

    def _connect_upstream(self, link: _Link) -> None:
        """Joint le serveur en ``CONNECT_TIMEOUT_S`` au plus ; lève ``OSError`` en cas d'échec."""
        upstream, address = link.upstream, (self.target_host, self.target_port)
        if os.name == "nt":
            # Sous Windows, une connexion locale faite avec un délai (socket non bloquante)
            # n'aboutit qu'au battement d'horloge suivant : ~15 ms perdues à chaque nouvelle
            # connexion. On se connecte donc en mode bloquant, et c'est la ronde (_sweep) qui
            # fait respecter le délai en fermant la socket.
            link.connect_deadline = time.perf_counter() + CONNECT_TIMEOUT_S
            try:
                upstream.connect(address)
            finally:
                link.connect_deadline = None
        else:
            upstream.settimeout(CONNECT_TIMEOUT_S)
            upstream.connect(address)
            upstream.settimeout(None)
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _unreachable(self, link: _Link) -> None:
        link.abort(lambda: self._fault("refused", "network.refuse", 0, reason="upstream_unreachable",
                                       text=_REFUSE_TEXT["upstream_unreachable"]))
        self._settle(link, joined=False)

    def _settle(self, link: _Link | None, *, joined: bool) -> None:
        """Tranche l'admission d'une connexion, une seule fois (sous Windows, la ronde et le fil de
        la connexion peuvent tous deux constater un serveur injoignable) ; réveille ``wait_links``."""
        with self._settled_changed:
            if link is not None:
                if link.settled:
                    return
                link.settled = True
            self._settled += 1
            if joined:
                self._joined += 1
            self._settled_changed.notify_all()

    def _spawn(self, target: Callable[[], Any], role: str) -> bool:
        """Lance un fil de connexion ; refuse (faux) si le proxy est en cours d'arrêt."""
        thread = threading.Thread(target=target, name=f"chaos-{self.name}-{role}", daemon=True)
        with self._lock:
            if self._stopping.is_set():
                return False
            thread.start()
            self._threads.append(thread)
        return True

    def _forget(self, link: _Link) -> None:
        with self._lock:
            self._links.discard(link)

    # -- relais ---------------------------------------------------------------

    def _pump(self, link: _Link, *, upward: bool) -> None:
        """Lit un sens de la connexion et décide du sort de chaque morceau."""
        if upward:
            source, line, destination, handle = link.client, link.up, link.upstream, self._on_request_bytes
        else:
            source, line, destination, handle = link.upstream, link.down, link.client, self._on_reply_bytes
        while True:
            try:
                data = source.recv(_RECV_BYTES)
            except OSError:
                link.abort()  # la source a coupé net : la coupure se propage à l'autre extrémité
                return
            if link.closed:
                return
            if not data:
                # Fin de flux : elle voyage comme les octets, derrière ceux qui attendent encore.
                delay_s = self.conditions.snapshot()["latency_ms"] / 2000
                line.schedule(lambda: link.end_of_stream(destination), time.perf_counter() + delay_s)
                return
            try:
                if not handle(link, data):
                    return
            except OSError:
                link.abort()
                return

    def _on_request_bytes(self, link: _Link, data: bytes) -> bool:
        """Sort d'un morceau client → serveur ; faux si la connexion vient d'être coupée."""
        conditions = self.conditions.snapshot()
        if conditions["down"]:
            self._cut(link, "down", len(data))
            return False
        if link.tainted and not conditions["blackhole"]:
            self._cut(link, "blackhole_end", len(data))
            return False
        opens_call = link.opens_call(data)
        if opens_call and not link.reply_lost:
            if self._take_armed("reset"):
                self._cut(link, "armed", len(data))
                return False
            if self._take_armed("lost_reply"):
                self._lose_reply(link, data, conditions)
                return True
        if conditions["blackhole"]:
            self._swallow(link, data, upward=True)
            return True
        spike = False
        if opens_call:
            probability = conditions["reset_probability"]
            if probability > 0 and self._rng.random() < probability:
                self._cut(link, "probability", len(data))
                return False
            probability = conditions["spike_probability"]
            spike = probability > 0 and self._rng.random() < probability
        self._relay(link, data, conditions, upward=True, spike=spike)
        return True

    def _on_reply_bytes(self, link: _Link, data: bytes) -> bool:
        """Sort d'un morceau serveur → client ; faux si la connexion vient d'être coupée."""
        conditions = self.conditions.snapshot()
        if conditions["down"]:
            self._cut(link, "down", len(data))
            return False
        if link.tainted and not conditions["blackhole"]:
            self._cut(link, "blackhole_end", len(data))
            return False
        if link.reply_lost:
            return True  # la réponse est avalée : le client n'en verra rien
        if conditions["blackhole"]:
            self._swallow(link, data, upward=False)
            return True
        self._relay(link, data, conditions, upward=False, spike=False)
        return True

    def _relay(self, link: _Link, data: bytes, conditions: dict[str, Any], *, upward: bool, spike: bool) -> float:
        """Relaie ``data`` avec le délai du moment ; renvoie l'heure de sortie prévue."""
        # La latence configurée est un aller-retour : chaque sens en porte la moitié.
        delay_ms = conditions["latency_ms"] / 2
        jitter_ms = conditions["jitter_ms"] / 2
        if jitter_ms > 0:
            delay_ms = max(0.0, delay_ms + self._rng.uniform(-jitter_ms, jitter_ms))
        if spike:
            delay_ms += conditions["spike_ms"]
        direction = "up" if upward else "down"
        with self._lock:
            self._counters[f"bytes_{direction}"] += len(data)
            self._counters[f"chunks_{direction}"] += 1
        if delay_ms >= 1.0 and self.bus.enabled:
            self.bus.emit(
                call_id="", protocol=self.name, side="network", stage="network.delay",
                size=len(data), duration_us=delay_ms * 1000,
                detail={"proxy": self.name, "direction": direction, "delay_ms": round(delay_ms, 3),
                        "spike": spike, "bytes": len(data)},
            )
        line = link.up if upward else link.down
        return line.send(data, delay_ms / 1000, conditions["bandwidth_kbps"])

    # -- pannes ---------------------------------------------------------------

    def _take_armed(self, kind: str) -> bool:
        if not self._armed[kind]:
            return False
        with self._lock:
            if not self._armed[kind]:
                return False
            self._armed[kind] -= 1
            return True

    def _cut(self, link: _Link, reason: str, size: int = 0) -> None:
        link.abort(lambda: self._fault("resets", "network.reset", size, reason=reason, text=_RESET_TEXT[reason]))

    def _refuse(self, client: socket.socket, reason: str) -> None:
        self._fault("refused", "network.refuse", 0, reason=reason, text=_REFUSE_TEXT[reason])
        _close_socket(client, reset=True)

    def _swallow(self, link: _Link, data: bytes, *, upward: bool) -> None:
        # Les octets avalés ne seront jamais rejoués : la connexion devra être réinitialisée
        # à la sortie du trou noir, sinon le serveur lirait un flux troué.
        link.tainted = True
        self._fault("blackholed", "network.blackhole", len(data),
                    direction="up" if upward else "down", bytes=len(data),
                    text="Trou noir : les octets sont avalés, personne ne répondra.")

    def _lose_reply(self, link: _Link, data: bytes, conditions: dict[str, Any]) -> None:
        """La requête passe, la réponse est avalée, puis la connexion est coupée côté client."""
        link.reply_lost = True
        self._fault("lost_replies", "network.lost_reply", len(data), bytes=len(data), hold_ms=self.hold_ms,
                    text="La requête atteint le serveur, mais sa réponse est perdue : "
                         "l’appelant ignore si l’effet a eu lieu.")
        delivered_at = self._relay(link, data, conditions, upward=True, spike=False)
        # Le compte à rebours part de la remise au serveur : il a le temps d'exécuter la requête.
        link.down.schedule(link.abort, delivered_at + self.hold_ms / 1000)

    # -- comptage & publication -----------------------------------------------

    def _count(self, counter: str) -> None:
        with self._lock:
            self._counters[counter] += 1

    def _fault(self, counter: str, stage: str, size: int, **detail: Any) -> None:
        """Compte une panne injectée et la publie sur le bus (s'il est actif)."""
        self._count(counter)
        bus = self.bus
        if bus.enabled:
            bus.emit(call_id="", protocol=self.name, side="network", stage=stage, size=size,
                     detail={"proxy": self.name, **detail})
