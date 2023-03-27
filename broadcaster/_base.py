import asyncio
import logging
import random
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, AsyncIterator, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("broadcaster")


class Event:
    def __init__(self, channel: str, message: str) -> None:
        self.channel = channel
        self.message = message

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Event)
            and self.channel == other.channel
            and self.message == other.message
        )

    def __repr__(self) -> str:
        return f"Event(channel={self.channel!r}, message={self.message!r})"


class Unsubscribed(Exception):
    pass


class Broadcast:
    def __init__(self, url: str, retry_connection=True):
        self.retry_connection = retry_connection

        from broadcaster._backends.base import BroadcastBackend

        parsed_url = urlparse(url)
        self._backend: BroadcastBackend
        self._subscribers: Dict[str, Any] = {}
        self._listener_task = None

        if parsed_url.scheme in ("redis", "rediss"):
            from broadcaster._backends.redis import RedisBackend

            self._backend = RedisBackend(url)

        elif parsed_url.scheme in ("postgres", "postgresql"):
            from broadcaster._backends.postgres import PostgresBackend

            self._backend = PostgresBackend(url)

        elif parsed_url.scheme == "kafka":
            from broadcaster._backends.kafka import KafkaBackend

            self._backend = KafkaBackend(url)

        elif parsed_url.scheme == "memory":
            from broadcaster._backends.memory import MemoryBackend

            self._backend = MemoryBackend(url)

        else:
            raise ValueError(f'Unsupported backend {url}')

    async def __aenter__(self) -> "Broadcast":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
        await self.disconnect()

    async def connect(
        self, initial_wait=0.2, max_wait=5.0, incremental_max_wait=2.0
    ) -> None:
        if self._listener_task:
            self._listener_task.cancel()
        self._listener_task = asyncio.create_task(self._listener())
        wait_time = initial_wait
        logger.info(f"Connecting...")
        while True:
            try:
                await self._backend.connect()
                logger.info(f"Connected")
                break
            except:
                logger.error("Error connecting to DB")
                if self.retry_connection:
                    await asyncio.sleep(wait_time)
                    wait_time = min(max_wait, wait_time)
                    wait_time += random.random() * incremental_max_wait  # nosec
                else:
                    raise

    async def disconnect(self) -> None:
        if self._listener_task.done():
            self._listener_task.result()
        else:
            self._listener_task.cancel()
        await self._backend.disconnect()

    async def _listener(self) -> None:
        while True:
            event = await self._backend.next_published()
            if event is None:
                # Backend is disconnected
                logger.error("Disconnected")
                if self.retry_connection:
                    await self.connect()
                    continue
                else:
                    break

            for queue in list(self._subscribers.get(event.channel, [])):
                await queue.put(event)

        # Unsubscribe all
        for queue in sum([list(qs) for qs in self._subscribers.values()], []):
            await queue.put(None)

    async def publish(self, channel: str, message: Any) -> None:
        await self._backend.publish(channel, message)

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator["Subscriber"]:
        queue: asyncio.Queue = asyncio.Queue()

        try:
            if not self._subscribers.get(channel):
                await self._backend.subscribe(channel)
                self._subscribers[channel] = {queue}
            else:
                self._subscribers[channel].add(queue)

            yield Subscriber(queue)

            self._subscribers[channel].remove(queue)
            if not self._subscribers.get(channel):
                del self._subscribers[channel]
                await self._backend.unsubscribe(channel)
        finally:
            await queue.put(None)


class Subscriber:
    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    async def __aiter__(self) -> Optional[AsyncGenerator]:
        try:
            while True:
                yield await self.get()
        except Unsubscribed:
            pass

    async def get(self) -> Event:
        item = await self._queue.get()
        if item is None:
            raise Unsubscribed()
        return item
