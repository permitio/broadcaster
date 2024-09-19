import asyncio
import typing
from urllib.parse import urlparse
import pulsar
from .._base import Event
from .base import BroadcastBackend


class PulsarBackend(BroadcastBackend):
    def __init__(self, url: str):
        parsed_url = urlparse(url)
        self._host = parsed_url.hostname or "localhost"
        self._port = parsed_url.port or 6650
        self._service_url = f"pulsar://{self._host}:{self._port}"
        self._client = None
        self._producer = None
        self._consumer = None

    async def connect(self) -> None:
        self._client = pulsar.Client(self._service_url)
        self._producer = self._client.create_producer("broadcast")
        self._consumer = self._client.subscribe(
            "broadcast",
            subscription_name="broadcast_subscription",
            consumer_type=pulsar.ConsumerType.Shared,
        )

    async def disconnect(self) -> None:
        if self._producer:
            self._producer.close()
        if self._consumer:
            self._consumer.close()
        if self._client:
            self._client.close()

    async def subscribe(self, channel: str) -> None:
        # In this implementation, we're using a single topic 'broadcast'
        # So we don't need to do anything here
        pass

    async def unsubscribe(self, channel: str) -> None:
        # Similarly, we don't need to do anything here
        pass

    async def publish(self, channel: str, message: typing.Any) -> None:
        encoded_message = f"{channel}:{message}".encode("utf-8")
        await self._run_in_executor(self._producer.send, encoded_message)

    async def next_published(self) -> Event:
        while True:
            msg = await self._run_in_executor(self._consumer.receive)
            channel, content = msg.data().decode("utf-8").split(":", 1)
            await self._run_in_executor(self._consumer.acknowledge, msg)
            return Event(channel=channel, message=content)

    async def _run_in_executor(self, func, *args):
        return await asyncio.get_event_loop().run_in_executor(None, func, *args)
