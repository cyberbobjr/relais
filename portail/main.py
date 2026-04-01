import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

# Configure logging to standard output
_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout
)

from common.redis_client import RedisClient
from common.envelope import Envelope
from common.shutdown import GracefulShutdown
from common.text_utils import strip_outer_quotes
from common.user_registry import UserRegistry
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
        self._dnd_cached: bool | None = None
        self._dnd_cache_at: float = 0.0
        self._user_registry: UserRegistry = UserRegistry()

    def _enrich_envelope(self, envelope: "Envelope") -> None:
        """Stamp user identity and LLM profile metadata onto the envelope.

        Resolves the sender against the UserRegistry and writes known fields
        into ``envelope.metadata`` in place.  Unknown users are silently
        skipped — their identity fields are simply absent, which lets the
        Sentinelle decide what to do.

        Fields always written:
            - ``llm_profile``: resolved from ``channel_profile`` metadata key
              (stamped upstream by the Aiguilleur) or ``"default"`` when absent
              or ``None``.

        Fields written only for known users:
            - ``user_role``: the user's role (e.g. ``"admin"``, ``"user"``).
            - ``display_name``: human-readable name.
            - ``custom_prompt_path``: per-user prompt override path, only
              written when the registry value is not ``None``.

        Args:
            envelope: The incoming envelope to enrich in place.
        """
        # llm_profile resolution: channel_profile > "default"
        channel_profile = envelope.metadata.get("channel_profile")
        envelope.metadata["llm_profile"] = channel_profile if channel_profile else "default"

        # Identity enrichment — skip silently when user not found
        record = self._user_registry.resolve_user(
            sender_id=envelope.sender_id,
            channel=envelope.channel,
        )
        if record is None:
            return

        envelope.metadata["user_role"] = record.role
        envelope.metadata["display_name"] = record.display_name
        if record.custom_prompt_path is not None:
            envelope.metadata["custom_prompt_path"] = record.custom_prompt_path

    async def _update_active_sessions(self, redis_conn: Any, envelope: "Envelope") -> None:
        """Track active sessions per user for the Crieur (push notifications).

        Stores user activity metadata in a Redis Hash with a 1-hour TTL.
        This method is fire-and-forget: any Redis failure is logged as a
        warning and swallowed so the main message pipeline is never blocked.

        Key: ``relais:active_sessions:{sender_id}``

        Fields written:
            - ``last_seen``: Current epoch timestamp as a float string.
            - ``channel``: The originating channel (e.g. "discord").
            - ``session_id``: The envelope session identifier.
            - ``display_name``: Present only when ``envelope.metadata`` contains
              a non-empty ``display_name`` value.

        Args:
            redis_conn: Active async Redis connection.
            envelope: The validated incoming envelope whose fields are persisted.
        """
        key = f"relais:active_sessions:{envelope.sender_id}"
        mapping: dict[str, Any] = {
            "last_seen": str(datetime.now(timezone.utc).timestamp()),
            "channel": envelope.channel,
            "session_id": envelope.session_id,
        }

        display_name: str = envelope.metadata.get("display_name", "")
        if display_name:
            mapping["display_name"] = display_name

        try:
            await redis_conn.hset(key, mapping=mapping)
            await redis_conn.expire(key, 3600)
        except Exception as exc:
            logger.warning(
                "Failed to update active_session for %s: %s",
                envelope.sender_id,
                exc,
            )

    @staticmethod
    def _is_command(content: str) -> bool:
        """Retourne True si le contenu est une commande Commandant.

        Gère les deux formes acceptées :
        - ``/clear``      — slash direct
        - ``"/clear"``    — entre guillemets doubles
        - ``'/clear'``    — entre quotes simples

        Args:
            content: Contenu brut de l'enveloppe.

        Returns:
            True si le message commence par '/' suivi d'au moins un caractère
            (après strip et dépouillage des guillemets éventuels), False sinon.
        """
        stripped = strip_outer_quotes(content)
        return stripped.startswith("/") and len(stripped) > 1

    async def _check_dnd(self, redis_conn: Any) -> bool:
        """Retourne True si le mode DND est actif, avec cache 1 seconde.

        Args:
            redis_conn: Connexion Redis async active.

        Returns:
            True si relais:state:dnd est positionné.
        """
        now = time.monotonic()
        if self._dnd_cached is None or now - self._dnd_cache_at >= 1.0:
            self._dnd_cached = bool(await redis_conn.get("relais:state:dnd"))
            self._dnd_cache_at = now
        return self._dnd_cached

    async def _process_stream(self, redis_conn: Any, shutdown: GracefulShutdown | None = None) -> None:
        """Consume incoming messages from Relays and forward to Sentinel.

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

        logger.info("Gateway listening to incoming messages...")

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
                            payload = data.get(b"payload") or data.get("payload", "{}")
                            if isinstance(payload, bytes):
                                payload = payload.decode()
                            envelope = Envelope.from_json(payload)

                            logger.info(
                                f"Received message: {envelope.correlation_id} "
                                f"from {envelope.channel}"
                            )

                            # Drop commands — handled exclusively by Commandant
                            if self._is_command(envelope.content):
                                logger.debug(
                                    "Command message — delegated to Commandant, skipping: %s",
                                    envelope.correlation_id,
                                )
                                continue  # Le finally:xack s'exécute quand même

                            # Check DND mode — drop message if active
                            dnd_active = await self._check_dnd(redis_conn)
                            if dnd_active:
                                logger.info(
                                    "DND active — dropping message %s from %s",
                                    envelope.correlation_id,
                                    envelope.sender_id,
                                )
                                continue  # Le finally:xack s'exécute quand même

                            # Enrich envelope with user identity and LLM profile
                            self._enrich_envelope(envelope)

                            # Update active session tracking
                            await self._update_active_sessions(redis_conn, envelope)

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
                                "correlation_id": envelope.correlation_id,
                                "sender_id": envelope.sender_id,
                                "message": f"Forwarded {envelope.correlation_id} to sentinelle",
                                "content_preview": envelope.content[:60] if envelope.content else "",
                            })

                        except Exception as inner_e:
                            logger.error(f"Failed to process message {target_id}: {inner_e}")
                            await redis_conn.xadd("relais:logs", {
                                "level": "ERROR",
                                "brick": "portail",
                                "correlation_id": "",
                                "message": f"Malformed envelope error: {inner_e}",
                                "error": str(inner_e),
                            })
                        finally:
                            # Acknowledge the message
                            await redis_conn.xack(self.stream_in, self.group_name, message_id)

            except Exception as e:
                logger.error(f"Stream error: {e}")
                await asyncio.sleep(1)

    async def start(self) -> None:
        """Starts Le Portail service and its main processing loop.

        Registers SIGTERM/SIGINT handlers via GracefulShutdown so the process
        exits cleanly when sent a termination signal.
        """
        shutdown = GracefulShutdown()
        shutdown.install_signal_handlers()
        redis_conn = await self.client.get_connection()
        await redis_conn.xadd("relais:logs", {
            "level": "INFO",
            "brick": "portail",
            "message": "Portail started"
        })
        try:
            await self._process_stream(redis_conn, shutdown=shutdown)
        except asyncio.CancelledError:
            logger.info("Portail shutting down...")
        finally:
            await self.client.close()
            logger.info("Portail stopped gracefully")


if __name__ == "__main__":
    from common.init import initialize_user_dir
    initialize_user_dir()
    portail = Portail()
    try:
        asyncio.run(portail.start())
    except KeyboardInterrupt:
        pass
