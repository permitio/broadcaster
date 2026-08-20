"""TCP keepalive on the Postgres backend.

Why: a LISTEN connection is idle by nature. If the server's address goes silent
without closing the socket (RDS Multi-AZ failover, partition) the client never
gets a FIN/RST and, without keepalive, stays ESTABLISHED-and-deaf forever.
asyncpg exposes no keepalive option, so the backend sets it on the socket.
"""
import socket

import pytest

from broadcaster import Broadcast
from broadcaster._backends.postgres import (
    DEFAULT_KEEPALIVE,
    PostgresBackend,
    apply_tcp_keepalive,
    split_keepalive_options,
)

PG_URL = "postgres://postgres:postgres@localhost:5432/broadcaster"


@pytest.fixture(autouse=True)
def _fresh_pools():
    """The backend caches asyncpg pools per URL at class level; a pool is bound to
    the event loop that created it and pytest-asyncio gives every test its own
    loop, so start and end each test with an empty cache."""
    PostgresBackend._pools = {}
    yield
    PostgresBackend._pools = {}


# --- URL parsing (no database needed) ---------------------------------------


def test_defaults_when_url_has_no_keepalive_params():
    clean, opts = split_keepalive_options(PG_URL)
    assert clean == PG_URL
    assert opts == DEFAULT_KEEPALIVE
    assert opts["keepalives"] == 1  # on by default, like libpq


def test_libpq_style_params_are_honoured_and_stripped_from_the_dsn():
    url = (
        PG_URL
        + "?keepalives_idle=5&sslmode=require&keepalives_interval=2&keepalives_count=7"
    )
    clean, opts = split_keepalive_options(url)
    assert opts == {
        "keepalives": 1,
        "keepalives_idle": 5,
        "keepalives_interval": 2,
        "keepalives_count": 7,
    }
    # our params must not reach asyncpg (it forwards unknown query params to the
    # server as session settings, which would fail the connection); other
    # params are preserved.
    assert clean == PG_URL + "?sslmode=require"


def test_keepalives_zero_disables():
    _, opts = split_keepalive_options(PG_URL + "?keepalives=0")
    assert opts["keepalives"] == 0


def test_non_integer_value_is_an_error_not_a_silent_default():
    with pytest.raises(ValueError):
        split_keepalive_options(PG_URL + "?keepalives_idle=soon")


def test_backend_parses_its_url_on_construction():
    backend = PostgresBackend(PG_URL + "?keepalives_idle=11")
    assert backend._dsn == PG_URL
    assert backend._keepalive["keepalives_idle"] == 11


# --- socket options on a real TCP socket (no database needed) -----------------


def _tcp_pair():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname())
    peer, _ = server.accept()
    return client, peer, server


def test_apply_tcp_keepalive_sets_the_options():
    client, peer, server = _tcp_pair()
    try:
        assert apply_tcp_keepalive(client, idle=5, interval=2, count=3) is True
        assert (
            client.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        )  # macOS returns the option bit (8), Linux 1
        if hasattr(socket, "TCP_KEEPINTVL"):
            assert client.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 2
        if hasattr(socket, "TCP_KEEPCNT"):
            assert client.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
        idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
            socket, "TCP_KEEPALIVE", None
        )
        if idle_opt is not None:
            assert client.getsockopt(socket.IPPROTO_TCP, idle_opt) == 5
    finally:
        client.close()
        peer.close()
        server.close()


def test_apply_tcp_keepalive_never_raises_on_a_closed_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.close()
    assert apply_tcp_keepalive(sock, idle=5, interval=2, count=3) is False


# --- end to end against the test Postgres -------------------------------------


def _listener_socket(broadcast: Broadcast):
    # the pooled connection is a proxy; the real asyncpg Connection is ._con
    conn = broadcast._backend._conn
    inner = getattr(conn, "_con", conn)
    return inner._transport.get_extra_info("socket")


@pytest.mark.asyncio
async def test_pool_connections_get_keepalive_by_default():
    async with Broadcast(PG_URL) as broadcast:
        sock = _listener_socket(broadcast)
        assert (
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        )  # macOS returns the option bit (8), Linux 1
        if hasattr(socket, "TCP_KEEPINTVL"):
            assert (
                sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)
                == DEFAULT_KEEPALIVE["keepalives_interval"]
            )


@pytest.mark.asyncio
async def test_pool_connections_honour_url_params():
    # distinct URL -> distinct pool (pools are keyed by the full URL)
    url = PG_URL + "?keepalives_idle=7&keepalives_interval=4&keepalives_count=2"
    async with Broadcast(url) as broadcast:
        sock = _listener_socket(broadcast)
        assert (
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        )  # macOS returns the option bit (8), Linux 1
        if hasattr(socket, "TCP_KEEPINTVL"):
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 4
        if hasattr(socket, "TCP_KEEPCNT"):
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 2
        # and the connection still works end to end
        async with broadcast.subscribe("chatroom") as subscriber:
            await broadcast.publish("chatroom", "hello")
            event = await subscriber.get()
            assert event.message == "hello"


@pytest.mark.asyncio
async def test_keepalives_zero_leaves_the_socket_alone():
    async with Broadcast(PG_URL + "?keepalives=0") as broadcast:
        sock = _listener_socket(broadcast)
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 0
