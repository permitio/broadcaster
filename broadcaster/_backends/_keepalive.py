"""TCP keepalive for backend sockets that sit idle while waiting for messages.

A subscriber connection (Postgres LISTEN, Redis SUBSCRIBE) sends nothing while it
waits. If the server's address goes silent without closing the socket (failover,
network partition, node death) the client never receives a FIN/RST and, without
keepalive, the kernel keeps the socket ESTABLISHED and the subscriber deaf forever.
"""
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)


def apply_tcp_keepalive(sock: Any, idle: int, interval: int, count: int) -> bool:
    """Enable TCP keepalive on ``sock`` with the given timings (seconds).

    Linux names the timers TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT.
    MacOS spells idle ``TCP_KEEPALIVE``.
    Platforms exposing none of them still get SO_KEEPALIVE with the kernel defaults.
    Returns True if SO_KEEPALIVE was set.
    Never raises: a socket that refuses an option (e.g. a Unix socket) is left as
    is and the failure is logged.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except (OSError, AttributeError) as exc:
        logger.warning("could not enable SO_KEEPALIVE on the socket: %r", exc)
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
