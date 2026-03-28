"""Event publisher for RELAIS monitoring via Redis Pub/Sub.

Provides fire-and-forget emission of system events on the
``relais:events:{event_type}`` channel family.
"""
import json
import logging
import time

import redis.asyncio as redis

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "relais:events"


class EventPublisher:
    """Publishes monitoring events to Redis Pub/Sub channels.

    Events are fire-and-forget: no acknowledgement or persistence
    is guaranteed. Subscribers (e.g. Le Scrutateur) consume them
    in real time.

    Example::

        publisher = EventPublisher(redis_conn)
        await publisher.emit("task_received", {"session_id": "abc", "brick": "atelier"})
    """

    def __init__(self, redis: redis.Redis) -> None:
        """Initializes the EventPublisher.

        Args:
            redis: An active async Redis connection with Pub/Sub permissions.
        """
        self._redis = redis

    async def emit(self, event_type: str, data: dict) -> None:
        """Publishes an event to the ``relais:events:{event_type}`` channel.

        A ``timestamp`` field (Unix epoch float) is automatically injected
        into ``data`` if not already present, so callers do not need to
        supply it manually.

        Args:
            event_type: Logical event category (e.g. ``"task_received"``,
                        ``"llm_error"``, ``"session_started"``).
            data: Arbitrary JSON-serialisable payload for the event.
                  Must not contain a ``"timestamp"`` key — it will be
                  overwritten.
        """
        channel = f"{CHANNEL_PREFIX}:{event_type}"

        payload = {**data, "timestamp": time.time(), "event_type": event_type}
        message = json.dumps(payload)

        try:
            await self._redis.publish(channel, message)
            logger.debug("Event emitted on '%s': %s", channel, message)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to publish event on '%s': %s", channel, exc
            )
