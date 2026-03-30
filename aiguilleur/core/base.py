"""BaseAiguilleur — abstract base class for all channel adapters.

Defines the lifecycle contract: start / stop / is_alive / restart.
Both NativeAiguilleur (thread) and ExternalAiguilleur (subprocess) implement
this interface so AiguilleurManager can supervise them uniformly.
"""

from __future__ import annotations

import abc
import logging
import time

from aiguilleur.channel_config import ChannelConfig

logger = logging.getLogger(__name__)


class BaseAiguilleur(abc.ABC):
    """Abstract base for channel adapters.

    Concrete subclasses implement the actual I/O for a channel (Discord,
    Telegram, …) or wrap an external process.  AiguilleurManager only
    calls the lifecycle methods defined here.

    Attributes:
        config:          Frozen ChannelConfig for this adapter.
        _restart_count:  Number of restarts performed so far.
    """

    def __init__(self, config: ChannelConfig) -> None:
        self.config: ChannelConfig = config
        self._restart_count: int = 0

    # ------------------------------------------------------------------
    # Lifecycle — subclasses must implement
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def start(self) -> None:
        """Start the adapter (non-blocking; spawns thread/process)."""

    @abc.abstractmethod
    def stop(self, timeout: float = 8.0) -> None:
        """Stop the adapter gracefully within *timeout* seconds."""

    @abc.abstractmethod
    def is_alive(self) -> bool:
        """Return True while the adapter is running."""

    # ------------------------------------------------------------------
    # Restart — default implementation with backoff
    # ------------------------------------------------------------------

    def restart(self, backoff: float = 0.0) -> None:
        """Stop, optionally wait, and start the adapter again.

        Args:
            backoff: Seconds to sleep before restarting (exponential).
        """
        logger.info(
            "Restarting adapter %s (attempt %d, backoff=%.1fs)",
            self.config.name,
            self._restart_count + 1,
            backoff,
        )
        try:
            self.stop(timeout=8.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error stopping %s before restart: %s", self.config.name, exc)

        if backoff > 0:
            time.sleep(backoff)

        self._restart_count += 1
        self.start()
