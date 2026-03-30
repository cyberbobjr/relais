"""AiguilleurManager — lifecycle supervisor for all channel adapters.

Loads channel configs, instantiates adapters, starts them, and monitors
health in a supervision loop.  On crash: exponential-backoff restart.
On SIGTERM: graceful shutdown with timeout.

The manager is ONLY a lifecycle supervisor.  It has no knowledge of
Redis Streams, Envelopes, or any message-bus protocol — that is entirely
the adapter's responsibility.
"""

from __future__ import annotations

import importlib
import logging
import signal
import time

from aiguilleur.channel_config import ChannelConfig, load_channels_config
from aiguilleur.core.base import BaseAiguilleur

logger = logging.getLogger(__name__)

# Backoff: min(2^restart_count, _MAX_BACKOFF_SECONDS)
_MAX_BACKOFF_SECONDS: float = 30.0
# Interval between supervision checks
_SUPERVISE_INTERVAL_SECONDS: float = 5.0
# Timeout per adapter on shutdown
_STOP_TIMEOUT_SECONDS: float = 8.0


class AiguilleurManager:
    """Lifecycle supervisor for all channel adapters.

    Usage::

        manager = AiguilleurManager()
        manager.run()  # blocks until SIGTERM/SIGINT
    """

    def __init__(self) -> None:
        self._adapters: dict[str, BaseAiguilleur] = {}
        self._running: bool = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Load adapters, start enabled ones, and supervise until shutdown."""
        self._install_signal_handlers()

        configs = load_channels_config()
        for name, cfg in configs.items():
            if not cfg.enabled:
                logger.info("Channel '%s' is disabled — skipping.", name)
                continue
            adapter = self._load_adapter(name, cfg)
            self._adapters[name] = adapter
            adapter.start()

        self._running = True
        self._supervise()

    # ------------------------------------------------------------------
    # Adapter discovery
    # ------------------------------------------------------------------

    def _load_adapter(self, name: str, cfg: ChannelConfig) -> BaseAiguilleur:
        """Instantiate the adapter for *name* using convention or class_path.

        Discovery order:
        1. If cfg.class_path is set: import that module and extract the class.
        2. Otherwise: import ``aiguilleur.channels.{name}.adapter`` and look
           for a class whose name ends with ``Aiguilleur``.

        Args:
            name: Channel identifier (e.g. 'discord').
            cfg:  Frozen ChannelConfig for the channel.

        Returns:
            Instantiated (but not yet started) BaseAiguilleur subclass.

        Raises:
            ImportError: Module not found.
            AttributeError: Class not found in module.
        """
        if cfg.class_path:
            module_path, class_name = cfg.class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
        else:
            module = importlib.import_module(f"aiguilleur.channels.{name}.adapter")
            # Find the *Aiguilleur class in the module
            cls = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and attr_name.endswith("Aiguilleur")
                    and attr_name != "BaseAiguilleur"
                ):
                    cls = attr
                    break
            if cls is None:
                raise AttributeError(
                    f"No *Aiguilleur class found in aiguilleur.channels.{name}.adapter"
                )

        logger.debug("Loaded adapter class %s for channel '%s'", cls.__name__, name)
        return cls(cfg)

    # ------------------------------------------------------------------
    # Supervision loop
    # ------------------------------------------------------------------

    def _supervise(self) -> None:
        """Main supervision loop — checks health and restarts crashed adapters."""
        while self._running:
            time.sleep(_SUPERVISE_INTERVAL_SECONDS)
            if self._running:
                self._check_and_restart()

    def _check_and_restart(self) -> None:
        """Inspect each adapter; restart crashed ones or remove exhausted ones."""
        for name in list(self._adapters.keys()):
            adapter = self._adapters[name]
            if adapter.is_alive():
                continue

            if adapter._restart_count >= adapter.config.max_restarts:
                logger.critical(
                    "Adapter '%s' exceeded max_restarts (%d) — removing from supervision.",
                    name,
                    adapter.config.max_restarts,
                )
                del self._adapters[name]
                continue

            backoff = min(2.0 ** adapter._restart_count, _MAX_BACKOFF_SECONDS)
            logger.warning(
                "Adapter '%s' is not alive — restarting (backoff=%.1fs).", name, backoff
            )
            adapter.restart(backoff=backoff)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    def _stop_all(self) -> None:
        """Stop all adapters and set _running=False."""
        self._running = False
        for name, adapter in self._adapters.items():
            logger.info("Stopping adapter '%s'...", name)
            try:
                adapter.stop(timeout=_STOP_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.error("Error stopping adapter '%s': %s", name, exc)

    def _install_signal_handlers(self) -> None:
        """Register SIGTERM and SIGINT handlers for graceful shutdown."""

        def _handle(signum: int, frame: object) -> None:  # type: ignore[type-arg]
            logger.info("Signal %d received — initiating graceful shutdown.", signum)
            self._stop_all()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
