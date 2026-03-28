"""Redis Streams abstraction for RELAIS bricks.

Factors out the XREADGROUP / XACK / XADD boilerplate that is repeated
across every consumer brick (Portail, Sentinelle, Atelier, Souvenir,
Archiviste).
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Type alias for the message callback used by StreamConsumer.consume()
MessageCallback = Callable[[str, dict[str, str]], Awaitable[None]]


class StreamConsumer:
    """Wraps the Redis Streams consumer group pattern (XREADGROUP / XACK).

    Example::

        consumer = StreamConsumer(redis_conn, "relais:tasks", "atelier_group", "atelier_1")
        await consumer.create_group()
        await consumer.consume(handle_message)
    """

    def __init__(
        self,
        redis: redis.Redis,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int = 5000,
    ) -> None:
        """Initializes the StreamConsumer.

        Args:
            redis: An active async Redis connection.
            stream: Redis stream key to read from.
            group: Consumer group name.
            consumer: Unique consumer name within the group.
            block_ms: Milliseconds to block on XREADGROUP. Defaults to 5000.
        """
        self._redis = redis
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.block_ms = block_ms

    async def create_group(self, mkstream: bool = True) -> None:
        """Creates the consumer group, ignoring BUSYGROUP if it already exists.

        Args:
            mkstream: If True, create the stream key if it does not exist.
        """
        try:
            await self._redis.xgroup_create(
                self.stream, self.group, id="0", mkstream=mkstream
            )
            logger.debug("Consumer group '%s' created on stream '%s'", self.group, self.stream)
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                logger.debug(
                    "Consumer group '%s' already exists on '%s'", self.group, self.stream
                )
            else:
                logger.warning("Unexpected error creating consumer group: %s", exc)
                raise

    async def ack(self, msg_id: str) -> None:
        """Acknowledges a message so it is removed from the Pending Entry List.

        Args:
            msg_id: The Redis stream message ID to acknowledge.
        """
        await self._redis.xack(self.stream, self.group, msg_id)
        logger.debug("ACKed message %s on stream '%s'", msg_id, self.stream)

    async def consume(self, callback: MessageCallback) -> None:
        """Runs the XREADGROUP read loop, calling callback for each message.

        The loop runs indefinitely until the task is cancelled. The callback
        is responsible for calling ``await consumer.ack(msg_id)`` after
        successful processing — this method never auto-acks.

        Args:
            callback: Async callable ``(msg_id: str, data: dict) -> None``.
                      Receives the stream message ID and the field/value dict.

        Raises:
            asyncio.CancelledError: Propagated cleanly when the task is cancelled.
        """
        logger.info(
            "Consumer '%s' starting on stream '%s' (group='%s')",
            self.consumer,
            self.stream,
            self.group,
        )
        while True:
            try:
                results: list[Any] = await self._redis.xreadgroup(
                    self.group,
                    self.consumer,
                    {self.stream: ">"},
                    count=10,
                    block=self.block_ms,
                )
            except asyncio.CancelledError:
                logger.info("Consumer '%s' cancelled — exiting loop", self.consumer)
                raise

            if not results:
                continue

            for _, messages in results:
                for msg_id, data in messages:
                    try:
                        await callback(msg_id, data)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "Unhandled error in message callback (msg_id=%s): %s",
                            msg_id,
                            exc,
                            exc_info=True,
                        )


class StreamProducer:
    """Wraps the Redis Streams producer pattern (XADD).

    Example::

        producer = StreamProducer(redis_conn, "relais:tasks")
        msg_id = await producer.publish({"payload": envelope.to_json()})
    """

    def __init__(self, redis: redis.Redis, stream: str) -> None:
        """Initializes the StreamProducer.

        Args:
            redis: An active async Redis connection.
            stream: Redis stream key to publish to.
        """
        self._redis = redis
        self.stream = stream

    async def publish(self, data: dict[str, str]) -> str:
        """Appends a message to the stream.

        Args:
            data: Field/value dictionary to store in the stream entry.

        Returns:
            The Redis stream message ID assigned to the new entry.
        """
        msg_id: str = await self._redis.xadd(self.stream, data)
        logger.debug("Published message %s to stream '%s'", msg_id, self.stream)
        return msg_id
