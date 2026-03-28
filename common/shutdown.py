"""Graceful shutdown management for RELAIS bricks.

Provides a centralized mechanism to register asyncio tasks and
handle SIGTERM/SIGINT signals cleanly before process exit.
"""
import asyncio
import logging
import signal
from typing import Optional

logger = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_SECONDS = 30


class GracefulShutdown:
    """Manages graceful shutdown of asyncio tasks on SIGTERM/SIGINT.

    Usage::

        shutdown = GracefulShutdown()
        shutdown.install_signal_handlers()
        task = asyncio.create_task(my_worker())
        shutdown.register(task)
        await shutdown.wait_for_tasks()
    """

    def __init__(self) -> None:
        """Initializes GracefulShutdown with an empty task registry and a stop event."""
        self._tasks: list[asyncio.Task] = []
        self._stop_event: asyncio.Event = asyncio.Event()

    def register(self, task: asyncio.Task) -> None:
        """Registers an asyncio task to be managed during shutdown.

        Args:
            task: The asyncio.Task to track. Completed tasks are silently ignored.
        """
        self._tasks.append(task)
        logger.debug("Registered task for graceful shutdown: %s", task.get_name())

    def is_stopping(self) -> bool:
        """Returns True if a shutdown signal has been received.

        Returns:
            True when the stop event is set, False otherwise.
        """
        return self._stop_event.is_set()

    @property
    def stop_event(self) -> asyncio.Event:
        """Exposes the internal stop event so bricks can await it directly.

        Returns:
            The asyncio.Event that is set when shutdown is requested.
        """
        return self._stop_event

    def signal_handler(self, sig: signal.Signals) -> None:
        """Handles SIGTERM or SIGINT by setting the stop event and cancelling tasks.

        This method is synchronous and safe to pass to loop.add_signal_handler().

        Args:
            sig: The signal received (SIGTERM or SIGINT).
        """
        logger.info("Received signal %s — initiating graceful shutdown", sig.name)
        self._stop_event.set()
        for task in self._tasks:
            if not task.done():
                task.cancel()

    def install_signal_handlers(self) -> None:
        """Registers SIGTERM and SIGINT handlers on the running event loop.

        Must be called from within a running asyncio event loop.

        Raises:
            RuntimeError: If there is no running event loop.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.signal_handler, sig)
        logger.debug("Signal handlers installed for SIGTERM and SIGINT")

    async def wait_for_tasks(self, timeout: Optional[float] = None) -> None:
        """Waits for all registered tasks to complete, then cancels stragglers.

        Args:
            timeout: Maximum seconds to wait. Defaults to SHUTDOWN_TIMEOUT_SECONDS (30s).
                     Tasks still running after the timeout are forcibly cancelled.
        """
        if timeout is None:
            timeout = SHUTDOWN_TIMEOUT_SECONDS

        active = [t for t in self._tasks if not t.done()]
        if not active:
            logger.debug("No active tasks to wait for")
            return

        logger.info("Waiting for %d task(s) to finish (timeout=%ss)…", len(active), timeout)
        try:
            await asyncio.wait_for(
                asyncio.gather(*active, return_exceptions=True),
                timeout=timeout,
            )
            logger.info("All tasks completed cleanly")
        except asyncio.TimeoutError:
            remaining = [t for t in active if not t.done()]
            logger.warning(
                "Shutdown timeout reached — force-cancelling %d task(s)", len(remaining)
            )
            for task in remaining:
                task.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)
            logger.info("Force-cancelled tasks finished")
