"""Handler for action ``clear`` — erase session context from all stores."""

from __future__ import annotations

import logging

from common.envelope import Envelope
from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger("souvenir")


class ClearHandler(BaseActionHandler):
    """Clear short-term and long-term memory for a session.

    Optionally sends a confirmation envelope back to the originating channel
    when ``envelope_json`` is present in the request payload.
    """

    async def handle(self, ctx: HandlerContext) -> None:
        """Clear both stores and optionally confirm to the user.

        Args:
            ctx: Handler context; reads ``session_id``, optionally ``user_id``
                (to erase the LangGraph checkpointer thread), and optionally
                ``envelope_json`` from ``ctx.req``.
        """
        session_id: str = ctx.req.get("session_id", "")
        user_id: str | None = ctx.req.get("user_id") or None

        await ctx.long_term_store.clear_session(session_id, user_id=user_id)
        logger.info("Cleared context for session=%s user_id=%s (SQLite+checkpointer)", session_id, user_id)

        envelope_json: str | None = ctx.req.get("envelope_json")
        if envelope_json:
            try:
                orig = Envelope.from_json(envelope_json)
                confirmation = Envelope.from_parent(
                    orig,
                    "✓ Conversation history cleared.",
                )
                await ctx.redis_conn.xadd(
                    f"relais:messages:outgoing:{orig.channel}",
                    {"payload": confirmation.to_json()},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not send /clear confirmation: %s", exc)
