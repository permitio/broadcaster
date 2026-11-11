import typing
from urllib.parse import urlparse

import asyncio_redis

from .._base import Event
from ._keepalive import apply_tcp_keepalive
from .base import BroadcastBackend

SUBSCRIBER_KEEPALIVE_IDLE = 30
SUBSCRIBER_KEEPALIVE_INTERVAL = 10
SUBSCRIBER_KEEPALIVE_COUNT = 3


class RedisBackend(BroadcastBackend):
    def __init__(self, url: str):
        parsed_url = urlparse(url)
        self._host = parsed_url.hostname or "localhost"
        self._port = parsed_url.port or 6379
        self._password = parsed_url.password or None

    async def connect(self) -> None:
        kwargs = {"host": self._host, "port": self._port, "password": self._password}
        self._pub_conn = await asyncio_redis.Connection.create(**kwargs)
        # The subscriber connection must not reconnect on its own. asyncio_redis'
        # auto-reconnect re-runs AUTH/SELECT while the protocol is still flagged as
        # "in pubsub"; the client rejects them ("Cannot run command inside pubsub
        # subscription") inside a fire-and-forget task, so SUBSCRIBE is never
        # re-sent and the subscriber stays deaf without any error. Messages
        # published while the connection was down are lost in any case. Instead,
        # a lost connection ends next_published() with None: Broadcast then ends
        # all subscribers (Unsubscribed), like the Postgres backend does when its
        # connection terminates, and the application can reconnect and resync.
        self._sub_conn = await asyncio_redis.Connection.create(
            **kwargs, auto_reconnect=False
        )
        sock = self._sub_conn.transport.get_extra_info("socket")
        if sock is not None:
            apply_tcp_keepalive(
                sock,
                SUBSCRIBER_KEEPALIVE_IDLE,
                SUBSCRIBER_KEEPALIVE_INTERVAL,
                SUBSCRIBER_KEEPALIVE_COUNT,
            )
        self._subscriber = await self._sub_conn.start_subscribe()

        # asyncio_redis only uses this callback to schedule its auto-reconnect.
        # Take it over to wake up next_published() when the connection is lost.
        messages_queue = self._subscriber._messages_queue
        self._sub_conn.protocol._connection_lost_callback = (
            lambda: messages_queue.put_nowait(None)
        )

    async def disconnect(self) -> None:
        self._pub_conn.close()
        self._sub_conn.close()

    async def subscribe(self, channel: str) -> None:
        await self._subscriber.subscribe([channel])

    async def unsubscribe(self, channel: str) -> None:
        if not self._sub_conn.protocol.is_connected:
            return
        await self._subscriber.unsubscribe([channel])

    async def publish(self, channel: str, message: typing.Any) -> None:
        await self._pub_conn.publish(channel, message)

    async def next_published(self) -> typing.Optional[Event]:
        message = await self._subscriber.next_published()
        if message is None:
            return None
        return Event(channel=message.channel, message=message.value)
