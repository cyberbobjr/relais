"""Streaming chunk publisher for the Atelier brick.

Publishes LLM response chunks to a Redis Stream so that streaming-capable
channels (Discord, Telegram, TUI) can deliver progressive responses to the
user without waiting for the full generation to complete.

Stream key format: ``relais:messages:streaming:{channel}:{correlation_id}``

Each entry contains:
    - ``chunk``: The text fragment (empty string for the final sentinel).
    - ``seq``: Monotonically increasing integer sequence number (as a string).
    - ``is_final``: ``"1"`` for the terminal sentinel entry, ``"0"`` otherwise.

The stream is capped at STREAM_MAXLEN entries (APPROX) and given a TTL of
STREAM_TTL_SECONDS seconds after finalize() is called.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class StreamPublisher:
    """Publishes LLM streaming chunks to a Redis Stream.

    Attributes:
        STREAM_TTL_SECONDS: Seconds before the stream key expires after
            finalize() is called.
        STREAM_MAXLEN: Approximate maximum number of entries kept in the
            stream (Redis XADD MAXLEN ~ trimming).
    """

    STREAM_TTL_SECONDS: int = 300
    STREAM_MAXLEN: int = 500

    def __init__(self, redis_conn: Any, channel: str, correlation_id: str) -> None:
        """Initialise the publisher for a specific channel and correlation ID.

        Args:
            redis_conn: An async Redis connection (aioredis / redis.asyncio).
            channel: The originating channel name (e.g. "discord").
            correlation_id: Unique request correlation ID used to build the
                stream key.
        """
        self._redis = redis_conn
        self._channel = channel
        self._correlation_id = correlation_id
        self._stream_key = (
            f"relais:messages:streaming:{channel}:{correlation_id}"
        )
        self._seq: int = 0

    async def push_chunk(self, chunk: str, is_final: bool = False) -> None:
        """Add a text chunk to the Redis Stream.

        Args:
            chunk: The text fragment to publish.  Pass an empty string for
                the final sentinel entry (see finalize()).
            is_final: When True the entry is marked as the stream terminator.
        """
        await self._redis.xadd(
            self._stream_key,
            {
                "chunk": chunk,
                "seq": str(self._seq),
                "is_final": "1" if is_final else "0",
            },
            maxlen=self.STREAM_MAXLEN,
        )
        self._seq += 1

    async def finalize(self) -> None:
        """Publish the terminal sentinel entry and set a TTL on the stream.

        Sends a final chunk entry with ``is_final="1"`` and an empty chunk
        string, then applies an expiry of STREAM_TTL_SECONDS so the stream
        is cleaned up automatically.
        """
        await self.push_chunk("", is_final=True)
        await self._redis.expire(self._stream_key, self.STREAM_TTL_SECONDS)
