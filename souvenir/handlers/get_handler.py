"""Handler for action ``get`` — retrieve session context history."""

from __future__ import annotations

import json
import logging

from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger("souvenir")


class GetHandler(BaseActionHandler):
    """Return recent conversation history for a session.

    Tries the short-term Redis cache first; falls back to SQLite when the
    cache is empty.  Publishes the result on ``relais:memory:response``.
    """

    async def handle(self, ctx: HandlerContext) -> None:
        """Fetch and publish session history.

        Args:
            ctx: Handler context; reads ``session_id`` and ``correlation_id``
                from ``ctx.req``.
        """
        session_id: str = ctx.req.get("session_id", "")
        correlation_id: str | None = ctx.req.get("correlation_id")

        messages = await ctx.context_store.get_recent(session_id, limit=20)
        if not messages:
            messages = await ctx.long_term_store.get_recent_messages(session_id, limit=20)
            if messages:
                logger.debug("Redis cache miss for session %s — using SQLite", session_id)

        payload = {"correlation_id": correlation_id, "messages": messages}
        await ctx.redis_conn.xadd(ctx.stream_res, {"payload": json.dumps(payload)})
        await ctx.redis_conn.xtrim(ctx.stream_res, maxlen=500, approximate=True)
        logger.info("Provided context for session %s (%d msgs)", session_id, len(messages))
