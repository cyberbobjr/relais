"""Portail brick — user identity resolution and envelope enrichment.

Functional role
---------------
First processing stage after channel ingestion.  Validates each incoming
Envelope, resolves the sender's identity from users.yaml via ``UserRegistry``,
and enriches the envelope's metadata before forwarding it to Sentinelle for
ACL enforcement.  Unknown senders are handled according to the configured
``unknown_user_policy`` (reject or allow as guest).

Technical overview
------------------
``Portail`` is a single asyncio consumer loop.  Key helpers:

* ``UserRegistry`` — loads and caches user records from users.yaml;
  resolves ``sender_id`` → ``UserRecord``.
* ``_enrich_envelope`` — writes the canonical ``user_record`` dict and
  derived fields (``display_name``, ``user_role``, ``llm_profile``,
  ``custom_prompt_path``) into ``envelope.metadata``.
* ``_apply_guest_stamps`` — stamps minimal guest metadata when the sender
  is unknown and the guest policy is "allow".
* ``_update_active_sessions`` — maintains a Redis Hash used by Crieur to
  push proactive notifications to active users.

Redis channels
--------------
Consumed:
  - relais:messages:incoming  (consumer group: portail_group)

Produced:
  - relais:security           — enriched envelopes forwarded to Sentinelle
  - relais:logs               — operational log entries

Redis keys written:
  - relais:active_sessions:{sender_id}  (Hash, TTL 1 h)

Processing flow
---------------
  (1) Consume from relais:messages:incoming (portail_group).
  (2) Deserialize Envelope from JSON payload.
  (3) Resolve sender via UserRegistry.
  (4) Apply unknown_user_policy: drop silently or stamp guest metadata.
  (5) Enrich envelope.metadata with user_record, display_name, user_role,
      llm_profile (resolved from channel_profile), custom_prompt_path.
  (6) Update relais:active_sessions:{sender_id} hash.
  (7) Forward enriched envelope to relais:security.
  (8) XACK the message (unconditional — validation errors are logged and
      dropped, never left in PEL).
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
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
from common.user_record import UserRecord
from portail.user_registry import UserRegistry

logger = logging.getLogger("portail")


class Portail:
    """La brique Le Portail du système RELAIS.

    Responsible for consuming incoming messages from external relays, enriching
    envelopes with a single ``user_record`` dict (resolved from portail.yaml),
    and forwarding them to La Sentinelle for security validation.

    The ``user_record`` dict in ``envelope.metadata`` is the sole carrier of
    user identity and role data for all downstream bricks.
    """

    def __init__(self) -> None:
        """Initialise Le Portail with Redis stream and group configurations."""
        self.client: RedisClient = RedisClient("portail")
        self.stream_in: str = "relais:messages:incoming"
        self.stream_out: str = "relais:security"
        self.group_name: str = "portail_group"
        self.consumer_name: str = "portail_1"
        self._user_registry: UserRegistry = UserRegistry()
        self._guest_profile: str = self._user_registry.guest_profile
        self._unknown_user_policy: str = self._user_registry.unknown_user_policy
        logger.info(
            "Portail: unknown_user_policy=%s, guest_profile=%s",
            self._unknown_user_policy,
            self._guest_profile,
        )

    def _enrich_envelope(self, envelope: "Envelope") -> None:
        """Stamp a single ``user_record`` dict into envelope.metadata.

        Resolves the sender against the UserRegistry.  When found, writes the
        fully-merged ``UserRecord.to_dict()`` under ``envelope.metadata["user_record"]``.
        Unknown users produce no ``user_record`` key — the caller
        (``_process_stream``) handles them via the configured policy.

        The ``llm_profile`` field inside ``user_record`` is resolved as:
        ``channel_profile`` (Aiguilleur) > user/role level > ``"default"``.

        Legacy top-level keys (``user_role``, ``display_name``, ``skills_dirs``,
        ``custom_prompt_path``, ``allowed_mcp_tools``) are NOT written.

        Args:
            envelope: The incoming envelope to enrich in place.
        """
        record : UserRecord | None = self._user_registry.resolve_user(
            sender_id=envelope.sender_id,
            channel=envelope.channel,
        )
        if record is None:
            return

        # llm_profile: channel_profile (Aiguilleur) overrides user/role value
        channel_profile: str | None = envelope.metadata.get("channel_profile")
        effective_llm_profile: str = (
            channel_profile if channel_profile else record.llm_profile
        )

        # Build the user_record dict from the resolved record, overriding
        # llm_profile with the channel_profile when present.
        user_record_dict = record.to_dict()
        user_record_dict["llm_profile"] = effective_llm_profile

        envelope.metadata["user_record"] = user_record_dict

    def _apply_guest_stamps(self, envelope: "Envelope") -> None:
        """Stamp a synthetic guest ``user_record`` dict onto an unknown-user envelope.

        Applied when ``unknown_user_policy=guest`` and ``resolve_user()``
        returned ``None``.  Uses ``UserRegistry.build_guest_record()`` so that
        role-level fields (actions, skills_dirs, etc.) are taken from the
        ``guest`` role config.

        Args:
            envelope: The incoming envelope to enrich in place.
        """
        guest_llm_profile = self._guest_profile
        channel_profile: str | None = envelope.metadata.get("channel_profile")
        effective_llm_profile: str = channel_profile if channel_profile else guest_llm_profile

        guest_record = self._user_registry.build_guest_record(llm_profile=effective_llm_profile)
        envelope.metadata["user_record"] = guest_record.to_dict()

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
            - ``display_name``: Present only when available in ``user_record``.

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

        user_record_dict: dict[str, Any] = envelope.metadata.get("user_record") or {}
        display_name: str = str(user_record_dict.get("display_name") or "")
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

    async def _process_stream(self, redis_conn: Any, shutdown: GracefulShutdown | None = None) -> None:
        """Consume incoming messages from Relays and forward to Sentinelle.

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

                            # Enrich envelope with single user_record dict
                            self._enrich_envelope(envelope)

                            # Apply unknown-user policy when user_record is absent
                            if "user_record" not in envelope.metadata:
                                policy = self._unknown_user_policy
                                if policy == "guest":
                                    self._apply_guest_stamps(envelope)
                                elif policy == "pending":
                                    logger.info(
                                        "unknown_user_policy=pending — publishing %s to pending_users",
                                        envelope.sender_id,
                                    )
                                    await redis_conn.xadd(
                                        "relais:admin:pending_users",
                                        {
                                            "sender_id": envelope.sender_id,
                                            "channel": envelope.channel,
                                            "correlation_id": envelope.correlation_id,
                                            "timestamp": str(envelope.timestamp),
                                        },
                                    )
                                    continue  # drop — finally:xack executes
                                else:
                                    # deny (default) — drop silently
                                    logger.info(
                                        "unknown_user_policy=deny — dropping message from %s",
                                        envelope.sender_id,
                                    )
                                    continue  # drop — finally:xack executes

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
        """Start Le Portail service and its main processing loop.

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
