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
        """Initialize PulsarBackend with connection settings.
        
        Args:
            url: Pulsar connection URL
            max_queue_size: Maximum size of the shared message queue
        """
        parsed_url = urlparse(url)
        self._host = parsed_url.hostname or "localhost"
        self._port = parsed_url.port or 6650
        self._service_url = f"pulsar://{self._host}:{self._port}"
        self._client = None
        self._producers: dict = {}
        self._consumers: dict = {}
        self._receiver_tasks: dict = {}
        self._shared_queue = asyncio.Queue(maxsize=max_queue_size)
        self._connected = False

    async def connect(self) -> None:
        """Establish connection to Pulsar broker."""
        if self._connected:
            logger.warning("Already connected to Pulsar")
            return

        try:
            logger.info("Connecting to Pulsar broker at %s", self._service_url)
            self._client = await asyncio.to_thread(pulsar.Client, self._service_url)
            self._connected = True
            logger.info("Successfully connected to Pulsar broker")
        except Exception as e:
            self._client = None
            logger.error("Failed to connect to Pulsar broker", exc_info=e)
            raise

    async def disconnect(self) -> None:
        """Disconnect from Pulsar, ensuring clean shutdown of all resources."""
        if not self._connected:
            logger.warning("Already disconnected from Pulsar")
            return

        logger.info("Starting Pulsar disconnect sequence")
        
        # Cancel and cleanup receiver tasks
        if self._receiver_tasks:
            logger.info("Cancelling receiver tasks...")
            for channel, task in self._receiver_tasks.items():
                task.cancel()
            
            try:
                await asyncio.gather(*self._receiver_tasks.values())
            except asyncio.CancelledError:
                logger.info("Receiver tasks cancelled successfully")
            except Exception as e:
                logger.error("Error during receiver tasks cleanup", exc_info=e)

        # Close producers
        if self._producers:
            logger.info("Closing producers...")
            for channel, producer in self._producers.items():
                try:
                    await asyncio.to_thread(producer.close)
                except Exception as e:
                    logger.error(f"Failed to close producer for channel {channel}", exc_info=e)

        # Close consumers
        if self._consumers:
            logger.info("Closing consumers...")
            for channel, consumer in self._consumers.items():
                try:
                    await asyncio.to_thread(consumer.close)
                except Exception as e:
                    logger.error(f"Failed to close consumer for channel {channel}", exc_info=e)

        # Finally close the client
        if self._client:
            logger.info("Closing Pulsar client...")
            try:
                await asyncio.to_thread(self._client.close)
            except Exception as e:
                logger.error("Failed to close Pulsar client", exc_info=e)

        # Clear all internal state
        self._producers.clear()
        self._consumers.clear()
        self._receiver_tasks.clear()
        self._client = None
        self._connected = False

        logger.info("Pulsar disconnect sequence completed")

    async def subscribe(self, channel: str) -> None:
        """Subscribe to a Pulsar topic/channel.
        
        Args:
            channel: The channel/topic name to subscribe to
        """
        if not self._connected:
            raise RuntimeError("Not connected to Pulsar broker")

        if channel in self._consumers:
            logger.warning(f"Already subscribed to channel: {channel}")
            return

        try:
            consumer = await asyncio.to_thread(
                lambda: self._client.subscribe(
                    channel,
                    subscription_name=f"broadcast_subscription_{channel}",
                    consumer_type=pulsar.ConsumerType.Shared,
                )
            )
            
            self._consumers[channel] = consumer
            
            # Create and store receiver task
            receiver_task = asyncio.create_task(
                self._receiver(channel, consumer),
                name=f"pulsar_receiver_{channel}"
            )
            self._receiver_tasks[channel] = receiver_task
            
            logger.info(f"Successfully subscribed to channel: {channel}")
            
        except Exception as e:
            logger.error(f"Failed to subscribe to channel {channel}", exc_info=e)
            # Cleanup any partially created resources
            await self._cleanup_subscription(channel)
            raise

    async def unsubscribe(self, channel: str) -> None:
        """Unsubscribe from a Pulsar topic/channel.
        
        Args:
            channel: The channel/topic to unsubscribe from
        """
        if not self._connected:
            logger.warning("Not connected to Pulsar broker")
            return

        if channel not in self._consumers:
            logger.warning(f"Not subscribed to channel: {channel}")
            return

        await self._cleanup_subscription(channel)
        logger.info(f"Successfully unsubscribed from channel: {channel}")

    async def publish(self, channel: str, message: typing.Any) -> None:
        """Publish a message to a Pulsar topic/channel.
        
        Args:
            channel: The channel/topic to publish to
            message: The message to publish
        """
        if not self._connected:
            raise RuntimeError("Not connected to Pulsar broker")

        try:
            # Get or create producer
            if channel not in self._producers:
                self._producers[channel] = await asyncio.to_thread(
                    lambda: self._client.create_producer(channel)
                )

            # Encode and send message
            encoded_message = str(message).encode("utf-8")
            await asyncio.to_thread(self._producers[channel].send, encoded_message)
            logger.debug(f"Published message to channel {channel}: {message}")
            
        except Exception as e:
            logger.error(f"Failed to publish to channel {channel}", exc_info=e)
            # Cleanup failed producer
            if channel in self._producers:
                try:
                    await asyncio.to_thread(self._producers[channel].close)
                    del self._producers[channel]
                except Exception as cleanup_e:
                    logger.error(f"Failed to cleanup producer for channel {channel}", exc_info=cleanup_e)
            raise

    async def next_published(self) -> Event:
        """Get the next published message from any subscribed channel."""
        if not self._connected:
            raise RuntimeError("Not connected to Pulsar broker")

        try:
            return await self._shared_queue.get()
        except Exception as e:
            logger.error("Failed to get next published message", exc_info=e)
            raise

    async def _cleanup_subscription(self, channel: str) -> None:
        """Clean up subscription resources for a channel.
        
        Args:
            channel: The channel to cleanup
        """
        # Cancel and cleanup receiver task
        if channel in self._receiver_tasks:
            task = self._receiver_tasks.pop(channel)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error cancelling receiver task for channel {channel}", exc_info=e)

        # Close and cleanup consumer
        if channel in self._consumers:
            consumer = self._consumers.pop(channel)
            try:
                await asyncio.to_thread(consumer.close)
            except Exception as e:
                logger.error(f"Error closing consumer for channel {channel}", exc_info=e)

    async def _receiver(self, channel: str, consumer: pulsar.Consumer) -> None:
        """Background task to receive messages from a Pulsar topic/channel.
        
        Args:
            channel: The channel being received from
            consumer: The Pulsar consumer instance
        """
        try:
            while True:
                try:
                    # Receive message
                    msg = await asyncio.to_thread(consumer.receive)
                    content = msg.data().decode("utf-8")
                    
                    # Acknowledge message
                    await asyncio.to_thread(consumer.acknowledge, msg)
                    
                    # Put message in shared queue
                    event = Event(channel=channel, message=content)
                    await self._shared_queue.put(event)
                    
                    logger.debug(f"Received message from channel {channel}: {content}")
                    
                except asyncio.CancelledError:
                    logger.info(f"Receiver for channel {channel} was cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error receiving message from channel {channel}", exc_info=e)
                    await asyncio.sleep(1)  # Backoff on error
                    
        except asyncio.CancelledError:
            logger.info(f"Receiver task for channel {channel} was cancelled")
        except Exception as e:
            logger.error(f"Fatal error in receiver for channel {channel}", exc_info=e)
        finally:
            try:
                await asyncio.to_thread(consumer.close)
            except Exception as e:
                logger.error(f"Error closing consumer in receiver cleanup for {channel}", exc_info=e)