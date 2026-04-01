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
            ctx: Handler context; reads ``session_id`` and optionally
                ``envelope_json`` from ``ctx.req``.
        """
        session_id: str = ctx.req.get("session_id", "")

        await ctx.context_store.clear(session_id)
        await ctx.long_term_store.clear_session(session_id)
        logger.info("Cleared context for session=%s (Redis + SQLite)", session_id)

        envelope_json: str | None = ctx.req.get("envelope_json")
        if envelope_json:
            try:
                orig = Envelope.from_json(envelope_json)
                confirmation = Envelope.from_parent(
                    orig,
                    "✓ Historique de conversation effacé.",
                )
                await ctx.redis_conn.xadd(
                    f"relais:messages:outgoing:{orig.channel}",
                    {"payload": confirmation.to_json()},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not send /clear confirmation: %s", exc)
