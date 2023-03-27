import asyncio
import logging
from typing import Any, Optional

import asyncpg

from .base import BroadcastBackend
from .._base import Event

logger = logging.getLogger("broadcaster.postgres")


class PostgresBackend(BroadcastBackend):
    def __init__(self, url: str):
        self._url = url
        self._listen_queue: asyncio.Queue = asyncio.Queue()
        self._conn: Optional[asyncpg.Connection] = None
        self.subscribed_channels = set()

    async def connect(self) -> None:
        if self._conn:
            await self.disconnect()
        while True:
            try:
                connection_attempt = await asyncpg.connect(
                    self._url, timeout=2.0, command_timeout=5.0
                )
                if isinstance(connection_attempt, asyncpg.Connection):
                    self._conn = connection_attempt
                    self._conn.add_termination_listener(self._termination_listener)
                    await self.resubscribe_all()
                    return
                else:
                    raise ConnectionError(f"Can't connect: {connection_attempt}")

            except Exception as e:
                raise ConnectionError(f"Can't connect {e}")

    async def disconnect(self) -> None:
        await self._conn.close()
        self._conn = None

    async def resubscribe_all(self):
        logger.debug(f"Resubscribing to {len(self.subscribed_channels)} channels")
        await asyncio.gather(
            *[self.subscribe(channel) for channel in self.subscribed_channels]
        )

    async def subscribe(self, channel: str) -> None:
        self.subscribed_channels.add(channel)
        await self._conn.add_listener(channel, self._listener)

    async def unsubscribe(self, channel: str) -> None:
        await self._conn.remove_listener(channel, self._listener)
        self.subscribed_channels.remove(channel)

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
