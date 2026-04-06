import asyncio
import logging
from typing import Any

from commandant.commands import COMMAND_REGISTRY, parse_command
from common.envelope import Envelope
from common.redis_client import RedisClient
from common.shutdown import GracefulShutdown
from common.streams import STREAM_COMMANDS, STREAM_LOGS

logger = logging.getLogger("commandant")


class Commandant:
    """Brique Le Commandant — interprète les commandes globales hors-LLM.

    Consomme relais:messages:incoming en parallèle avec Le Portail via
    son propre consumer group (commandant_group). Traite les commandes
    connues et ACK tous les messages (commandes ou non).
    """

    def __init__(self) -> None:
        self.client: RedisClient = RedisClient("commandant")
        self.stream_in: str = STREAM_COMMANDS
        self.group_name: str = "commandant_group"
        self.consumer_name: str = "commandant_1"

    async def _process_stream(
        self,
        redis_conn: Any,
        shutdown: GracefulShutdown | None = None,
    ) -> None:
        """Boucle principale de consommation.

        Args:
            redis_conn: Connexion Redis async active.
            shutdown: Instance GracefulShutdown. Si None, une nouvelle est créée.
        """
        if shutdown is None:
            shutdown = GracefulShutdown()

        try:
            await redis_conn.xgroup_create(
                self.stream_in, self.group_name, mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                logger.warning("Consumer group error: %s", exc)

        logger.info("Commandant listening on %s ...", self.stream_in)

        while not shutdown.is_stopping():
            try:
                results = await redis_conn.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_in: ">"},
                    count=10,
                    block=2000,
                )

                if not results:
                    continue

                for _, messages in results:
                    for message_id, data in messages:
                        try:
                            payload = data.get(b"payload") or data.get("payload", "{}")
                            if isinstance(payload, bytes):
                                payload = payload.decode()

                            envelope = Envelope.from_json(payload)
                            result = parse_command(envelope.content)

                            if result is not None:
                                spec = COMMAND_REGISTRY.get(result.command)
                                if spec is not None:
                                    await spec.handler(envelope, redis_conn)
                                    logger.info(
                                        "Executed command /%s for sender=%s",
                                        result.command,
                                        envelope.sender_id,
                                    )

                        except Exception as inner_exc:
                            logger.error(
                                "Failed to process message %s: %s",
                                message_id,
                                inner_exc,
                            )
                        finally:
                            await redis_conn.xack(
                                self.stream_in, self.group_name, message_id
                            )

            except Exception as exc:
                logger.error("Stream error: %s", exc)
                await asyncio.sleep(1)

    async def start(self) -> None:
        """Point d'entrée de la brique Commandant.

        Installe les handlers de signal SIGTERM/SIGINT et démarre la boucle.
        """
        shutdown = GracefulShutdown()
        shutdown.install_signal_handlers()
        redis_conn = await self.client.get_connection()
        await redis_conn.xadd(STREAM_LOGS, {
            "level": "INFO",
            "brick": "commandant",
            "message": "Commandant started",
        })
        try:
            await self._process_stream(redis_conn, shutdown=shutdown)
        except asyncio.CancelledError:
            logger.info("Commandant shutting down...")
        finally:
            await self.client.close()
            logger.info("Commandant stopped gracefully")


if __name__ == "__main__":
    from common.init import initialize_user_dir
    initialize_user_dir()
    commandant = Commandant()
    try:
        asyncio.run(commandant.start())
    except KeyboardInterrupt:
        pass
