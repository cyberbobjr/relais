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
import threading
import time

from aiguilleur.channel_config import ChannelConfig, load_channels_config
from aiguilleur.core.base import BaseAiguilleur
from common.config_loader import resolve_config_path

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

    # Hard fields — changing them requires an adapter restart
    _HARD_FIELDS: tuple[str, ...] = ("type", "class_path", "enabled", "command")

    def __init__(self) -> None:
        self._adapters: dict[str, BaseAiguilleur] = {}
        self._running: bool = False
        self._reload_lock: threading.Lock = threading.Lock()
        self._shutdown_event: threading.Event = threading.Event()

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

        self._start_config_watcher()
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
        """Stop all adapters, signal the watcher thread, and set _running=False."""
        self._running = False
        self._shutdown_event.set()
        for name, adapter in self._adapters.items():
            logger.info("Stopping adapter '%s'...", name)
            try:
                adapter.stop(timeout=_STOP_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.error("Error stopping adapter '%s': %s", name, exc)

    # ------------------------------------------------------------------
    # Hot-reload — soft fields only (profile, prompt_path)
    # ------------------------------------------------------------------

    def _reload_channel_profiles(self) -> None:
        """Reload aiguilleur.yaml and update soft fields live without restarting adapters.

        Only ``profile`` (via ``ProfileRef.update()``) and ``prompt_path`` are
        updated in-place.  Hard fields (``type``, ``class_path``, ``enabled``,
        ``command``) emit a WARNING and are otherwise ignored — a restart is
        required for those changes to take effect.

        If the YAML file cannot be read or parsed, logs an error and leaves all
        current configs unchanged (fail-closed).
        """
        try:
            new_configs = load_channels_config()
        except Exception as exc:  # noqa: BLE001
            logger.error("aiguilleur.yaml reload failed — keeping previous config: %s", exc)
            return

        with self._reload_lock:
            for name, new_cfg in new_configs.items():
                adapter = self._adapters.get(name)
                if adapter is None:
                    continue

                old_cfg = adapter.config

                # Replace adapter.config first so that concurrent on_message() calls
                # immediately see the new prompt_path/streaming alongside the new profile.
                # profile_ref is updated AFTER the swap so profile is never ahead of
                # prompt_path (eliminating the TOCTOU window).
                updated_cfg = ChannelConfig(
                    name=new_cfg.name,
                    enabled=new_cfg.enabled,
                    streaming=new_cfg.streaming,
                    type=new_cfg.type,
                    command=new_cfg.command,
                    args=new_cfg.args,
                    class_path=new_cfg.class_path,
                    max_restarts=new_cfg.max_restarts,
                    profile=new_cfg.profile,
                    prompt_path=new_cfg.prompt_path,
                    profile_ref=old_cfg.profile_ref,  # SAME object — identity preserved
                )
                adapter.config = updated_cfg

                # Update soft field: profile via ProfileRef (preserves object identity).
                # Done after adapter.config swap so profile and prompt_path are always
                # consistent from the perspective of any concurrent reader.
                if old_cfg.profile != new_cfg.profile:
                    logger.info(
                        "Channel '%s': profile %r → %r (live)", name, old_cfg.profile, new_cfg.profile
                    )
                    old_cfg.profile_ref.update(new_cfg.profile)

                # Warn on hard-field changes
                for field_name in self._HARD_FIELDS:
                    old_val = getattr(old_cfg, field_name, None)
                    new_val = getattr(new_cfg, field_name, None)
                    if old_val != new_val:
                        logger.warning(
                            "Channel '%s': field '%s' changed (%r → %r) — restart required.",
                            name,
                            field_name,
                            old_val,
                            new_val,
                        )

            # Warn about running channels that disappeared from the new config
            for name in list(self._adapters.keys()):
                if name not in new_configs:
                    logger.warning(
                        "Channel '%s' was removed from aiguilleur.yaml — "
                        "restart required to deactivate the running adapter.",
                        name,
                    )

    def _start_config_watcher(self) -> None:
        """Start a background daemon thread that watches aiguilleur.yaml for changes.

        Uses ``watchfiles`` for efficient filesystem event detection.  If the
        package is not installed, logs a warning and returns without error
        (hot-reload is silently disabled; static config still works).

        The watcher thread is a daemon so it does not block process exit.  It
        stops when ``self._shutdown_event`` is set (called by ``_stop_all``).
        """
        try:
            import watchfiles  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "watchfiles not installed — aiguilleur.yaml hot-reload disabled."
                "  Install with: pip install watchfiles"
            )
            return

        try:
            channels_yaml_path = str(resolve_config_path("aiguilleur.yaml"))
        except FileNotFoundError:
            logger.warning("aiguilleur.yaml not found — hot-reload disabled.")
            return

        shutdown_event = self._shutdown_event

        def _watch_loop() -> None:
            for _changes in watchfiles.watch(channels_yaml_path, stop_event=shutdown_event):
                if shutdown_event.is_set():
                    break
                logger.info("aiguilleur.yaml changed — reloading soft config fields")
                self._reload_channel_profiles()

        t = threading.Thread(
            target=_watch_loop,
            name="aiguilleur-config-watcher",
            daemon=True,
        )
        t.start()

    def _install_signal_handlers(self) -> None:
        """Register SIGTERM and SIGINT handlers for graceful shutdown."""

        def _handle(signum: int, frame: object) -> None:  # type: ignore[type-arg]
            logger.info("Signal %d received — initiating graceful shutdown.", signum)
            self._stop_all()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
