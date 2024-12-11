import asyncio
import logging
import typing
from urllib.parse import urlparse
import pulsar
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
            self._client = await asyncio.to_thread(pulsar.Client, self._service_url)
            logger.info("Successfully connected to Pulsar brokers")
        except Exception as e:
            logger.error(f"Error connecting to Pulsar: {e}", exc_info=True)
            raise

    async def disconnect(self) -> None:
        try:
            # Cancel all receiver tasks
            for task in self._receiver_tasks.values():
                task.cancel()

            try:
                await asyncio.gather(*self._receiver_tasks.values(), return_exceptions=True)
            except Exception as e:
                logger.error("Error during receiver tasks cleanup: %s", e, exc_info=True)

            # Close producers and consumers first
            close_coros = [
                asyncio.to_thread(producer.close)
                for producer in self._producers.values()
            ] + [
                asyncio.to_thread(consumer.close)
                for consumer in self._consumers.values()
            ]

            try:
                await asyncio.gather(*close_coros, return_exceptions=True)
            except Exception as e:
                logger.error("Error closing producers/consumers: %s", e, exc_info=True)

            # Close client after producers/consumers
            if self._client:
                try:
                    await asyncio.to_thread(self._client.close)
                except Exception as e:
                    logger.error("Error closing Pulsar client: %s", e, exc_info=True)

            self._producers.clear()
            self._consumers.clear()
            self._receiver_tasks.clear()
            self._client = None

            logger.info("Disconnected from Pulsar")
        except Exception as e:
            logger.error("Unexpected error during disconnect: %s", e, exc_info=True)
            raise

    async def subscribe(self, channel: str) -> None:
        if channel not in self._consumers:
            try:
                consumer = await asyncio.to_thread(
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
                # Clean up any partially created resources
                if channel in self._consumers:
                    try:
                        await asyncio.to_thread(self._consumers[channel].close)
                        del self._consumers[channel]
                    except Exception as cleanup_error:
                        logger.error(f"Error during subscription cleanup: {cleanup_error}", exc_info=True)
                raise

    async def unsubscribe(self, channel: str) -> None:
        # First check if the channel exists in our consumers
        if channel not in self._consumers:
            logger.warning(f"Attempted to unsubscribe from channel {channel} which was not subscribed")
            return

        # Get the consumer and remove it from the dict
        consumer = self._consumers.pop(channel, None)
        
        # Check if we actually got a consumer object
        if consumer is None:
            logger.warning(f"Consumer for channel {channel} was not in the client's list")
            return

        try:
            # Cancel and wait for the receiver task first
            if channel in self._receiver_tasks:
                logger.info(f"Stopped consuming messages from channel {channel}, closing consumer...")
                task = self._receiver_tasks.pop(channel)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"Error cancelling receiver task for channel {channel}: {e}", exc_info=True)

            # Then close the consumer
            await asyncio.to_thread(consumer.close)
            logger.info(f"Unsubscribed from channel: {channel}")
        except Exception as e:
            logger.error(f"Error during unsubscribe from channel {channel}: {e}", exc_info=True)
            raise

    async def publish(self, channel: str, message: typing.Any) -> None:
        try:
            if channel not in self._producers:
                self._producers[channel] = await asyncio.to_thread(
                    lambda: self._client.create_producer(channel)
                )
            encoded_message = str(message).encode("utf-8")
            await asyncio.to_thread(self._producers[channel].send, encoded_message)
            logger.debug(f"Published message to channel {channel}: {message}")
        except Exception as e:
            logger.error(f"Error publishing to channel {channel}: {e}", exc_info=True)
            # Clean up failed producer
            if channel in self._producers:
                try:
                    await asyncio.to_thread(self._producers[channel].close)
                    del self._producers[channel]
                except Exception as cleanup_error:
                    logger.error(f"Error cleaning up failed producer: {cleanup_error}", exc_info=True)
            raise

    async def next_published(self) -> Event:
        try:
            return await self._shared_queue.get()
        except Exception as e:
            logger.error("Error getting next published message: %s", e, exc_info=True)
            raise

    async def _receiver(self, channel: str, consumer: pulsar.Consumer) -> None:
        try:
            while True:
                try:
                    msg = await asyncio.to_thread(consumer.receive)
                    content = msg.data().decode("utf-8")
                    await asyncio.to_thread(consumer.acknowledge, msg)
                    await self._shared_queue.put(Event(channel=channel, message=content))
                    logger.debug(f"Received message from channel {channel}: {content}")
                except asyncio.CancelledError:
                    logger.info(f"Receiver for channel {channel} was cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error receiving message from channel {channel}: {e}", exc_info=True)
                    # Add a small delay before retrying to avoid tight loop on persistent errors
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info(f"Receiver task for channel {channel} was cancelled")
        except Exception as e:
            logger.error(f"Fatal error in receiver for channel {channel}: {e}", exc_info=True)
        finally:
            try:
                await asyncio.to_thread(consumer.close)
            except Exception as e:
                logger.error(f"Error closing consumer in receiver cleanup for channel {channel}: {e}", exc_info=True)
                