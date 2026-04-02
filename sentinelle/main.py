import asyncio
import logging
import os
import sys
from typing import Any

# Configure logging
_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout
)

from common.redis_client import RedisClient
from common.envelope import Envelope
from common.shutdown import GracefulShutdown
from common.command_utils import is_command, extract_command_name, KNOWN_COMMANDS
from sentinelle.acl import ACLManager

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
        self.stream_commands: str = "relais:commands"
        self.group_name: str = "sentinelle_group"
        self.consumer_name: str = "sentinelle_1"
        self.outgoing_group_name: str = "sentinelle_outgoing_group"
        self.outgoing_consumer_name: str = "sentinelle_outgoing_1"
        self._acl: ACLManager = ACLManager()

    async def _reply_inline(self, redis_conn: Any, envelope: Envelope, message: str) -> None:
        """Send a short reply directly to the channel's outgoing stream.

        Args:
            redis_conn: Active Redis connection.
            envelope: The originating envelope (used to derive channel and parent metadata).
            message: Plain-text reply content.
        """
        reply = Envelope.create_response_to(envelope, message)
        out_stream = f"relais:messages:outgoing:{envelope.channel}"
        await redis_conn.xadd(out_stream, {"payload": reply.to_json()})

    async def _handle_command(
        self,
        redis_conn: Any,
        envelope: Envelope,
        acl_context: str,
        acl_scope: str | None,
    ) -> None:
        """Route an authenticated command envelope after ACL identity check.

        Unknown commands receive an inline rejection.  Known commands are
        checked against the per-command action in the role's *actions* list
        (or the "command" wildcard); unauthorised ones get a permission
        reply, authorised ones are forwarded to ``relais:commands``.

        Args:
            redis_conn: Active Redis connection.
            envelope: The command envelope (content starts with '/').
            acl_context: Already-sanitised access context ("dm" or "group").
            acl_scope: Optional scope_id from envelope metadata.
        """
        cmd_name = extract_command_name(envelope.content)

        if cmd_name is None:
            logger.error("extract_command_name returned None for content=%r", envelope.content)
            return

        if cmd_name not in KNOWN_COMMANDS:
            await self._reply_inline(redis_conn, envelope, f"Commande inconnue : /{cmd_name}")
            logger.info("Unknown command /%s from %s — replied inline", cmd_name, envelope.sender_id)
            return

        cmd_authorized = self._acl.is_allowed(
            envelope.sender_id,
            envelope.channel,
            context=acl_context,
            scope_id=acl_scope,
            action=cmd_name,
        )
        if cmd_authorized:
            await asyncio.gather(
                redis_conn.xadd(self.stream_commands, {"payload": envelope.to_json()}),
                redis_conn.xadd("relais:logs", {
                    "level": "INFO",
                    "brick": "sentinelle",
                    "correlation_id": envelope.correlation_id,
                    "sender_id": envelope.sender_id,
                    "message": f"Routed command /{cmd_name} to relais:commands",
                }),
            )
        else:
            await self._reply_inline(
                redis_conn, envelope, f"Vous n'avez pas la permission d'exécuter /{cmd_name}"
            )
            logger.warning(
                "Unauthorised command /%s from %s — replied inline", cmd_name, envelope.sender_id
            )

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

                            # ACL check — context-aware, reads access_context/access_scope
                            # injected by Aiguilleur adapters into the envelope metadata.
                            # Sanitize: only "dm" and "group" are valid; default to "dm".
                            _raw_context = envelope.metadata.get("access_context", "dm")
                            acl_context: str = _raw_context if _raw_context in {"dm", "group"} else "dm"
                            acl_scope: str | None = envelope.metadata.get("access_scope")
                            is_authorized = self._acl.is_allowed(
                                envelope.sender_id,
                                envelope.channel,
                                context=acl_context,
                                scope_id=acl_scope,
                            )

                            if is_authorized:
                                envelope.add_trace("sentinelle", "ACL verified")

                                if is_command(envelope.content):
                                    await self._handle_command(redis_conn, envelope, acl_context, acl_scope)
                                else:
                                    await asyncio.gather(
                                        redis_conn.xadd(self.stream_out, {"payload": envelope.to_json()}),
                                        redis_conn.xadd("relais:logs", {
                                            "level": "INFO",
                                            "brick": "sentinelle",
                                            "correlation_id": envelope.correlation_id,
                                            "sender_id": envelope.sender_id,
                                            "message": f"Approved {envelope.correlation_id} to atelier",
                                            "content_preview": envelope.content[:60] if envelope.content else "",
                                        }),
                                    )
                            else:
                                logger.warning(
                                    f"Unauthorized message {envelope.correlation_id} dropped."
                                )
                                if self._acl.unknown_user_policy == "pending":
                                    await self._acl.notify_pending(
                                        redis_conn, envelope.sender_id, envelope.channel
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

    async def _process_outgoing_stream(self, redis_conn: Any, shutdown: GracefulShutdown | None = None) -> None:
        """Consume outgoing-pending messages and forward them to per-channel streams.

        Reads from the single aggregated ``relais:messages:outgoing_pending``
        stream, applies a pass-through outgoing rule (currently unconditional
        forward), and publishes to ``relais:messages:outgoing:{envelope.channel}``
        for the Aiguilleur adapter to consume.  The destination channel is read
        from the envelope — no env-var configuration needed.

        Exits cleanly when ``shutdown.is_stopping()`` returns True.

        Args:
            redis_conn: Active Redis connection.
            shutdown: GracefulShutdown instance controlling the loop lifetime.
                If None a new instance is created (backward-compatible).
        """
        if shutdown is None:
            shutdown = GracefulShutdown()

        stream_pending = "relais:messages:outgoing_pending"

        try:
            await redis_conn.xgroup_create(stream_pending, self.outgoing_group_name, mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.warning(f"Outgoing consumer group error: {e}")

        logger.info("Sentinelle listening to outgoing_pending...")

        while not shutdown.is_stopping():
            try:
                results = await redis_conn.xreadgroup(
                    self.outgoing_group_name,
                    self.outgoing_consumer_name,
                    {stream_pending: ">"},
                    count=10,
                    block=2000,
                )

                if not results:
                    continue

                for _, messages in results:
                    for message_id, data in messages:
                        target_id = message_id
                        try:
                            payload = data.get("payload", "{}")
                            envelope = Envelope.from_json(payload)

                            stream_out = f"relais:messages:outgoing:{envelope.channel}"
                            logger.debug(
                                f"Outgoing pass-through: {envelope.correlation_id} "
                                f"→ {stream_out}"
                            )

                            # Outgoing rule — currently a pass-through.
                            # Future: apply output content policy here.
                            envelope.add_trace("sentinelle", "outgoing pass-through")
                            await redis_conn.xadd(stream_out, {"payload": envelope.to_json()})

                        except Exception as inner_e:
                            logger.error(
                                f"Failed to process outgoing message {target_id}: {inner_e}"
                            )
                            await redis_conn.xadd("relais:logs", {
                                "level": "ERROR",
                                "brick": "sentinelle",
                                "correlation_id": "",
                                "message": f"Outgoing validation error: {inner_e}",
                                "error": str(inner_e),
                            })
                        finally:
                            await redis_conn.xack(stream_pending, self.outgoing_group_name, message_id)

            except Exception as e:
                logger.error(f"Outgoing stream error: {e}")
                await asyncio.sleep(1)

    async def start(self) -> None:
        """Starts La Sentinelle service and its main processing loops.

        Runs two concurrent loops:
        - Incoming: ``relais:security`` → ACL check → ``relais:tasks``
        - Outgoing: ``relais:messages:outgoing_pending`` → pass-through
          → ``relais:messages:outgoing:{envelope.channel}``

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
            await asyncio.gather(
                self._process_stream(redis_conn, shutdown=shutdown),
                self._process_outgoing_stream(redis_conn, shutdown=shutdown),
            )
        except asyncio.CancelledError:
            logger.info("Sentinelle shutting down...")
        finally:
            await self.client.close()
            logger.info("Sentinelle stopped gracefully")


if __name__ == "__main__":
    from common.init import initialize_user_dir
    initialize_user_dir()
    sentinelle = Sentinelle()
    try:
        asyncio.run(sentinelle.start())
    except KeyboardInterrupt:
        pass
