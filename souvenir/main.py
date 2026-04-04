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

Produced:
  - relais:memory:response  — responses to file_read / file_list / clear
  - relais:logs             — operational log entries

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
