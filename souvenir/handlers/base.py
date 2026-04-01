"""Base abstractions for Souvenir action handlers.

Defines :class:`HandlerContext` (frozen dataclass carrying all handler
dependencies) and :class:`BaseActionHandler` (ABC each handler implements).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from souvenir.context_store import ContextStore
from souvenir.long_term_store import LongTermStore


@dataclass(frozen=True)
class HandlerContext:
    """Immutable bundle of dependencies passed to every action handler.

    Args:
        redis_conn: Async Redis connection.
        context_store: Short-term Redis context store.
        long_term_store: Long-term SQLite store.
        req: Parsed JSON payload from the stream message.
        stream_res: Name of the response stream (``relais:memory:response``).
    """

    redis_conn: Any
    context_store: ContextStore
    long_term_store: LongTermStore
    req: dict[str, Any]
    stream_res: str


class BaseActionHandler(abc.ABC):
    """Abstract base class for Souvenir action handlers.

    Subclasses implement :meth:`handle` to process one specific action
    received from ``relais:memory:request``.
    """

    @abc.abstractmethod
    async def handle(self, ctx: HandlerContext) -> None:
        """Execute the handler logic for one incoming request.

        Args:
            ctx: Context bundle containing all runtime dependencies and the
                parsed request payload.
        """
