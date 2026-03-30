"""NativeAiguilleur — Python adapter running in a dedicated OS thread.

Each NativeAiguilleur spawns one daemon thread that calls asyncio.run(self.run()).
This gives the adapter its own event loop and its own Redis client, completely
isolated from other adapters and from the main process.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from aiguilleur.channel_config import ChannelConfig
from aiguilleur.core.base import BaseAiguilleur

logger = logging.getLogger(__name__)


class NativeAiguilleur(BaseAiguilleur):
    """Base for Python-native channel adapters.

    Subclasses implement ``run()`` — an async coroutine that owns the
    adapter's entire lifecycle (Redis client creation, subscription loop,
    clean shutdown when ``_stop_event`` is set).

    The thread is a daemon so Python exit does not block on it.
    """

    def __init__(self, config: ChannelConfig) -> None:
        super().__init__(config)
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the adapter thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_in_thread,
            name=f"aiguilleur-{self.config.name}",
            daemon=True,
        )
        self._thread.start()
        logger.info("Adapter %s started (thread=%s)", self.config.name, self._thread.name)

    def stop(self, timeout: float = 8.0) -> None:
        """Signal the adapter to stop and wait for thread exit."""
        logger.info("Stopping adapter %s (timeout=%.1fs)", self.config.name, timeout)
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "Adapter %s did not stop within %.1fs", self.config.name, timeout
                )

    def is_alive(self) -> bool:
        """Return True while the adapter thread is running."""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_in_thread(self) -> None:
        """Entry point called in the adapter thread.

        Runs the async ``run()`` coroutine in a fresh event loop.
        """
        try:
            asyncio.run(self.run())
        except Exception as exc:  # noqa: BLE001
            logger.error("Adapter %s crashed: %s", self.config.name, exc, exc_info=True)

    # ------------------------------------------------------------------
    # Abstract — subclasses implement
    # ------------------------------------------------------------------

    @property
    def stop_event(self) -> threading.Event:
        """The stop signal — poll this in your run() loop to exit cleanly."""
        return self._stop_event

    async def run(self) -> None:
        """Async entry point for the adapter.

        Implement the adapter logic here.  Check ``self.stop_event.is_set()``
        periodically to detect shutdown requests.

        Raises:
            NotImplementedError: Must be overridden by concrete subclasses.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement run()"
        )
