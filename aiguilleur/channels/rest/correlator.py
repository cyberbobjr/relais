"""ResponseCorrelator — maps correlation IDs to asyncio Futures.

The REST adapter uses this to bridge the synchronous HTTP request with the
asynchronous outgoing Redis stream. Each POST /v1/messages call registers a
Future; when the outgoing consumer picks up the reply envelope it resolves
the Future so the HTTP handler can return the result.

Thread-safety: protected by asyncio.Lock (all operations run in the same
event loop). Do NOT call from a separate thread.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from common.envelope import Envelope

logger = logging.getLogger("aiguilleur.rest.correlator")


class ResponseCorrelator:
    """Registry of pending correlation_id → asyncio.Future mappings.

    Lifecycle of a correlation:
    1. ``register(corr_id)`` — called by the HTTP handler before publishing.
    2. ``resolve(corr_id, envelope)`` — called by the outgoing consumer on delivery.
    3. ``cancel(corr_id)`` — called in ``finally`` by the HTTP handler on timeout
       or client disconnect to free the entry and cancel the Future.

    If ``resolve`` is called for an unknown ``corr_id`` (e.g. because the HTTP
    handler already timed out and called ``cancel``), it logs at DEBUG level only
    and does nothing. This avoids spurious exceptions for orphan envelopes.
    """

    def __init__(self) -> None:
        """Initialise an empty correlator."""
        self._pending: dict[str, asyncio.Future] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def register(self, corr_id: str) -> asyncio.Future:
        """Create and register a new Future for the given correlation ID.

        Must be called before publishing the envelope to Redis so that
        ``resolve`` cannot race with the registration.

        Args:
            corr_id: Correlation ID from the Envelope.

        Returns:
            The newly created asyncio.Future. The caller awaits this Future.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            self._pending[corr_id] = future
            logger.debug("Registered correlation %s", corr_id)
            return future

    async def resolve(self, corr_id: str, envelope: "Envelope") -> None:
        """Resolve the Future for *corr_id* with *envelope*.

        If *corr_id* is not in the registry (orphan — handler already timed
        out or was cancelled), logs at DEBUG level and returns silently. This
        is the expected path after a timeout.

        Args:
            corr_id: Correlation ID to resolve.
            envelope: The outgoing Envelope received from the Redis bus.
        """
        async with self._lock:
            future = self._pending.pop(corr_id, None)
        if future is None:
            logger.debug(
                "resolve() called for unknown corr_id %s (orphan after timeout)",
                corr_id,
            )
            return
        if not future.done():
            future.set_result(envelope)
            logger.debug("Resolved correlation %s", corr_id)

    async def cancel(self, corr_id: str) -> None:
        """Cancel and remove the Future for *corr_id*.

        Safe to call when ``corr_id`` is not registered (no-op). The caller
        (HTTP handler ``finally`` block) must always call this to prevent
        memory leaks when a request times out or the client disconnects.

        Args:
            corr_id: Correlation ID to cancel.
        """
        async with self._lock:
            future = self._pending.pop(corr_id, None)
        if future is not None and not future.done():
            future.cancel()
            logger.debug("Cancelled correlation %s", corr_id)
