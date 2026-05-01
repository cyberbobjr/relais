"""StreamingMixin — shared XREAD consumer logic for NativeAiguilleur subclasses.

Provides the two methods that were copy-pasted verbatim across Discord and WhatsApp:
- ``subscribe_streaming_start``: Pub/Sub listener that spawns a consumer task per signal.
- ``_consume_stream``: XREAD loop that buffers tokens and calls ``_deliver`` on is_final.

Concrete adapters inherit this mixin alongside ``NativeAiguilleur`` and implement
the single abstract method ``_deliver(envelope, full_text)`` for channel-specific
delivery (e.g. Discord ``channel.send`` or WhatsApp ``_send_message``).

Usage::

    class MyAiguilleur(StreamingMixin, NativeAiguilleur):
        async def _deliver(self, envelope: Envelope, full_text: str) -> None:
            await self._my_channel_send(full_text)

        async def run(self) -> None:
            ...
            await asyncio.gather(
                self._consume_outgoing(),
                self.subscribe_streaming_start(
                    self._redis, "mychannel", self._streaming_tasks, self._log
                ),
            )
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from common.envelope import Envelope
from common.streams import pubsub_streaming_start, stream_streaming

_STREAM_TIMEOUT_SECONDS: float = 300.0
_XREAD_COUNT: int = 50
_XREAD_BLOCK_MAX_MS: int = 5000


class StreamingMixin:
    """Mixin providing shared streaming consumer logic for NativeAiguilleur subclasses.

    Subclasses must implement ``_deliver``.
    """

    async def subscribe_streaming_start(
        self,
        redis_conn: Any,
        channel_name: str,
        streaming_tasks: set[asyncio.Task[Any]],
        log: logging.Logger,
    ) -> None:
        """Subscribe to the Pub/Sub streaming-start channel and spawn consumer tasks.

        Listens on ``relais:streaming:start:{channel_name}``. For each signal,
        parses the envelope and spawns a ``_consume_stream`` task that reads
        tokens until ``is_final=1`` then calls ``_deliver``.

        Args:
            redis_conn:      Async Redis connection for this adapter.
            channel_name:    Channel identifier (e.g. "discord", "whatsapp").
            streaming_tasks: The adapter's live-task set for lifecycle tracking.
            log:             Logger for this adapter.
        """
        pubsub = redis_conn.pubsub()
        await pubsub.subscribe(pubsub_streaming_start(channel_name))
        log.info("Subscribed to %s", pubsub_streaming_start(channel_name))

        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    data = message.get("data", b"")
                    if isinstance(data, bytes):
                        data = data.decode()
                    envelope = Envelope.from_json(data)
                except Exception as exc:
                    log.error("Failed to parse streaming start envelope: %s", exc)
                    continue

                task = asyncio.get_running_loop().create_task(
                    self._consume_stream(redis_conn, channel_name, envelope, log)
                )
                streaming_tasks.add(task)
                task.add_done_callback(streaming_tasks.discard)
        except Exception as exc:
            log.error("Streaming start subscriber error: %s", exc)

    async def _consume_stream(
        self,
        redis_conn: Any,
        channel_name: str,
        envelope: Envelope,
        log: logging.Logger,
        timeout: float = _STREAM_TIMEOUT_SECONDS,
    ) -> None:
        """XREAD loop: buffer tokens until is_final=1, then call _deliver.

        Args:
            redis_conn:   Async Redis connection for this adapter.
            channel_name: Channel identifier (e.g. "discord", "whatsapp").
            envelope:     Original request envelope (corr_id, reply metadata).
            log:          Logger for this adapter.
            timeout:      Max seconds to wait for the stream to complete.
        """
        corr_id = envelope.correlation_id
        stream_key = stream_streaming(channel_name, corr_id)
        last_id = "0-0"
        buffer: list[str] = []
        deadline = asyncio.get_running_loop().time() + timeout

        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    log.warning("Streaming reply timed out for %s", corr_id[:8])
                    return

                results = await redis_conn.xread(
                    streams={stream_key: last_id},
                    count=_XREAD_COUNT,
                    block=min(_XREAD_BLOCK_MAX_MS, int(remaining * 1000)),
                )
                if not results:
                    continue

                for _, entries in results:
                    for entry_id, fields in entries:
                        last_id = entry_id
                        chunk = fields.get("chunk", "")
                        is_final = fields.get("is_final", "0") == "1"

                        if chunk:
                            buffer.append(chunk)

                        if is_final:
                            await self._deliver(envelope, "".join(buffer))
                            return
        except Exception as exc:
            log.error("Streaming reply consumer error for %s: %s", corr_id[:8], exc)

    async def _deliver(self, envelope: Envelope, full_text: str) -> None:
        """Channel-specific delivery of the fully assembled reply.

        Called once when is_final=1 is received. Subclasses override this
        to send the assembled text to the appropriate channel.

        Args:
            envelope:  Original request envelope.
            full_text: Fully assembled reply text.

        Raises:
            NotImplementedError: Must be overridden by concrete subclasses.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement _deliver()")
