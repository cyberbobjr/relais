"""Handler for action ``memory.history_read`` — serve full conversation history.

Forgeron (or any caller) requests the full ``messages_raw`` history for a
session.  The handler queries ``LongTermStore``, applies token-based
truncation (oldest-first), and writes the result to a temporary Redis key
for the caller to pick up via ``BRPOP``.
"""

from __future__ import annotations

import json
import logging

from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger("souvenir")

#: Approximate characters per token for budget estimation.
_CHARS_PER_TOKEN = 4

#: TTL in seconds for the Redis response key.
_RESPONSE_TTL_SECONDS = 60


class HistoryReadHandler(BaseActionHandler):
    """Return ``messages_raw`` for a session, truncated to a token budget.

    Reads ``session_id``, ``max_tokens``, and ``correlation_id`` from
    ``ctx.req``.  Queries ``LongTermStore.get_full_session_messages_raw()``
    for all turns in the session, estimates token usage (~4 chars/token),
    and drops the oldest turns until the total fits within ``max_tokens``.

    The result is ``LPUSH``-ed as a JSON list to
    ``relais:memory:response:{correlation_id}`` with a 60 s TTL so the
    caller can ``BRPOP`` it.
    """

    async def handle(self, ctx: HandlerContext) -> None:
        """Serve the full messages_raw history for a session.

        Args:
            ctx: Handler context; reads ``session_id``, ``max_tokens``,
                and ``correlation_id`` from ``ctx.req``.
        """
        session_id: str = ctx.req.get("session_id", "")
        max_tokens: int = ctx.req.get("max_tokens", 64000)
        correlation_id: str = ctx.req.get("correlation_id", "")

        if not session_id or not correlation_id:
            logger.warning(
                "HistoryReadHandler: missing session_id or correlation_id"
            )
            return

        # Fetch all turns for the session (oldest-first).
        turns = await ctx.long_term_store.get_full_session_messages_raw(session_id)

        # Truncate oldest turns until total fits within the token budget.
        truncated = _truncate_to_token_budget(turns, max_tokens)

        response_key = f"{ctx.stream_res}:{correlation_id}"
        payload = json.dumps(truncated)

        await ctx.redis_conn.lpush(response_key, payload)
        await ctx.redis_conn.expire(response_key, _RESPONSE_TTL_SECONDS)

        logger.debug(
            "HistoryReadHandler: served %d/%d turns for session=%s (key=%s)",
            len(truncated),
            len(turns),
            session_id,
            response_key,
        )


def _truncate_to_token_budget(
    turns: list[list[dict]], max_tokens: int
) -> list[list[dict]]:
    """Drop oldest turns until total estimated tokens fit within budget.

    Token estimation uses ~4 characters per token on the JSON-serialized
    representation of each turn.

    Args:
        turns: List of turns (oldest-first), each being a list of message dicts.
        max_tokens: Maximum token budget.

    Returns:
        A suffix of *turns* that fits within the budget (newest turns preserved).
    """
    if not turns:
        return []

    # Pre-compute per-turn token estimates.
    turn_tokens = [len(json.dumps(t)) // _CHARS_PER_TOKEN for t in turns]
    total = sum(turn_tokens)

    # Drop from the front (oldest) until we fit.
    start = 0
    while total > max_tokens and start < len(turns):
        total -= turn_tokens[start]
        start += 1

    return turns[start:]
