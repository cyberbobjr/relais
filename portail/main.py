import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from common.redis_client import RedisClient
from common.envelope import Envelope

# Configure logging to standard output
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("portail")


class Portail:
    """La brique Le Portail du système RELAIS.

    Responsible for consuming incoming messages from external relays (e.g., Discord),
    updating session mappings, and forwarding them to La Sentinelle for security validation.
    """

    def __init__(self) -> None:
        """Initializes Le Portail with default stream and group configurations."""
        self.client: RedisClient = RedisClient("portail")
        self.stream_in: str = "relais:messages:incoming"
        self.stream_out: str = "relais:security"
        self.group_name: str = "portail_group"
        self.consumer_name: str = "portail_1"

    async def _update_session(self, redis_conn: Any, user_id: str, channel: str) -> None:
        """Update active session TTL to route response appropriately.

        Args:
            redis_conn: Active Redis connection.
            user_id: ID of the user initiating the session.
            channel: Communication channel name.
        """
        key = f"relais:active_sessions:{user_id}"
        await redis_conn.hset(key, channel, datetime.now(timezone.utc).timestamp())
        await redis_conn.expire(key, 3600)  # TTL: 1 hour

    async def _process_stream(self, redis_conn: Any) -> None:
        """Consume incoming messages from Relays and forward to Sentinel.

        Args:
            redis_conn: Active Redis connection.
        """
        try:
            await redis_conn.xgroup_create(self.stream_in, self.group_name, mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.warning(f"Consumer group error: {e}")

        logger.info("Gateway listening to incoming messages...")

        while True:
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
                                f"Received message: {envelope.correlation_id} "
                                f"from {envelope.channel}"
                            )

                            # Update session mapping
                            await self._update_session(
                                redis_conn, envelope.sender_id, envelope.channel
                            )

                            # Add trace
                            envelope.add_trace("portail", "received and session updated")

                            # Forward to La Sentinelle
                            await redis_conn.xadd(
                                self.stream_out, {"payload": envelope.to_json()}
                            )

                            # Log to Redis stream
                            await redis_conn.xadd("relais:logs", {
                                "level": "INFO",
                                "brick": "portail",
                                "message": f"Forwarded {envelope.correlation_id} to sentinelle"
                            })

                        except Exception as inner_e:
                            logger.error(f"Failed to process message {target_id}: {inner_e}")
                            await redis_conn.xadd("relais:logs", {
                                "level": "ERROR",
                                "brick": "portail",
                                "message": f"Malformed envelope error: {inner_e}"
                            })
                        finally:
                            # Acknowledge the message
                            await redis_conn.xack(self.stream_in, self.group_name, message_id)

            except Exception as e:
                logger.error(f"Stream error: {e}")
                await asyncio.sleep(1)

    async def start(self) -> None:
        """Starts Le Portail service and its main processing loop."""
        redis_conn = await self.client.get_connection()
        await redis_conn.xadd("relais:logs", {
            "level": "INFO",
            "brick": "portail",
            "message": "Portail started"
        })
        try:
            await self._process_stream(redis_conn)
        except asyncio.CancelledError:
            logger.info("Portail shutting down...")
        finally:
            await self.client.close()


if __name__ == "__main__":
    from pathlib import Path
    from common.init import initialize_user_dir
    initialize_user_dir(Path(__file__).parent.parent)
    portail = Portail()
    try:
        asyncio.run(portail.start())
    except KeyboardInterrupt:
        pass
