"""Structured logger for RELAIS bricks.

Writes log entries to both the Python ``logging`` module and the
``relais:logs`` Redis stream so that Archiviste can persist them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any


class BrickLogger:
    """Structured logger that writes to Python logging AND ``relais:logs``.

    Each log call emits one entry to the standard Python logger **and** one
    entry to the ``relais:logs`` Redis stream.  Redis errors are silently
    swallowed so that log emission never blocks or crashes the pipeline.

    Args:
        brick_name: Human-readable brick identifier (e.g. ``"portail"``).
        redis_getter: Zero-argument callable returning an async Redis
            connection.  Called on every log call so the logger always
            uses the current connection without holding a stale reference.
    """

    def __init__(self, brick_name: str, redis_getter: Callable[[], Any]) -> None:
        """Initialise the BrickLogger.

        Args:
            brick_name: Name used as the ``brick`` field in log entries.
            redis_getter: Callable that returns the active async Redis
                connection.
        """
        self._brick_name = brick_name
        self._redis_getter = redis_getter
        self._logger = logging.getLogger(brick_name)

    async def _xadd(self, level: str, message: str, correlation_id: str, **extras: Any) -> None:
        """Publish one log entry to ``relais:logs``.

        Silently swallows Redis errors so logging never blocks the pipeline.

        Args:
            level: Log level string (``"INFO"``, ``"ERROR"``, etc.).
            message: Human-readable log message.
            correlation_id: Correlation ID of the originating request.
            **extras: Additional key-value pairs added to the log entry.
        """
        try:
            redis = self._redis_getter()
            entry: dict[str, str] = {
                "level": level,
                "brick": self._brick_name,
                "correlation_id": correlation_id,
                "message": message,
            }
            entry.update({k: str(v) for k, v in extras.items()})
            await redis.xadd("relais:logs", entry)
        except Exception:  # noqa: BLE001
            pass  # Log emission must never crash the pipeline

    async def info(self, message: str, correlation_id: str = "", **extras: Any) -> None:
        """Log at INFO level to both Python logger and ``relais:logs``.

        Args:
            message: Log message text.
            correlation_id: Optional request correlation identifier.
            **extras: Additional fields included in the stream entry.
        """
        self._logger.info("[%s] %s", correlation_id or "-", message)
        await self._xadd("INFO", message, correlation_id, **extras)

    async def warning(self, message: str, correlation_id: str = "", **extras: Any) -> None:
        """Log at WARNING level to both Python logger and ``relais:logs``.

        Args:
            message: Log message text.
            correlation_id: Optional request correlation identifier.
            **extras: Additional fields included in the stream entry.
        """
        self._logger.warning("[%s] %s", correlation_id or "-", message)
        await self._xadd("WARN", message, correlation_id, **extras)

    async def error(self, message: str, correlation_id: str = "", **extras: Any) -> None:
        """Log at ERROR level to both Python logger and ``relais:logs``.

        Args:
            message: Log message text.
            correlation_id: Optional request correlation identifier.
            **extras: Additional fields included in the stream entry.
        """
        self._logger.error("[%s] %s", correlation_id or "-", message)
        await self._xadd("ERROR", message, correlation_id, **extras)
