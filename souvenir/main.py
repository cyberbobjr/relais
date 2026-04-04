"""Souvenir brick — long-term memory service.

Functional role
---------------
Archives conversational turns and handles agent memory file operations.
Passively observes outgoing replies to persist full turn history in SQLite.

Technical overview
------------------
``Souvenir`` runs two concurrent asyncio consumer loops:

* Request loop — handles ``clear`` / ``file_write`` / ``file_read`` /
  ``file_list`` actions from Commandant/agents.
* Outgoing observer loop — watches per-channel outgoing streams to archive
  each exchange to SQLite.

> **Note**: Atelier no longer fetches context from Souvenir.  Conversation
> history is managed by the LangGraph checkpointer (``AsyncSqliteSaver``,
> ``checkpoints.db``) owned by Atelier.  The ``get`` action and the
> ``relais:memory:response`` stream have been removed.

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
  - relais:memory:request               (consumer group: souvenir_group)
  - relais:messages:outgoing:{channel}  for each channel in _DEFAULT_CHANNELS
                                        (consumer group: souvenir_outgoing_group)

Produced:
  - relais:logs             — operational log entries

Processing flow — request loop
------------------------------
  (1) Consume from relais:memory:request (souvenir_group).
  (2) Parse action field from JSON payload.
  (3) Dispatch to registered handler (clear / file_write / file_read / file_list).
  (4) XACK.

Processing flow — outgoing observer loop
-----------------------------------------
  (1) Consume from relais:messages:outgoing:{channel}
      (souvenir_outgoing_group).
  (2) Deserialize Envelope.
  (3) Read messages_raw list from envelope.metadata["messages_raw"] (produced
      by Atelier via atelier.message_serializer.serialize_messages()).
  (4) Archive turn to SQLite long-term store: one row per correlation_id
      (upsert), fields user_content + assistant_content + messages_raw JSON.
  (5) XACK.
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

from common.envelope import Envelope
from common.redis_client import RedisClient
from common.shutdown import GracefulShutdown
from souvenir.file_store import FileStore
from souvenir.handlers import HandlerContext, build_registry
from souvenir.long_term_store import LongTermStore

logger = logging.getLogger("souvenir")

# Canaux dont les streams sortants sont observés.
_DEFAULT_CHANNELS = ["discord", "telegram"]


class Souvenir:
    """Brique mémoire : archivage long terme (SQLite/SQLModel) et fichiers agent.

    Consomme deux familles de streams :

    1. ``relais:memory:request`` — actions ``clear`` / ``file_write`` / ``file_read`` / ``file_list``.
    2. ``relais:messages:outgoing:{channel}`` — observe les réponses sortantes
       pour archiver le tour complet dans SQLite (``messages_raw``) et extraire
       des faits utilisateur.
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
        self._channels: list[str] = _DEFAULT_CHANNELS
        self._action_registry = build_registry()

    # ------------------------------------------------------------------
    # Public handler methods (testable without a running Redis)
    async def _handle_outgoing(
        self,
        envelope: Envelope,
        long_term_store: LongTermStore,
    ) -> None:
        """Traite un message sortant : archivage en SQLite long terme.

        Reads messages_raw from envelope.metadata (full LangChain message
        list for the turn, serialized by Atelier) and archives the turn to
        SQLite (upsert on correlation_id).

        Args:
            envelope: L'enveloppe du message sortant (réponse de l'assistant).
            long_term_store: Store long terme SQLite.
        """
        messages_raw: list[dict] = envelope.metadata.get("messages_raw") or []
        await long_term_store.archive(envelope, messages_raw)

    # ------------------------------------------------------------------
    # Internal consumer loops
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

    async def _process_outgoing_streams(
        self,
        redis_conn: Any,
        shutdown: GracefulShutdown | None = None,
    ) -> None:
        """Observe ``relais:messages:outgoing:{channel}`` pour tous les canaux connus.

        Crée un consumer group par canal (idempotent) puis entre dans une
        boucle de lecture. Chaque message est désérialisé en ``Envelope`` et
        traité par ``_handle_outgoing``.

        Exits cleanly when ``shutdown.is_stopping()`` returns True.

        Args:
            redis_conn: Connexion Redis async.
            shutdown: GracefulShutdown instance controlling the loop lifetime.
                If None a new instance is created (backward-compatible).
        """
        if shutdown is None:
            shutdown = GracefulShutdown()

        outgoing_group = "souvenir_outgoing_group"
        stream_map: dict[str, str] = {}
        for channel in self._channels:
            stream = f"relais:messages:outgoing:{channel}"
            try:
                await redis_conn.xgroup_create(stream, outgoing_group, mkstream=True)
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    logger.warning("Outgoing group error for %s: %s", channel, exc)
            stream_map[stream] = ">"

        logger.info(
            "Souvenir observing outgoing streams: %s", list(stream_map.keys())
        )

        while not shutdown.is_stopping():
            try:
                results = await redis_conn.xreadgroup(
                    outgoing_group,
                    self.consumer_name,
                    stream_map,
                    count=10,
                    block=2000,
                )

                for _stream, messages in results:
                    for message_id, data in messages:
                        try:
                            raw = data.get("payload", "{}")
                            envelope = Envelope.from_json(
                                raw if isinstance(raw, str) else raw.decode()
                            )
                            await self._handle_outgoing(
                                envelope=envelope,
                                long_term_store=self._long_term,
                            )
                        except Exception as inner_exc:
                            logger.error(
                                "Failed to process outgoing message %s: %s",
                                message_id,
                                inner_exc,
                            )
                        finally:
                            await redis_conn.xack(
                                _stream, outgoing_group, message_id
                            )

            except Exception as exc:
                logger.error("Outgoing stream error: %s", exc)
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Démarre la brique Souvenir.

        Initialise les tables SQLite, obtient la connexion Redis et lance les
        deux boucles de consommation en parallèle via ``asyncio.gather``.

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
            await asyncio.gather(
                self._process_request_stream(redis_conn, shutdown=shutdown),
                self._process_outgoing_streams(redis_conn, shutdown=shutdown),
            )
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
