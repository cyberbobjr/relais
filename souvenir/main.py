"""Brique Souvenir — gestion de la mémoire court et long terme.

Architecture dual-stream :

* ``relais:memory:request``          — requêtes ``get`` depuis Atelier.
* ``relais:messages:outgoing:{ch}``  — observation des réponses pour archivage
                                        et extraction de faits.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any

from atelier.profile_loader import load_profiles, resolve_profile
from common.envelope import Envelope
from common.redis_client import RedisClient
from common.shutdown import GracefulShutdown
from souvenir.context_store import ContextStore
from souvenir.long_term_store import LongTermStore
from souvenir.memory_extractor import MemoryExtractor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("souvenir")

# Canaux dont les streams sortants sont observés.
_DEFAULT_CHANNELS = ["discord", "telegram"]

# Modèle de secours si le profil memory_extractor ne peut pas être chargé.
_FALLBACK_EXTRACTION_MODEL = "glm-4.7-flash"


class Souvenir:
    """Brique mémoire : court terme (Redis) et long terme (SQLite/SQLModel).

    Consomme deux familles de streams :

    1. ``relais:memory:request`` — action ``get`` : retourne l'historique.
    2. ``relais:messages:outgoing:{channel}`` — observe les réponses sortantes
       pour mettre à jour le contexte Redis, archiver dans SQLite et extraire
       des faits utilisateur.
    """

    def __init__(self) -> None:
        """Initialise les streams Redis, les stores mémoire et l'extracteur."""
        self.client = RedisClient("souvenir")
        self.stream_req = "relais:memory:request"
        self.stream_res = "relais:memory:response"
        self.group_name = "souvenir_group"
        self.consumer_name = "souvenir_1"
        self._long_term = LongTermStore()
        litellm_url = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
        try:
            _profiles = load_profiles()
            _extraction_profile = resolve_profile(_profiles, "memory_extractor")
            extraction_model: str = _extraction_profile.model
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not load 'memory_extractor' profile from profiles.yaml "
                "(falling back to %s): %s",
                _FALLBACK_EXTRACTION_MODEL,
                exc,
            )
            extraction_model = _FALLBACK_EXTRACTION_MODEL
        self._extractor = MemoryExtractor(litellm_url=litellm_url, model=extraction_model)
        self._channels: list[str] = _DEFAULT_CHANNELS

    # ------------------------------------------------------------------
    # Public handler methods (testable without a running Redis)
    # ------------------------------------------------------------------

    async def _handle_get_request(
        self,
        redis_conn: Any,
        context_store: ContextStore,
        long_term_store: LongTermStore,
        session_id: str,
        correlation_id: str,
    ) -> None:
        """Répond à une requête ``get`` en retournant l'historique de session.

        Essaie d'abord le cache Redis (``context_store.get_recent``). Si vide,
        utilise le fallback SQLite (``long_term_store.get_recent_messages``).
        Publie la réponse sur ``relais:memory:response``.

        Args:
            redis_conn: Connexion Redis async.
            context_store: Store court terme Redis.
            long_term_store: Store long terme SQLite.
            session_id: Identifiant de la session à récupérer.
            correlation_id: ID de corrélation à inclure dans la réponse.
        """
        messages = await context_store.get_recent(session_id, limit=20)
        if not messages:
            messages = await long_term_store.get_recent_messages(session_id, limit=20)
            if messages:
                logger.debug("Redis cache miss for session %s — using SQLite", session_id)

        payload = {
            "correlation_id": correlation_id,
            "messages": messages,
        }
        await redis_conn.xadd(self.stream_res, {"payload": json.dumps(payload)})
        logger.info("Provided context for session %s (%d msgs)", session_id, len(messages))

    async def _handle_outgoing(
        self,
        envelope: Envelope,
        context_store: ContextStore,
        long_term_store: LongTermStore,
        memory_extractor: MemoryExtractor,
    ) -> None:
        """Traite un message sortant : contexte, archivage et extraction de faits.

        Séquence :
        1. Appends the user+assistant turn pair to the Redis context cache.
        2. Archives both messages to SQLite for long-term persistence.
        3. Extracts durable user facts via LLM (fire-and-forget, non-blocking).
        4. Upserts extracted facts into SQLite.

        Args:
            envelope: L'enveloppe du message sortant (réponse de l'assistant).
            context_store: Store court terme Redis.
            long_term_store: Store long terme SQLite.
            memory_extractor: Extracteur de faits utilisateur.
        """
        user_message = envelope.metadata.get("user_message", "")

        await context_store.append_turn(
            session_id=envelope.session_id,
            user_content=user_message,
            assistant_content=envelope.content,
        )

        await long_term_store.archive(envelope)

        try:
            facts = await memory_extractor.extract(envelope)
            if facts:
                await long_term_store.upsert_facts(envelope.sender_id, facts)
                logger.debug(
                    "Upserted %d facts for sender=%s", len(facts), envelope.sender_id
                )
        except Exception as exc:
            logger.warning("Memory extraction/upsert error (non-blocking): %s", exc)

    # ------------------------------------------------------------------
    # Internal consumer loops
    # ------------------------------------------------------------------

    async def _process_request_stream(
        self,
        redis_conn: Any,
        context_store: ContextStore,
        shutdown: GracefulShutdown | None = None,
    ) -> None:
        """Consomme ``relais:memory:request`` et répond aux actions ``get``.

        Supprime l'ancienne action ``append`` (désormais gérée par
        ``_process_outgoing_streams``). L'action ``store_memory`` est
        conservée pour compatibilité avec les bricks existants.

        Exits cleanly when ``shutdown.is_stopping()`` returns True.

        Args:
            redis_conn: Connexion Redis async.
            context_store: Store court terme Redis.
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
                            req = json.loads(data.get("payload", "{}"))
                            action = req.get("action")
                            session_id = req.get("session_id", "")
                            correlation_id = req.get("correlation_id")

                            if action == "get":
                                await self._handle_get_request(
                                    redis_conn=redis_conn,
                                    context_store=context_store,
                                    long_term_store=self._long_term,
                                    session_id=session_id,
                                    correlation_id=correlation_id,
                                )

                            elif action == "store_memory":
                                user_id = req.get("user_id", session_id)
                                key = req.get("key", "")
                                value = req.get("value", "")
                                source = req.get("source", "manual")
                                await self._long_term.store(user_id, key, value, source)
                                logger.info(
                                    "Stored long-term memory for user=%s key=%s",
                                    user_id,
                                    key,
                                )

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
        context_store: ContextStore,
        shutdown: GracefulShutdown | None = None,
    ) -> None:
        """Observe ``relais:messages:outgoing:{channel}`` pour tous les canaux connus.

        Crée un consumer group par canal (idempotent) puis entre dans une
        boucle de lecture. Chaque message est désérialisé en ``Envelope`` et
        traité par ``_handle_outgoing``.

        Exits cleanly when ``shutdown.is_stopping()`` returns True.

        Args:
            redis_conn: Connexion Redis async.
            context_store: Store court terme Redis.
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
                                context_store=context_store,
                                long_term_store=self._long_term,
                                memory_extractor=self._extractor,
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
        context_store = ContextStore(redis=redis_conn)
        try:
            logger.warning(
                "Initialising SQLite schema via _create_tables() — "
                "run 'alembic upgrade head' in production instead."
            )
            await self._long_term._create_tables()
            await asyncio.gather(
                self._process_request_stream(redis_conn, context_store, shutdown=shutdown),
                self._process_outgoing_streams(redis_conn, context_store, shutdown=shutdown),
            )
        except asyncio.CancelledError:
            logger.info("Souvenir shutting down...")
        finally:
            await self._long_term.close()
            await self.client.close()
            logger.info("Souvenir stopped gracefully")


if __name__ == "__main__":
    from pathlib import Path

    from common.init import initialize_user_dir

    initialize_user_dir(Path(__file__).parent.parent)
    souvenir = Souvenir()
    try:
        asyncio.run(souvenir.start())
    except KeyboardInterrupt:
        pass
