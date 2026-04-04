"""Handler for the ``archive`` memory action.

Atelier publishes an ``archive`` request to ``relais:memory:request`` after
each completed agent turn.  This handler reconstructs the outgoing Envelope
and persists the full turn to SQLite via ``LongTermStore.archive``.

The payload schema::

    {
        "action": "archive",
        "envelope_json": "<Envelope.to_json() of the outgoing response>",
        "messages_raw": [...]   # serialized LangChain messages for this turn
    }

No response is published — archival is fire-and-forget.
"""

import logging

from common.envelope import Envelope
from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger(__name__)


class ArchiveHandler(BaseActionHandler):
    """Archive a completed agent turn to the long-term SQLite store."""

    async def handle(self, ctx: HandlerContext) -> None:
        """Persist the turn to SQLite.

        Args:
            ctx: Handler context with ``req``, ``long_term_store``,
                ``redis_conn``, and ``stream_res``.
        """
        req = ctx.req
        envelope_json: str = req.get("envelope_json", "")
        messages_raw: list[dict] = req.get("messages_raw") or []

        if not envelope_json:
            logger.warning("archive action missing envelope_json, skipping")
            return

        envelope = Envelope.from_json(envelope_json)
        await ctx.long_term_store.archive(envelope, messages_raw)
        logger.debug(
            "archive ok correlation=%s session=%s",
            envelope.correlation_id,
            envelope.session_id,
        )
