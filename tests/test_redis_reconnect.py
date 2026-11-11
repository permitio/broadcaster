"""A lost Redis subscriber connection ends the subscribers instead of going deaf.

Why: asyncio_redis' auto-reconnect re-sends AUTH while the protocol is still
flagged as "in pubsub"; the client rejects it inside a fire-and-forget task, so
SUBSCRIBE is never re-sent and, with a password-protected Redis, the subscriber
silently stops receiving messages. The backend now disables auto-reconnect on the
subscriber connection and surfaces the loss as Unsubscribed instead.

These tests kill the subscriber connection server-side (CLIENT KILL TYPE pubsub),
which is what Redis failovers and managed-Redis maintenance do.
"""
import asyncio
import socket
from typing import AsyncIterator, Optional, cast

import asyncio_redis
import pytest
import pytest_asyncio
from asyncio_redis.protocol import _NoTransaction

from broadcaster import Broadcast
from broadcaster._backends.redis import SUBSCRIBER_KEEPALIVE_INTERVAL, RedisBackend
from broadcaster._base import Unsubscribed

HOST, PORT = "localhost", 6379
PASSWORD = "broadcaster-test-password"


async def _redis_command(*args: str, password: Optional[str] = None) -> object:
    conn = await asyncio_redis.Connection.create(
        HOST, PORT, password=password, auto_reconnect=False
    )
    try:
        return await conn.protocol._query(
            _NoTransaction, *[arg.encode() for arg in args]
        )
    finally:
        conn.close()


async def _wait_until_subscribed(channel: str, password: Optional[str] = None) -> None:
    # asyncio_redis does not wait for the SUBSCRIBE confirmation; a publish right
    # after subscribe() can reach Redis first and get lost.
    for _ in range(50):
        reply = await _redis_command("PUBSUB", "NUMSUB", channel, password=password)
        counts = [await item for item in reply]  # type: ignore[attr-defined]
        if int(counts[1]) > 0:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"no subscriber on {channel!r}")


async def _kill_subscriber_connections(password: Optional[str] = None) -> None:
    killed = await _redis_command("CLIENT", "KILL", "TYPE", "pubsub", password=password)
    assert killed, "no pubsub connection was killed"


@pytest_asyncio.fixture
async def redis_password() -> AsyncIterator[str]:
    """Require a password on the test Redis for the duration of one test."""
    await _redis_command("CONFIG", "SET", "requirepass", PASSWORD)
    try:
        yield PASSWORD
    finally:
        await _redis_command("CONFIG", "SET", "requirepass", "", password=PASSWORD)


async def _assert_lost_connection_unsubscribes(
    url: str, password: Optional[str] = None
) -> None:
    async with Broadcast(url) as broadcast:
        async with broadcast.subscribe("chatroom") as subscriber:
            await _wait_until_subscribed("chatroom", password)
            await broadcast.publish("chatroom", "before")
            event = await asyncio.wait_for(subscriber.get(), timeout=5)
            assert event.message == "before"

            await _kill_subscriber_connections(password)

            with pytest.raises(Unsubscribed):
                await asyncio.wait_for(subscriber.get(), timeout=5)


@pytest.mark.asyncio
async def test_lost_subscriber_connection_unsubscribes():
    await _assert_lost_connection_unsubscribes(f"redis://{HOST}:{PORT}")


@pytest.mark.asyncio
async def test_lost_subscriber_connection_unsubscribes_with_password(redis_password):
    await _assert_lost_connection_unsubscribes(
        f"redis://:{redis_password}@{HOST}:{PORT}", redis_password
    )


@pytest.mark.asyncio
async def test_new_broadcast_works_after_connection_loss(redis_password):
    url = f"redis://:{redis_password}@{HOST}:{PORT}"
    await _assert_lost_connection_unsubscribes(url, redis_password)

    async with Broadcast(url) as broadcast:
        async with broadcast.subscribe("chatroom") as subscriber:
            await _wait_until_subscribed("chatroom", redis_password)
            await broadcast.publish("chatroom", "after")
            event = await asyncio.wait_for(subscriber.get(), timeout=5)
            assert event.message == "after"


@pytest.mark.asyncio
async def test_subscriber_socket_has_tcp_keepalive():
    async with Broadcast(f"redis://{HOST}:{PORT}") as broadcast:
        backend = cast(RedisBackend, broadcast._backend)
        sock = backend._sub_conn.transport.get_extra_info("socket")
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        if hasattr(socket, "TCP_KEEPINTVL"):
            assert (
                sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)
                == SUBSCRIBER_KEEPALIVE_INTERVAL
            )
