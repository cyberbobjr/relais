"""Handler for action ``memory.resume`` — resume a previous session."""

from __future__ import annotations

import logging

from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_OUTGOING
from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger("souvenir")


class ResumeHandler(BaseActionHandler):
    """Confirm session resumption and embed resume metadata in the response envelope.

    Reads ``target_session_id`` and ``user_id`` from ``ctx.req``.  Checks that
    the session exists in ``long_term_store``; publishes a confirmation (or
    error) message to the originating channel.

    No history is displayed — only the confirmation text is sent to the user.
    The target ``session_id`` is embedded in ``response.context["resume"]``
    so downstream bricks (Atelier) can pick it up and restore the session.
    """

    async def handle(self, ctx: HandlerContext) -> None:
        """Check session existence, publish confirmation, and embed resume context.

        Args:
            ctx: Handler context; reads ``target_session_id``, ``user_id``,
                and ``envelope_json`` from ``ctx.req``.
        """
        target_session_id: str = ctx.req.get("target_session_id", "")
        user_id: str | None = ctx.req.get("user_id") or None
        envelope_json: str | None = ctx.req.get("envelope_json")

        if not user_id:
            logger.warning("ResumeHandler: missing user_id — refusing ownership-unchecked resume")
            return

        if not envelope_json:
            logger.warning("ResumeHandler: no envelope_json in request, cannot reply")
            return

        try:
            orig = Envelope.from_json(envelope_json)
        except Exception as exc:
            logger.warning("ResumeHandler: failed to parse envelope_json: %s", exc)
            return

        # Check session existence and ownership (limit=1 for minimal I/O).
        # Passing user_id ensures a different user cannot resume another user's session.
        try:
            turns = await ctx.long_term_store.get_session_history(
                target_session_id, limit=1, user_id=user_id
            )
        except Exception as exc:
            logger.error("ResumeHandler: get_session_history failed: %s", exc)
            turns = []

        if not turns:
            text = "Session introuvable."
            response = Envelope.from_parent(orig, text)
            response.action = ACTION_MESSAGE_OUTGOING
        else:
            short_id = target_session_id[:8]
            text = f"Session {short_id}... reprise."
            response = Envelope.from_parent(orig, text)
            response.action = ACTION_MESSAGE_OUTGOING
            # Embed resume metadata for Atelier/downstream bricks
            response.context["resume"] = {"session_id": target_session_id}

        try:
            await ctx.redis_conn.xadd(
                f"relais:messages:outgoing:{orig.channel}",
                {"payload": response.to_json()},
            )
        except Exception as exc:
            logger.warning("ResumeHandler: failed to publish response: %s", exc)
