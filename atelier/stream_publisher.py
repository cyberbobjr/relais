"""Streaming chunk publisher for the Atelier brick.

Publishes LLM response chunks to a Redis Stream so that streaming-capable
channels (Discord, Telegram, TUI) can deliver progressive responses to the
user without waiting for the full generation to complete.

Stream key format: ``relais:messages:streaming:{channel}:{correlation_id}``

Each entry contains:
    - ``type``: Entry kind — ``"token"`` for text fragments, ``"progress"`` for
      pipeline events (tool calls, subagent starts).
    - ``chunk``: The text fragment (empty string for non-token entries and the
      final sentinel).
    - ``seq``: Monotonically increasing integer sequence number (as a string).
    - ``is_final``: ``"1"`` for the terminal sentinel entry, ``"0"`` otherwise.
    - ``event``: (progress entries only) Event name, e.g. ``"tool_call"``,
      ``"tool_result"``, ``"subagent_start"``.
    - ``detail``: (progress entries only) Human-readable context string.

The stream is capped at STREAM_MAXLEN entries (APPROX) and given a TTL of
STREAM_TTL_SECONDS seconds after finalize() is called.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.envelope import Envelope
    from atelier.progress_config import ProgressConfig

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

    def __init__(
        self,
        redis_conn: Any,
        channel: str,
        correlation_id: str,
        source_envelope: "Envelope | None" = None,
        progress_config: "ProgressConfig | None" = None,
    ) -> None:
        """Initialise the publisher for a specific channel and correlation ID.

        Args:
            redis_conn: An async Redis connection (aioredis / redis.asyncio).
            channel: The originating channel name (e.g. "discord").
            correlation_id: Unique request correlation ID used to build the
                stream key.
            source_envelope: When provided, progress events are additionally
                published to ``relais:messages:outgoing:{channel}`` so that
                non-streaming adapters (e.g. Discord) can display them.
            progress_config: Optional ProgressConfig controlling which events
                are published and whether the outgoing stream is written.
                When None, all progress events are published unconditionally
                (backward-compatible behaviour).
        """
        self._redis = redis_conn
        self._channel = channel
        self._correlation_id = correlation_id
        self._stream_key = (
            f"relais:messages:streaming:{channel}:{correlation_id}"
        )
        self._outgoing_key: str | None = (
            f"relais:messages:outgoing:{channel}" if source_envelope is not None else None
        )
        self._source_envelope = source_envelope
        self._progress_config = progress_config
        self._seq: int = 0

    async def push_chunk(self, chunk: str, is_final: bool = False) -> None:
        """Add a text token chunk to the Redis Stream.

        Each entry carries ``type='token'`` so that consumers can distinguish
        token fragments from progress events published by ``push_progress()``.

        Args:
            chunk: The text fragment to publish.  Pass an empty string for
                the final sentinel entry (see finalize()).
            is_final: When True the entry is marked as the stream terminator.
        """
        await self._redis.xadd(
            self._stream_key,
            {
                "type": "token",
                "chunk": chunk,
                "seq": str(self._seq),
                "is_final": "1" if is_final else "0",
            },
            maxlen=self.STREAM_MAXLEN,
        )
        self._seq += 1

    async def push_progress(self, event: str, detail: str) -> None:
        """Publish a pipeline progress event to the Redis Stream.

        Progress events allow streaming-capable channel adapters to display
        UX feedback during long tool calls (e.g. typing indicator labels,
        progress bars) without waiting for the final LLM reply.

        When a ``ProgressConfig`` was supplied at construction time, the method
        checks the master ``enabled`` flag and the per-event flag before
        publishing.  It also truncates ``detail`` to ``detail_max_length`` and
        honours ``publish_to_outgoing``.  When no ``ProgressConfig`` is present
        the original unconditional behaviour is preserved.

        The entry uses ``is_final='0'`` and carries ``type='progress'`` so
        that consumers can filter it out of the token stream.

        Args:
            event: Short event identifier, e.g. ``'tool_call'``,
                ``'tool_result'``, or ``'subagent_start'``.
            detail: Human-readable detail string, e.g. the tool name or
                a truncated preview of the tool result.
        """
        cfg = self._progress_config

        if cfg is not None:
            # Master switch
            if not cfg.enabled:
                return
            # Per-event flag (unknown events default to True)
            if not cfg.events.get(event, True):
                return
            # Truncate detail
            detail = detail[: cfg.detail_max_length]

        await self._redis.xadd(
            self._stream_key,
            {
                "type": "progress",
                "event": event,
                "detail": detail,
                "seq": str(self._seq),
                "is_final": "0",
            },
            maxlen=self.STREAM_MAXLEN,
        )
        self._seq += 1

        # Publish to outgoing stream only if enabled (or no config — legacy path)
        publish_to_outgoing = cfg.publish_to_outgoing if cfg is not None else True
        if (
            publish_to_outgoing
            and self._outgoing_key is not None
            and self._source_envelope is not None
        ):
            await self._publish_progress_to_outgoing(event, detail)

    async def _publish_progress_to_outgoing(self, event: str, detail: str) -> None:
        """Publish a progress event envelope to the channel's outgoing stream.

        Creates a child ``Envelope`` from the source envelope with
        ``metadata["message_type"]="progress"`` so that non-streaming adapters
        (e.g. Discord) can identify and display it appropriately.

        Args:
            event: Short event identifier (e.g. ``'tool_call'``).
            detail: Human-readable detail string.
        """
        from common.envelope import Envelope
        from common.contexts import CTX_ATELIER, ensure_ctx

        assert self._source_envelope is not None  # guarded by caller
        progress_env = Envelope.from_parent(self._source_envelope, content="")
        atelier_ctx = ensure_ctx(progress_env, CTX_ATELIER)
        atelier_ctx["message_type"] = "progress"
        atelier_ctx["progress_event"] = event
        atelier_ctx["progress_detail"] = detail
        await self._redis.xadd(self._outgoing_key, {"payload": progress_env.to_json()})

    async def finalize(self) -> None:
        """Publish the terminal sentinel entry and set a TTL on the stream.

        Sends a final chunk entry with ``is_final="1"`` and an empty chunk
        string, then applies an expiry of STREAM_TTL_SECONDS so the stream
        is cleaned up automatically.
        """
        await self.push_chunk("", is_final=True)
        await self._redis.expire(self._stream_key, self.STREAM_TTL_SECONDS)
