import anyio
import asyncio
import logging
import typing
from urllib.parse import urlparse
import pulsar
import traceback
from broadcaster._base import Event
from .base import BroadcastBackend

logger = logging.getLogger(__name__)

class PulsarBackend(BroadcastBackend):
    def __init__(self, url: str, max_queue_size: int = 1000):
        parsed_url = urlparse(url)
        self._host = parsed_url.hostname or "localhost"
        self._port = parsed_url.port or 6650
        self._service_url = f"pulsar://{self._host}:{self._port}"
        self._client = None
        self._producers = {}
        self._consumers = {}
        self._receiver_tasks = {}
        self._shared_queue = asyncio.Queue(maxsize=max_queue_size)

    async def connect(self) -> None:
        try:
            logger.info("Connecting to Pulsar brokers")
            self._client = await anyio.to_thread.run_sync(
                lambda: pulsar.Client(self._service_url)
            )
            logger.info("Successfully connected to Pulsar brokers")
        except Exception as e:
            logger.error(f"Error connecting to Pulsar: {e}", exc_info=True)
            raise e

    async def disconnect(self) -> None:
        # Cancel all receiver tasks
        for task in self._receiver_tasks.values():
            task.cancel()
        

        await asyncio.gather(*self._receiver_tasks.values(), return_exceptions=True)
        
        # Close producers and consumers first
        close_coros = [
            anyio.to_thread.run_sync(producer.close)
            for producer in self._producers.values()
        ] + [
            anyio.to_thread.run_sync(consumer.close)
            for consumer in self._consumers.values()
        ]



        await asyncio.gather(*close_coros, return_exceptions=True)
        
        # Close client after producers/consumers
        if self._client:
            await anyio.to_thread.run_sync(self._client.close)
        self._producers.clear()
        self._consumers.clear()
        self._receiver_tasks.clear()
        self._client = None
        
        logger.info("Disconnected from Pulsar")

    async def _safe_close(self, obj: typing.Any, description: str) -> None:
        """Helper method to safely close Pulsar objects with error logging"""
        try:
            await anyio.to_thread.run_sync(obj.close)
            logger.debug(f"Successfully closed {description}")
        except Exception as e:
            logger.error(f"Error closing {description}: {e}", exc_info=True)
            raise

    async def subscribe(self, channel: str) -> None:
        if channel not in self._consumers:
            try:
                consumer = await anyio.to_thread.run_sync(
                    lambda: self._client.subscribe(
                        channel,
                        subscription_name=f"broadcast_subscription_{channel}",
                        consumer_type=pulsar.ConsumerType.Shared,
                    )
                )
                self._consumers[channel] = consumer
                self._receiver_tasks[channel] = asyncio.create_task(self._receiver(channel, consumer))
                logger.info(f"Subscribed to channel: {channel}")
            except Exception as e:
                logger.error(f"Error subscribing to channel {channel}: {e}", exc_info=True)
                raise

    async def unsubscribe(self, channel: str) -> None:
        if channel not in self._consumers:
            logger.warning(f"Attempted to unsubscribe from channel {channel} which was not subscribed")
            return
            
        consumer = self._consumers.pop(channel)

        try:
            await anyio.to_thread.run_sync(consumer.close)
        except ValueError:
            logger.warning(f"Consumer for channel {channel} was not in the client's list")
        except Exception as e:
            logger.error(f"Error closing consumer for channel {channel}: {e}", exc_info=True)
        else:
            logger.info(f"Unsubscribed from channel: {channel}")

    async def publish(self, channel: str, message: typing.Any) -> None:
        try:
            if channel not in self._producers:
                self._producers[channel] = await anyio.to_thread.run_sync(
                    lambda: self._client.create_producer(channel)
                )
            encoded_message = str(message).encode("utf-8")
            await anyio.to_thread.run_sync(lambda: self._producers[channel].send(encoded_message))
            logger.debug(f"Published message to channel {channel}: {message}")
        except Exception as e:
            logger.error(f"Error publishing to channel {channel}: {e}", exc_info=True)
            raise

    async def next_published(self) -> Event:
        return await self._shared_queue.get()

    async def _receiver(self, channel: str, consumer: pulsar.Consumer) -> None:
        try:
            while True:
                try:
                    msg = await anyio.to_thread.run_sync(consumer.receive)
                    content = msg.data().decode("utf-8")
                    await anyio.to_thread.run_sync(consumer.acknowledge, msg)
                    await self._shared_queue.put(Event(channel=channel, message=content))
                    logger.debug(f"Received message from channel {channel}: {content}")
                except asyncio.CancelledError:
                    logger.info(f"Receiver for channel {channel} was cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error receiving message from channel {channel}: {e}", exc_info=True)
        finally:
            try:
                await anyio.to_thread.run_sync(consumer.close)
            except Exception as e:
                logger.error(f"Error closing consumer in receiver cleanup for channel {channel}: {e}", exc_info=True)