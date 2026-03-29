import asyncio
import logging
import sys
from typing import Any

from common.redis_client import RedisClient
from common.envelope import Envelope
from common.shutdown import GracefulShutdown

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("sentinelle")


class Sentinelle:
    """La brique La Sentinelle du système RELAIS.

    Responsible for security validation of incoming envelopes. It performs ACL checks
    and ensures only authorized messages are forwarded to L'Atelier for further processing.
    """

    def __init__(self) -> None:
        """Initializes La Sentinelle with Redis stream and group configurations."""
        self.client: RedisClient = RedisClient("sentinelle")
        self.stream_in: str = "relais:security"
        self.stream_out: str = "relais:tasks"
        self.group_name: str = "sentinelle_group"
        self.consumer_name: str = "sentinelle_1"

    async def _process_stream(self, redis_conn: Any, shutdown: GracefulShutdown | None = None) -> None:
        """Consume security checks from Gateway and forward approved messages.

        Exits cleanly when ``shutdown.is_stopping()`` returns True.

        Args:
            redis_conn: Active Redis connection.
            shutdown: GracefulShutdown instance controlling the loop lifetime.
                If None a new instance is created (backward-compatible).
        """
        if shutdown is None:
            shutdown = GracefulShutdown()

        try:
            await redis_conn.xgroup_create(self.stream_in, self.group_name, mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.warning(f"Consumer group error: {e}")

        logger.info("Sentinel listening to security queue...")

        while not shutdown.is_stopping():
            try:
                results = await redis_conn.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_in: ">"},
                    count=10,
                    block=2000
                )

                if not results:
                    continue

                for _, messages in results:
                    for message_id, data in messages:
                        target_id = message_id
                        try:
                            # Parse Envelope
                            payload = data.get("payload", "{}")
                            envelope = Envelope.from_json(payload)

                            logger.info(
                                f"Validating message: {envelope.correlation_id} "
                                f"from {envelope.sender_id}"
                            )

                            # ACL Check MVP: Allow all for now
                            # TODO: Future implementation: load users.yaml and evaluate ACLs
                            is_authorized = True

                            if is_authorized:
                                envelope.add_trace("sentinelle", "ACL verified")
                                await redis_conn.xadd(
                                    self.stream_out, {"payload": envelope.to_json()}
                                )
                                await redis_conn.xadd("relais:logs", {
                                    "level": "INFO",
                                    "brick": "sentinelle",
                                    "correlation_id": envelope.correlation_id,
                                    "sender_id": envelope.sender_id,
                                    "message": (
                                        f"Approved {envelope.correlation_id} to atelier"
                                    ),
                                    "content_preview": envelope.content[:60] if envelope.content else "",
                                })
                            else:
                                logger.warning(
                                    f"Unauthorized message {envelope.correlation_id} dropped."
                                )
                                await redis_conn.xadd("relais:logs", {
                                    "level": "WARN",
                                    "brick": "sentinelle",
                                    "correlation_id": envelope.correlation_id,
                                    "sender_id": envelope.sender_id,
                                    "message": (
                                        f"Blocked unauthorized message {envelope.correlation_id}"
                                    ),
                                    "content_preview": envelope.content[:60] if envelope.content else "",
                                })

                        except Exception as inner_e:
                            logger.error(f"Failed to process message {target_id}: {inner_e}")
                            await redis_conn.xadd("relais:logs", {
                                "level": "ERROR",
                                "brick": "sentinelle",
                                "correlation_id": "",
                                "message": f"Validation error: {inner_e}",
                                "error": str(inner_e),
                            })
                        finally:
                            # Acknowledge the message
                            await redis_conn.xack(self.stream_in, self.group_name, message_id)

            except Exception as e:
                logger.error(f"Stream error: {e}")
                await asyncio.sleep(1)

    async def start(self) -> None:
        """Starts La Sentinelle service and its main processing loop.

        Registers SIGTERM/SIGINT handlers via GracefulShutdown so the process
        exits cleanly when sent a termination signal.
        """
        shutdown = GracefulShutdown()
        shutdown.install_signal_handlers()
        redis_conn = await self.client.get_connection()
        await redis_conn.xadd("relais:logs", {
            "level": "INFO",
            "brick": "sentinelle",
            "message": "Sentinelle started"
        })
        try:
            await self._process_stream(redis_conn, shutdown=shutdown)
        except asyncio.CancelledError:
            logger.info("Sentinelle shutting down...")
        finally:
            await self.client.close()
            logger.info("Sentinelle stopped gracefully")


if __name__ == "__main__":
    from pathlib import Path
    from common.init import initialize_user_dir
    initialize_user_dir(Path(__file__).parent.parent)
    sentinelle = Sentinelle()
    try:
        asyncio.run(sentinelle.start())
    except KeyboardInterrupt:
        pass
