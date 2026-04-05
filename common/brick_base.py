"""Abstract base class for RELAIS bricks.

Provides the shared lifecycle plumbing that every brick needs:

* Redis stream consumption loop (``_run_stream_loop``)
* Graceful shutdown via ``asyncio.Event``
* Hot-reload file watcher (``_start_file_watcher``)
* Redis Pub/Sub reload listener (``_config_reload_listener``)
* Structured logging to ``relais:logs`` (``BrickLogger``)
* Signal-handler wiring via ``GracefulShutdown``

Usage::

    class MyBrick(BrickBase):
        def _load(self) -> None:
            # load YAML config into self._config, etc.
            ...

        def stream_specs(self) -> list[StreamSpec]:
            return [
                StreamSpec(
                    stream="relais:my:stream",
                    group="my_group",
                    consumer="my_1",
                    handler=self._handle,
                    ack_mode="always",
                )
            ]

        async def _handle(self, envelope: Envelope, redis: Any) -> bool:
            ...
            return True
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from common.brick_logger import BrickLogger
from common.config_reload import safe_reload, watch_and_reload
from common.envelope import Envelope
from common.redis_client import RedisClient
from common.shutdown import GracefulShutdown

# Configure logging once at import time using LOG_LEVEL env-var.
_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout,
)


# ---------------------------------------------------------------------------
# StreamSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamSpec:
    """Descriptor for one Redis stream a brick wants to consume.

    Attributes:
        stream: Redis stream name (e.g. ``"relais:tasks"``).
        group: Consumer group name.
        consumer: Consumer name within the group.
        handler: Async callable ``(envelope, redis_conn) -> bool``.
            Return ``True`` to ACK the message, ``False`` to leave it in the PEL.
        ack_mode: ``"always"`` — XACK unconditionally (errors are logged and
            the message is dropped from the PEL); ``"on_success"`` — XACK only
            when the handler returns ``True`` (message stays in PEL on ``False``
            so it is re-delivered on the next poll).
        block_ms: Milliseconds to block on ``XREADGROUP`` (default 2 000 ms).
        count: Maximum messages to fetch per ``XREADGROUP`` call (default 10).
    """

    stream: str
    group: str
    consumer: str
    handler: Callable[[Envelope, Any], Awaitable[bool]]
    ack_mode: Literal["always", "on_success"] = "always"
    block_ms: int = 2000
    count: int = 10


# ---------------------------------------------------------------------------
# BrickLogger — re-exported from common.brick_logger for backward compatibility
# ---------------------------------------------------------------------------
# BrickLogger is defined in common/brick_logger.py and imported at the top of
# this module.  It is re-exported here so that existing code that imports it
# via ``from common.brick_base import BrickLogger`` continues to work.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BrickBase
# ---------------------------------------------------------------------------


class BrickBase(abc.ABC):
    """Abstract base class for RELAIS pipeline bricks.

    Handles the full service lifecycle: Redis connection, stream loops,
    hot-reload (file watcher + Pub/Sub listener), graceful shutdown, and
    structured logging.

    Subclasses MUST implement:
        * ``_load()`` — load/reload configuration from disk.
        * ``stream_specs()`` — return the list of streams to consume.

    Subclasses MAY override:
        * ``_config_watch_paths()`` — config files to watch for changes.
        * ``_build_config_candidate()`` — build a new config snapshot.
        * ``_apply_config(candidate)`` — apply the new snapshot atomically.
        * ``on_startup(redis)`` — async hook called before the stream loops start.
        * ``on_shutdown()`` — async hook called after all loops have stopped.
        * ``_extra_lifespan(stack)`` — hook to enter additional async context managers.
    """

    def __init__(self, brick_name: str) -> None:
        """Initialise common brick infrastructure.

        Args:
            brick_name: Stable brick identifier used for Redis ACL, logging,
                and Pub/Sub channel names (e.g. ``"portail"``).
        """
        self._brick_name = brick_name
        self._config_lock: asyncio.Lock = asyncio.Lock()
        self._logger = logging.getLogger(brick_name)
        # BrickLogger is set up in start() once we have a Redis connection;
        # we create a placeholder here so subclasses can reference it freely.
        self._brick_logger: BrickLogger | None = None
        # client is created lazily in start() so that unit tests can patch
        # common.brick_base.RedisClient before it is instantiated.
        self.client: RedisClient | None = None

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def _load(self) -> None:
        """Initialisation hook — load brick configuration from disk.

        This is an **initialisation** hook called by the subclass ``__init__``
        (not by BrickBase itself) to populate the brick's configuration state
        on first startup.  It is **not** called by the hot-reload mechanism.

        Hot-reload uses a separate two-step contract:

        * ``_build_config_candidate()`` — build a new config snapshot without
          mutating ``self`` (called by ``reload_config()`` via ``safe_reload``).
        * ``_apply_config(candidate)`` — atomically swap in the new snapshot
          while ``self._config_lock`` is held.

        Subclasses MUST call ``self._load()`` in their own ``__init__`` (after
        calling ``super().__init__``).  If a subclass has no configuration to
        load, it should implement this method as a no-op.

        Must be synchronous — do not touch Redis or any async resource.

        Raises:
            Any exception from YAML parsing or filesystem access propagates
            to the subclass ``__init__`` and aborts startup.
        """

    @abc.abstractmethod
    def stream_specs(self) -> list[StreamSpec]:
        """Return the list of Redis streams this brick wants to consume.

        Returns:
            One ``StreamSpec`` per stream/consumer-group pair.
        """

    # ------------------------------------------------------------------
    # Optional hooks (override in subclasses as needed)
    # ------------------------------------------------------------------

    def _config_watch_paths(self) -> list[Path]:
        """Return filesystem paths to watch for hot-reload.

        Override to return your brick's YAML config file(s).

        Returns:
            Empty list by default (no file watching).
        """
        return []

    def _build_config_candidate(self) -> Any:
        """Build a fresh config snapshot from disk without mutating ``self``.

        Override together with ``_apply_config`` to get atomic hot-reload via
        ``safe_reload``.  The default no-op means ``reload_config()`` always
        succeeds immediately.

        Returns:
            ``None`` by default.
        """
        return None

    def _apply_config(self, candidate: Any) -> None:
        """Apply a freshly loaded configuration snapshot.

        Called while ``self._config_lock`` is held.

        Args:
            candidate: The value returned by ``_build_config_candidate()``.
        """

    async def on_startup(self, redis: Any) -> None:
        """Async hook called once before stream loops start.

        Override to initialise resources that require an async context (e.g.
        SQLite tables via ``CREATE TABLE IF NOT EXISTS``).

        Args:
            redis: Live async Redis connection.
        """

    async def on_shutdown(self) -> None:
        """Async hook called once after all stream loops have stopped.

        Override to release resources (close DB connections, flush caches…).
        """

    async def _extra_lifespan(self, stack: AsyncExitStack) -> None:
        """Hook to enter additional async context managers into the lifespan stack.

        Used by Atelier to manage the LangGraph checkpointer context manager.

        Args:
            stack: The ``AsyncExitStack`` used for the brick's lifespan.
        """

    # ------------------------------------------------------------------
    # Hot-reload
    # ------------------------------------------------------------------

    async def reload_config(self) -> bool:
        """Hot-reload brick configuration using ``safe_reload``.

        Subclasses with non-trivial configuration should override
        ``_build_config_candidate`` and ``_apply_config`` instead of this
        method.

        Returns:
            ``True`` when the configuration was successfully reloaded.
            ``False`` when the loader raised (previous config preserved).
        """
        candidate = self._build_config_candidate()
        if candidate is None:
            # Nothing to reload
            return True
        return await safe_reload(
            self._config_lock,
            self._brick_name,
            self._build_config_candidate,
            self._apply_config,
            checkpoint_paths=self._config_watch_paths(),
        )

    def _start_file_watcher(self, shutdown_event: "asyncio.Event | None" = None) -> "asyncio.Task | None":
        """Create a background task that watches config files for changes.

        Returns ``None`` when watchfiles is not installed (hot-reload degrades
        gracefully to Redis Pub/Sub only).

        The file watcher task terminates via asyncio task cancellation (not via
        ``shutdown_event``); ``shutdown_event`` is accepted for interface
        uniformity with bricks that manage their own watcher loop.

        Args:
            shutdown_event: Optional event to signal loop termination.
                Accepted for interface compatibility; the default file watcher
                uses task cancellation rather than this event.

        Returns:
            An asyncio.Task, or None if watchfiles is unavailable or there
            are no paths to watch.
        """
        from common.config_reload import watchfiles as _wf

        paths = self._config_watch_paths()
        if not paths:
            return None
        if _wf is None:
            self._logger.warning(
                "%s: watchfiles not installed — file-based hot-reload disabled. "
                "Install with: pip install watchfiles",
                self._brick_name,
            )
            return None
        return asyncio.create_task(
            watch_and_reload(paths, self.reload_config, self._brick_name)
        )

    async def _config_reload_listener(
        self,
        redis_conn: Any,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        """Subscribe to ``relais:config:reload:{brick}`` and trigger hot-reloads.

        Only the exact string ``"reload"`` triggers a config reload; all other
        messages are silently ignored.

        Args:
            redis_conn: Active async Redis connection (must support pub/sub).
            shutdown_event: Optional event to signal loop termination.  When
                ``None`` (e.g. in unit tests where ``listen()`` returns a
                finite iterator), the loop runs until the iterator is
                exhausted.
        """
        pubsub = redis_conn.pubsub()
        channel = f"relais:config:reload:{self._brick_name}"
        await pubsub.subscribe(channel)
        self._logger.info("%s: subscribed to %s", self._brick_name, channel)

        async for message in pubsub.listen():
            if shutdown_event is not None and shutdown_event.is_set():
                break
            if message.get("type") != "message":
                continue
            data = message.get("data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if data == "reload":
                self._logger.info(
                    "%s: received reload signal — reloading config", self._brick_name
                )
                await self.reload_config()

    # ------------------------------------------------------------------
    # Stream loop
    # ------------------------------------------------------------------

    async def _run_stream_loop(
        self,
        spec: StreamSpec,
        redis: Any,
        shutdown_event: asyncio.Event,
    ) -> None:
        """Consume messages from a single Redis stream until shutdown.

        Creates the consumer group (idempotent), then polls with
        ``XREADGROUP`` in a tight loop.  For each message:

        1. Deserialize the ``"payload"`` field as an ``Envelope``.
        2. Call ``spec.handler(envelope, redis)``.
        3. ACK according to ``spec.ack_mode``:
           - ``"always"``: XACK unconditionally (after handler or on exception).
           - ``"on_success"``: XACK only when handler returns ``True``.

        Exceptions raised by the handler are caught and logged; the loop
        continues processing the next message.

        Args:
            spec: The ``StreamSpec`` describing the stream to consume.
            redis: Active async Redis connection.
            shutdown_event: Event that signals when the loop should exit.
        """
        _log = getattr(self, "_logger", logging.getLogger("brick"))
        _name = getattr(self, "_brick_name", "brick")

        try:
            await redis.xgroup_create(spec.stream, spec.group, mkstream=True)
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" not in str(exc):
                _log.warning(
                    "%s: consumer group error for %s: %s", _name, spec.stream, exc
                )

        _log.info(
            "%s: listening on %s (group=%s, ack_mode=%s)",
            _name,
            spec.stream,
            spec.group,
            spec.ack_mode,
        )

        while not shutdown_event.is_set():
            try:
                results = await redis.xreadgroup(
                    spec.group,
                    spec.consumer,
                    {spec.stream: ">"},
                    count=spec.count,
                    block=spec.block_ms,
                )

                if not results:
                    continue

                for _stream, messages in results:
                    for message_id, data in messages:
                        should_ack = spec.ack_mode == "always"
                        try:
                            raw = data.get(b"payload") or data.get("payload", "{}")
                            if isinstance(raw, bytes):
                                raw = raw.decode()
                            envelope = Envelope.from_json(raw)
                            result = await spec.handler(envelope, redis)
                            if spec.ack_mode == "on_success":
                                should_ack = bool(result)
                        except Exception as exc:  # noqa: BLE001
                            _log.error(
                                "%s: error processing message %s on %s: %s",
                                _name,
                                message_id,
                                spec.stream,
                                exc,
                                exc_info=True,
                            )
                            # ack_mode="always" → still ACK to avoid poisoning PEL
                            # ack_mode="on_success" → leave in PEL (already False)
                        finally:
                            if should_ack:
                                await redis.xack(spec.stream, spec.group, message_id)

            except Exception as exc:  # noqa: BLE001
                _log.error(
                    "%s: stream loop error on %s: %s", _name, spec.stream, exc
                )
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the brick: connect, run startup hook, launch loops, then shutdown.

        Orchestration order:
        1. Obtain Redis connection.
        2. Log startup to ``relais:logs``.
        3. Enter ``_extra_lifespan`` context managers.
        4. Call ``on_startup(redis)``.
        5. Start file-watcher and Pub/Sub-reload tasks.
        6. Launch one asyncio task per ``StreamSpec`` (all run concurrently).
        7. Await all tasks (they exit when ``shutdown_event`` is set).
        8. Cancel background tasks.
        9. Call ``on_shutdown()``.
        10. Close Redis connection.
        """
        shutdown = GracefulShutdown()
        shutdown.install_signal_handlers()
        brick_name = getattr(self, "_brick_name", "unknown")
        # Ensure _logger is available even when __new__ bypasses __init__ (tests).
        if not hasattr(self, "_logger"):
            self._logger = logging.getLogger(brick_name)
        if not hasattr(self, "_config_lock"):
            self._config_lock = asyncio.Lock()
        if getattr(self, "client", None) is None:
            self.client = RedisClient(brick_name)
        redis_conn = await self.client.get_connection()
        self._brick_logger = BrickLogger(brick_name, lambda: redis_conn)

        await redis_conn.xadd(
            "relais:logs",
            {
                "level": "INFO",
                "brick": brick_name,
                "message": f"{brick_name} started",
            },
        )

        shutdown_event = shutdown.stop_event

        async with AsyncExitStack() as stack:
            await self._extra_lifespan(stack)
            await self.on_startup(redis_conn)

            reload_listener_task = asyncio.create_task(
                self._config_reload_listener(redis_conn, shutdown_event)
            )
            watcher_task = self._start_file_watcher(shutdown_event)

            stream_tasks = [
                asyncio.create_task(
                    self._run_stream_loop(spec, redis_conn, shutdown_event)
                )
                for spec in self.stream_specs()
            ]

            try:
                if stream_tasks:
                    await asyncio.gather(*stream_tasks)
            except asyncio.CancelledError:
                self._logger.info("%s shutting down...", brick_name)
            finally:
                reload_listener_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reload_listener_task
                if watcher_task is not None:
                    watcher_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await watcher_task
                for t in stream_tasks:
                    if not t.done():
                        t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.gather(*stream_tasks, return_exceptions=True)

        await self.on_shutdown()
        await self.client.close()
        self._logger.info("%s stopped gracefully", brick_name)
