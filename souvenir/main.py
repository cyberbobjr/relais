"""Brique Souvenir — gestion de la mémoire court et long terme."""

import asyncio
import json
import logging
import sys

from common.redis_client import RedisClient
from souvenir.context_store import ContextStore
from souvenir.long_term_store import LongTermStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("souvenir")


class Souvenir:
    """Brique mémoire : court terme (Redis) et long terme (SQLite/SQLModel).

    Consomme le stream ``relais:memory:request`` et traite trois actions :

    * ``append``       — ajoute un message dans l'historique court terme.
    * ``get``          — retourne l'historique court terme au Workshop.
    * ``store_memory`` — persiste un souvenir long terme dans SQLite.
    """

    def __init__(self) -> None:
        """Initialise les streams Redis et les stores mémoire."""
        self.client = RedisClient("souvenir")
        self.stream_req = "relais:memory:request"
        self.stream_res = "relais:memory:response"
        self.group_name = "souvenir_group"
        self.consumer_name = "souvenir_1"
        self._long_term = LongTermStore()

    async def _process_stream(self, redis_conn) -> None:  # type: ignore[no-untyped-def]
        """Consume memory requests from Workshop.

        Traite les actions ``append``, ``get`` et ``store_memory`` reçues sur
        ``relais:memory:request``. Acquitte (XACK) chaque message après
        traitement, quelle que soit l'issue.

        Args:
            redis_conn: Connexion Redis async (redis.asyncio.Redis).

        Returns:
            None
        """
        context_store = ContextStore(redis=redis_conn)

        try:
            await redis_conn.xgroup_create(
                self.stream_req, self.group_name, mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                logger.warning("Consumer group error: %s", exc)

        logger.info("Memory listening to requests...")

        while True:
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

                            if action == "append":
                                role = req.get("role", "user")
                                content = req.get("message", "")
                                await context_store.append(session_id, role, content)
                                logger.debug(
                                    "Appended message to session %s", session_id
                                )

                            elif action == "get":
                                history = await context_store.get(session_id)
                                res = {
                                    "correlation_id": correlation_id,
                                    "history": history,
                                }
                                await redis_conn.xadd(
                                    self.stream_res, {"payload": json.dumps(res)}
                                )
                                logger.info(
                                    "Provided context history for session %s", session_id
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
                logger.error("Stream error: %s", exc)
                await asyncio.sleep(1)

    async def start(self) -> None:
        """Démarre la brique Souvenir.

        Obtient la connexion Redis, initialise les tables SQLite si nécessaire
        (dev/test uniquement — en production, utiliser ``alembic upgrade head``),
        puis entre dans la boucle de traitement des streams.

        Returns:
            None
        """
        redis_conn = await self.client.get_connection()
        await redis_conn.xadd(
            "relais:logs",
            {"level": "INFO", "brick": "souvenir", "message": "Souvenir started"},
        )
        try:
            logger.warning(
                "Initialising SQLite schema via _create_tables() — "
                "run 'alembic upgrade head' in production instead."
            )
            await self._long_term._create_tables()
            await self._process_stream(redis_conn)
        except asyncio.CancelledError:
            logger.info("Souvenir shutting down...")
        finally:
            await self._long_term.close()
            await self.client.close()


if __name__ == "__main__":
    from pathlib import Path
    from common.init import initialize_user_dir
    initialize_user_dir(Path(__file__).parent.parent)
    souvenir = Souvenir()
    try:
        asyncio.run(souvenir.start())
    except KeyboardInterrupt:
        pass
