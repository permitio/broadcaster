import asyncio
import logging
import os
import socket
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from .._base import Event
from .base import BroadcastBackend

logger = logging.getLogger(__name__)

try:
    POOL_MAX_SIZE = int(os.getenv("BROADCASTER_PG_MAX_POOL_SIZE"))
except TypeError:
    POOL_MAX_SIZE = 10

# TCP keepalive on the Postgres connections.
#
# A LISTEN connection is idle by nature: it sends nothing and waits for NOTIFYs.
# If the server's address goes silent without closing the socket (RDS Multi-AZ
# failover, network partition, node death) the client never receives a FIN/RST,
# the kernel keeps the socket ESTABLISHED forever, and the listener is deaf
# without any error. libpq clients get SO_KEEPALIVE for free (``keepalives=1``
# is the libpq default); asyncpg exposes no such option (MagicStack/asyncpg#606),
# so we set it on the socket ourselves, with the libpq parameter names so the
# knobs look familiar in the URL:
#
#   postgres://user:pw@host/db?keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=3
#
# Defaults: on, 30 s idle, 10 s between probes, 3 lost probes -> the connection
# errors within ~60 s of the peer going silent and asyncpg's termination
# listener fires. ``keepalives=0`` disables it. The parameters are stripped from
# the URL before it reaches asyncpg (asyncpg would otherwise forward unknown
# query parameters to the server as session settings).
KEEPALIVE_PARAMS = (
    "keepalives",
    "keepalives_idle",
    "keepalives_interval",
    "keepalives_count",
)
DEFAULT_KEEPALIVE: Dict[str, int] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}


def split_keepalive_options(url: str) -> Tuple[str, Dict[str, int]]:
    """Return ``(url_without_keepalive_params, keepalive_options)``.

    Unknown or absent parameters fall back to DEFAULT_KEEPALIVE; a value that is
    not an integer raises ValueError (a typo in the URL should not silently turn
    keepalive off).
    """
    parts = urlsplit(url)
    options = dict(DEFAULT_KEEPALIVE)
    remaining = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in KEEPALIVE_PARAMS:
            options[key] = int(value)
        else:
            remaining.append((key, value))
    clean_url = urlunsplit(parts._replace(query=urlencode(remaining)))
    return clean_url, options


def apply_tcp_keepalive(sock: Any, idle: int, interval: int, count: int) -> bool:
    """Enable TCP keepalive on ``sock`` with the given timings (seconds).

    Linux names the timers TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT; macOS
    spells idle ``TCP_KEEPALIVE``; platforms exposing none of them still get
    SO_KEEPALIVE with the kernel defaults. Returns True if SO_KEEPALIVE was set.
    Never raises: a socket that refuses an option (e.g. a Unix socket) is left as
    is and the failure is logged.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except (OSError, AttributeError) as exc:
        logger.warning("could not enable SO_KEEPALIVE on the Postgres socket: %r", exc)
        return False
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
        socket, "TCP_KEEPALIVE", None
    )
    for opt, value in (
        (idle_opt, idle),
        (getattr(socket, "TCP_KEEPINTVL", None), interval),
        (getattr(socket, "TCP_KEEPCNT", None), count),
    ):
        if opt is None or value is None or int(value) <= 0:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, int(value))
        except OSError as exc:
            logger.warning(
                "could not set TCP keepalive option %s=%s: %r", opt, value, exc
            )
    return True


def _socket_of(conn: Any) -> Optional[Any]:
    transport = getattr(conn, "_transport", None)
    if transport is None:
        return None
    return transport.get_extra_info("socket")


class PostgresBackend(BroadcastBackend):
    _pools: Dict[str, Any] = {}
    _pools_lock = asyncio.Lock()

    def __init__(self, url: str):
        self._url = url
        self._dsn, self._keepalive = split_keepalive_options(url)

    async def _init_connection(self, conn: Any) -> None:
        """asyncpg pool ``init`` hook: runs once for every new connection."""
        if not self._keepalive["keepalives"]:
            return
        sock = _socket_of(conn)
        if sock is None:
            logger.warning(
                "asyncpg connection exposes no socket; TCP keepalive not applied"
            )
            return
        apply_tcp_keepalive(
            sock,
            self._keepalive["keepalives_idle"],
            self._keepalive["keepalives_interval"],
            self._keepalive["keepalives_count"],
        )

    async def _get_pool(self) -> Any:
        async with self.__class__._pools_lock:
            if self._url not in self.__class__._pools:
                self.__class__._pools[self._url] = await asyncpg.create_pool(
                    self._dsn, max_size=POOL_MAX_SIZE, init=self._init_connection
                )
            return self.__class__._pools[self._url]

    async def connect(self) -> None:
        self._conn = await (await self._get_pool()).acquire()
        self._listen_queue: asyncio.Queue = asyncio.Queue()
        self._conn.add_termination_listener(self._termination_listener)

    async def disconnect(self) -> None:
        try:
            self._conn.remove_termination_listener(self._termination_listener)
        except Exception:
            # Best effort, would fail if conn already closed (thus released)
            pass

        await (await self._get_pool()).release(self._conn)
        self._conn = None

    async def subscribe(self, channel: str) -> None:
        await self._conn.add_listener(channel, self._listener)

    async def unsubscribe(self, channel: str) -> None:
        try:
            await self._conn.remove_listener(channel, self._listener)
        except Exception:
            # Best effort, would fail if conn already closed (thus released)
            pass

    async def publish(self, channel: str, message: str) -> None:
        await self._conn.execute("SELECT pg_notify($1, $2);", channel, message)

    async def _listener(self, *args: Any) -> None:
        connection, pid, channel, payload = args
        event = Event(channel=channel, message=payload)
        await self._listen_queue.put(event)

    async def _termination_listener(self, *args: Any) -> None:
        await self._listen_queue.put(None)

    async def next_published(self) -> Event:
        return await self._listen_queue.get()
