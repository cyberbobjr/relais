"""Push registry for the REST adapter SSE push endpoint.

Manages per-user XREAD reader tasks and fans out messages to all active
SSE subscriber queues for each user.

Each user gets exactly one background reader task that does:
    XREAD BLOCK 2000 STREAMS relais:messages:outgoing:rest:{user_id} $

When a message arrives, the payload is put onto every subscriber queue.
If a queue is full (maxsize=256), the oldest item is evicted and a WARNING
is logged before adding the new item.

Reader tasks are created lazily on first subscribe and cancelled when the
last subscriber for a given user disconnects.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from common.streams import stream_outgoing_user

logger = logging.getLogger("aiguilleur.rest.push_registry")

_QUEUE_MAXSIZE = 256
_XREAD_BLOCK_MS = 2000


class PushRegistry:
    """Registry that bridges per-user Redis push streams to SSE subscriber queues.

    Attributes:
        _redis_conn: Async Redis connection used for XREAD.
        _subscribers: Mapping from user_id to the set of active subscriber queues.
        _readers: Mapping from user_id to the active reader asyncio.Task.
        _lock: Async mutex protecting _subscribers and _readers mutations.
    """

    def __init__(self, redis_conn: Any) -> None:
        """Initialise the registry.

        Args:
            redis_conn: Async Redis connection (real or mock) supporting
                ``xread(streams_dict, count, block)`` coroutine.
        """
        self._redis_conn = redis_conn
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._readers: dict[str, asyncio.Task] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def subscribe(self, user_id: str) -> asyncio.Queue:
        """Register a new subscriber queue for the given user.

        Creates a bounded asyncio.Queue (maxsize=256) and adds it to the
        subscriber set. If no reader task exists for this user, one is
        started lazily.

        Args:
            user_id: Stable user identifier (e.g. ``"usr_admin"``).

        Returns:
            A new asyncio.Queue that will receive JSON payload strings
            as messages arrive on the user's push stream.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        async with self._lock:
            if user_id not in self._subscribers:
                self._subscribers[user_id] = set()
            self._subscribers[user_id].add(queue)

            if user_id not in self._readers or self._readers[user_id].done():
                task = asyncio.create_task(
                    self._reader_loop(user_id),
                    name=f"push-reader-{user_id}",
                )
                self._readers[user_id] = task

        return queue

    async def unsubscribe(self, user_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue for the given user.

        If this was the last subscriber, the reader task for this user is
        cancelled.

        Args:
            user_id: Stable user identifier.
            queue: The exact Queue instance returned by subscribe().
        """
        async with self._lock:
            subs = self._subscribers.get(user_id)
            if subs is None:
                return
            subs.discard(queue)
            if not subs:
                del self._subscribers[user_id]
                task = self._readers.pop(user_id, None)
                if task is not None and not task.done():
                    task.cancel()

    async def close(self) -> None:
        """Cancel all active reader tasks and clear internal state.

        Safe to call multiple times.
        """
        async with self._lock:
            tasks = list(self._readers.values())
            self._readers.clear()
            self._subscribers.clear()

        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _reader_loop(self, user_id: str) -> None:
        """Background task: read Redis push stream and fan out to subscribers.

        Runs until cancelled (when the last subscriber disconnects or on
        registry close). Reads from
        ``relais:messages:outgoing:rest:{user_id}`` starting at ``$``
        (only new messages).

        Args:
            user_id: Stable user identifier whose push stream to read.
        """
        stream_name = stream_outgoing_user("rest", user_id)
        last_id = "$"
        logger.debug("Push reader started for user=%s stream=%s", user_id, stream_name)

        try:
            while True:
                try:
                    results = await self._redis_conn.xread(
                        {stream_name: last_id},
                        count=10,
                        block=_XREAD_BLOCK_MS,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("Push reader xread error user=%s: %s", user_id, exc)
                    await asyncio.sleep(1)
                    continue

                if not results:
                    continue

                for _, messages in results:
                    for msg_id, data in messages:
                        last_id = msg_id
                        payload = data.get(b"payload") or data.get("payload") or b""
                        if isinstance(payload, bytes):
                            payload = payload.decode()
                        if not payload:
                            continue
                        await self._fanout(user_id, payload)

        except asyncio.CancelledError:
            logger.debug("Push reader cancelled for user=%s", user_id)
            raise

    async def _fanout(self, user_id: str, payload: str) -> None:
        """Deliver payload to all subscriber queues for a user.

        If a queue is full (maxsize reached), the oldest item is evicted
        via get_nowait() and a WARNING is logged before inserting the new
        payload.

        Args:
            user_id: Stable user identifier.
            payload: JSON string payload to deliver.
        """
        async with self._lock:
            queues = tuple(self._subscribers.get(user_id, ()))

        for queue in queues:
            if queue.full():
                try:
                    evicted = queue.get_nowait()
                    logger.warning(
                        "Push queue full for user=%s — evicted oldest item: %.80s",
                        user_id,
                        evicted,
                    )
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning(
                    "Push queue still full after eviction for user=%s — dropping payload",
                    user_id,
                )
