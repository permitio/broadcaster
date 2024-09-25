import anyio
import logging
import typing
from urllib.parse import urlparse
import pulsar
from broadcaster._base import Event
from .base import BroadcastBackend

logger = logging.getLogger(__name__)

class PulsarBackend(BroadcastBackend):
    def __init__(self, url: str):
        parsed_url = urlparse(url)
        self._host = parsed_url.hostname or "localhost"
        self._port = parsed_url.port or 6650
        self._service_url = f"pulsar://{self._host}:{self._port}"
        self._client = None
        self._producers = {}
        self._consumers = {}
        self._subscribed_channels = set()

    async def connect(self) -> None:
        try:
            logger.info("Connecting to Pulsar brokers")
            self._client = await anyio.to_thread.run_sync(
                lambda: pulsar.Client(self._service_url)
            )
            logger.info("Successfully connected to Pulsar brokers")
        except Exception as e:
            logger.error(f"Error connecting to Pulsar: {e}")
            raise e

    async def disconnect(self) -> None:
        for producer in self._producers.values():
            await anyio.to_thread.run_sync(producer.close)
        for consumer in self._consumers.values():
            await anyio.to_thread.run_sync(consumer.close)
        if self._client:
            await anyio.to_thread.run_sync(self._client.close)

    async def subscribe(self, channel: str) -> None:
        if channel not in self._subscribed_channels:
            self._subscribed_channels.add(channel)
            consumer = await anyio.to_thread.run_sync(
                lambda: self._client.subscribe(
                    channel,
                    subscription_name=f"broadcast_subscription_{channel}",
                    consumer_type=pulsar.ConsumerType.Shared,
                )
            )
            self._consumers[channel] = consumer
            logger.info(f"Subscribed to channel: {channel}")

    async def unsubscribe(self, channel: str) -> None:
        if channel in self._subscribed_channels:
            self._subscribed_channels.remove(channel)
            consumer = self._consumers.pop(channel, None)
            if consumer:
                await anyio.to_thread.run_sync(consumer.close)
            logger.info(f"Unsubscribed from channel: {channel}")

    async def publish(self, channel: str, message: typing.Any) -> None:
        if channel not in self._producers:
            self._producers[channel] = await anyio.to_thread.run_sync(
                lambda: self._client.create_producer(channel)
            )
        encoded_message = str(message).encode("utf-8")
        await anyio.to_thread.run_sync(lambda: self._producers[channel].send(encoded_message))
        logger.info(f"Published message to channel {channel}: {message}")

    async def next_published(self) -> Event:
        while True:
            if not self._consumers:
                await anyio.sleep(0.1)  # Wait a bit before checking again
                continue
            
            for channel, consumer in self._consumers.items():
                try:
                    msg = await anyio.to_thread.run_sync(
                        lambda: consumer.receive(timeout_millis=100)
                    )
                    if msg:
                        content = msg.data().decode("utf-8")
                        await anyio.to_thread.run_sync(consumer.acknowledge, msg)
                        logger.info(f"Received message from channel {channel}: {content}")
                        return Event(channel=channel, message=content)
                except pulsar.Timeout:
                    # No message received, continue to next consumer
                    continue
                except Exception as e:
                    logger.error(f"Error receiving message from channel {channel}: {e}")

            # If we've checked all consumers and found no messages, wait a bit before the next iteration
            await anyio.sleep(0.1)
