"""Souvenir brick — long-term memory service.

Functional role
---------------
Archives conversational turns and handles agent memory file operations.
Archival is triggered by Atelier via an ``archive`` action on
``relais:memory:request`` — the full message history no longer transits
through the outgoing envelope stream.

Technical overview
------------------
``Souvenir`` runs a single asyncio consumer loop on ``relais:memory:request``.

* ``archive`` — persists the completed agent turn to SQLite (sent by Atelier
  after each successful LLM call).
* ``clear`` / ``file_write`` / ``file_read`` / ``file_list`` — memory file
  operations triggered by agents via ``SouvenirBackend``.

Key classes:

* ``LongTermStore`` — SQLite ``~/.relais/storage/memory.db``; stores full
  message history as one row per turn (upsert on ``correlation_id``).
* ``FileStore`` — SQLite ``~/.relais/storage/memory.db`` (table
  ``memory_files``); stores persistent agent memory files routed by
  ``SouvenirBackend`` in Atelier.
* ``HandlerContext`` + action registry — extensible dispatch pattern for
  request actions.

Redis channels
--------------
Consumed:
  - relais:memory:request   (consumer group: souvenir_group)
  - relais:config:reload:souvenir  (Pub/Sub channel for hot-reload trigger)

Produced:
  - relais:memory:response  — responses to file_read / file_list / clear
  - relais:logs             — operational log entries

Configuration hot-reload
------------------------
Souvenir watches souvenir configuration for changes and reloads without
restarting:

* Watched files: souvenir/profiles.yaml (memory extractor model config)
* Reload trigger: File system change detected via watchfiles library
* Reload mechanism: safe_reload() performs atomic parse → lock → swap pattern;
  if new config is invalid YAML, previous config is preserved
* Redis Pub/Sub channel: relais:config:reload:souvenir (listens for external
  reload triggers from operator)
* Config backups: up to 5 versions stored in ~/.relais/config/backups/

Processing flow
---------------
  (1) Consume from relais:memory:request (souvenir_group).
  (2) Parse action field from JSON payload.
  (3) Dispatch to registered handler (archive / clear / file_write / file_read / file_list).
  (4) XACK.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout,
)

from common.redis_client import RedisClient
from common.shutdown import GracefulShutdown
from souvenir.file_store import FileStore
from souvenir.handlers import HandlerContext, build_registry
from souvenir.long_term_store import LongTermStore

logger = logging.getLogger("souvenir")


class Souvenir:
    """Brique mémoire : archivage long terme (SQLite/SQLModel) et fichiers agent.

    Consomme ``relais:memory:request`` pour les actions ``archive`` / ``clear``
    / ``file_write`` / ``file_read`` / ``file_list``.  L'archivage SQLite est
    déclenché par Atelier via l'action ``archive`` — l'historique ne transite
    plus dans les enveloppes sortantes.
    """

    def __init__(self) -> None:
        """Initialise les streams Redis, les stores mémoire et le registre d'actions."""
        self.client = RedisClient("souvenir")
        self.stream_req = "relais:memory:request"
        self.stream_res = "relais:memory:response"
        self.group_name = "souvenir_group"
        self.consumer_name = "souvenir_1"
        self._long_term = LongTermStore()
        self._file_store = FileStore()
        self._action_registry = build_registry()
        # Config reload lock — Souvenir has no config to reload today;
        # the interface exists for consistency and forward-compatibility.
        self._config_lock: asyncio.Lock = asyncio.Lock()

    def _load(self) -> None:
        """Reload Souvenir's configuration from disk.

        No-op for now: Souvenir has no YAML configuration to reload.  The
        method exists so that the hot-reload contract is consistent across all
        bricks and can be extended in the future without interface changes.

        Returns:
            None
        """

    async def reload_config(self) -> bool:
        """Hot-reload Souvenir's configuration.

        No-op for now — returns True immediately since there is no configuration
        to reload.  The method exists for interface consistency with other bricks.

        Returns:
            True (always — there is nothing to reload, so it always "succeeds").
        """
        return True

    def _config_watch_paths(self) -> list[Path]:
        """Return the list of config file paths to watch for changes.

        Souvenir has no YAML configuration to watch today.  Returns an empty
        list so that ``_start_file_watcher`` knows not to create a task.

        Returns:
            An empty list.
        """
        return []

    def _start_file_watcher(self) -> asyncio.Task | None:
        """Create a file watcher task, or return None if there are no paths to watch.

        Returns:
            None when ``_config_watch_paths()`` is empty (current behaviour).
            An asyncio.Task when there are paths to watch (future use).
        """
        paths = self._config_watch_paths()
        if not paths:
            return None
        from common.config_reload import watch_and_reload
        return asyncio.create_task(
            watch_and_reload(paths, self.reload_config, "souvenir")
        )

    async def _config_reload_listener(self, redis_conn: Any) -> None:
        """Subscribe to ``relais:config:reload:souvenir`` and trigger hot-reloads.

        Runs as a background asyncio task.  Only the exact string ``"reload"``
        triggers a config reload (currently a no-op); all other messages are
        silently ignored.

        Args:
            redis_conn: Active async Redis connection (must support pub/sub).
        """
        pubsub = redis_conn.pubsub()
        channel = "relais:config:reload:souvenir"
        await pubsub.subscribe(channel)
        logger.info("Souvenir: subscribed to %s", channel)

        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if data == "reload":
                logger.info("Souvenir: received reload signal — reloading config")
                await self.reload_config()

    # ------------------------------------------------------------------
    # Internal consumer loop
    # ------------------------------------------------------------------

    async def _process_request_stream(
        self,
        redis_conn: Any,
        shutdown: GracefulShutdown | None = None,
    ) -> None:
        """Consomme ``relais:memory:request`` et répond aux actions enregistrées.

        Exits cleanly when ``shutdown.is_stopping()`` returns True.

        Args:
            redis_conn: Connexion Redis async.
            shutdown: GracefulShutdown instance controlling the loop lifetime.
                If None a new instance is created (backward-compatible).
        """
        if shutdown is None:
            shutdown = GracefulShutdown()

        try:
            await redis_conn.xgroup_create(
                self.stream_req, self.group_name, mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                logger.warning("Consumer group error: %s", exc)

        logger.info("Souvenir listening on relais:memory:request ...")

        while not shutdown.is_stopping():
            try:
                results = await redis_conn.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_req: ">"},
                    count=10,
                    block=2000,
                )

                for _stream, messages in results:
                    for message_id, data in messages:
                        try:
                            _payload = data.get(b"payload") or data.get("payload", "{}")
                            if isinstance(_payload, bytes):
                                _payload = _payload.decode()
                            req = json.loads(_payload)
                            action = req.get("action")
                            ctx = HandlerContext(
                                redis_conn=redis_conn,
                                long_term_store=self._long_term,
                                file_store=self._file_store,
                                req=req,
                                stream_res=self.stream_res,
                            )
                            handler = self._action_registry.get(action)
                            if handler:
                                await handler.handle(ctx)
                            else:
                                logger.warning("Unknown memory action: %s", action)

                        except Exception as inner_exc:
                            logger.error(
                                "Failed to process memory message %s: %s",
                                message_id,
                                inner_exc,
                            )
                        finally:
                            await redis_conn.xack(
                                self.stream_req, self.group_name, message_id
                            )

            except Exception as exc:
                logger.error("Request stream error: %s", exc)
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Démarre la brique Souvenir.

        Initialise les tables SQLite, obtient la connexion Redis et lance la
        boucle de consommation ``relais:memory:request``.

        Registers SIGTERM/SIGINT handlers via GracefulShutdown so both consumer
        loops exit cleanly when the process receives a termination signal.

        Returns:
            None
        """
        shutdown = GracefulShutdown()
        shutdown.install_signal_handlers()
        redis_conn = await self.client.get_connection()
        await redis_conn.xadd(
            "relais:logs",
            {"level": "INFO", "brick": "souvenir", "correlation_id": "", "sender_id": "", "message": "Souvenir started"},
        )
        try:
            logger.warning(
                "Initialising SQLite schema via _create_tables() — "
                "run 'alembic upgrade head' in production instead."
            )
            await self._long_term._create_tables()
            await self._file_store._create_tables()
            await self._process_request_stream(redis_conn, shutdown=shutdown)
        except asyncio.CancelledError:
            logger.info("Souvenir shutting down...")
        finally:
            await self._long_term.close()
            await self._file_store.close()
            await self.client.close()
            logger.info("Souvenir stopped gracefully")


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    souvenir = Souvenir()
    try:
        asyncio.run(souvenir.start())
    except KeyboardInterrupt:
        pass
