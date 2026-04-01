"""Handler for action ``store_memory`` — persist a fact in long-term store."""

from __future__ import annotations

import logging

from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger("souvenir")


class StoreMemoryHandler(BaseActionHandler):
    """Persist a key/value fact in the long-term SQLite store.

    Reads ``user_id``, ``key``, ``value``, and ``source`` from the request
    payload and delegates storage to ``long_term_store.store()``.
    """

    async def handle(self, ctx: HandlerContext) -> None:
        """Store a user fact in the long-term store.

        Args:
            ctx: Handler context; reads ``user_id``, ``key``, ``value``, and
                ``source`` from ``ctx.req``.  Falls back to ``session_id`` for
                ``user_id`` and ``"manual"`` for ``source`` when absent.
        """
        session_id: str = ctx.req.get("session_id", "")
        user_id: str = ctx.req.get("user_id", session_id)
        key: str = ctx.req.get("key", "")
        value: str = ctx.req.get("value", "")
        source: str = ctx.req.get("source", "manual")

        await ctx.long_term_store.store(user_id, key, value, source)
        logger.info("Stored long-term memory for user=%s key=%s", user_id, key)
