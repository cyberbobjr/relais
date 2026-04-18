"""Handler for action ``memory.sessions`` — list archived sessions for a user."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_OUTGOING
from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger("souvenir")


class SessionsHandler(BaseActionHandler):
    """Return a numbered list of archived sessions for a user.

    Reads ``user_id`` from ``ctx.req``, queries ``long_term_store.list_sessions()``,
    formats a human-readable list, and publishes it directly to
    ``relais:messages:outgoing:{channel}``.
    """

    async def handle(self, ctx: HandlerContext) -> None:
        """List sessions and publish the formatted list to the origin channel.

        Args:
            ctx: Handler context; reads ``user_id`` and ``envelope_json``
                from ``ctx.req``.
        """
        user_id: str = ctx.req.get("user_id", "")
        envelope_json: str | None = ctx.req.get("envelope_json")

        if not envelope_json:
            logger.warning("SessionsHandler: no envelope_json in request, cannot reply")
            return

        try:
            orig = Envelope.from_json(envelope_json)
        except Exception as exc:
            logger.warning("SessionsHandler: failed to parse envelope_json: %s", exc)
            return

        try:
            sessions = await ctx.long_term_store.list_sessions(user_id)
        except Exception as exc:
            logger.error("SessionsHandler: list_sessions failed: %s", exc)
            sessions = []

        text = self._format(sessions)

        response = Envelope.from_parent(orig, text)
        response.action = ACTION_MESSAGE_OUTGOING

        try:
            await ctx.redis_conn.xadd(
                f"relais:messages:outgoing:{orig.channel}",
                {"payload": response.to_json()},
            )
        except Exception as exc:
            logger.warning("SessionsHandler: failed to publish response: %s", exc)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format(sessions: list[dict]) -> str:
        """Format a list of session dicts into a human-readable numbered list.

        Args:
            sessions: List of session summary dicts (from ``list_sessions()``),
                each with ``session_id``, ``last_active``, ``turn_count``,
                ``preview``.

        Returns:
            A formatted string ready to send to the user.
        """
        if not sessions:
            return "Aucune session trouvée."

        lines = ["Sessions disponibles :"]
        for i, s in enumerate(sessions, start=1):
            # Format date from epoch timestamp
            last_active: float = s.get("last_active") or 0.0
            date_str = datetime.fromtimestamp(last_active, tz=timezone.utc).strftime("%Y-%m-%d")

            turn_count: int = s.get("turn_count", 0)
            preview: str = s.get("preview", "")
            session_id: str = s.get("session_id", "")

            # Truncate preview to 40 chars for display
            preview_display = (preview[:37] + "...") if len(preview) > 40 else preview

            lines.append(
                f" {i}. [{date_str}] {turn_count} tours — \"{preview_display}\" (id: {session_id})"
            )

        lines.append("Tape /resume <session_id> pour reprendre.")
        return "\n".join(lines)
