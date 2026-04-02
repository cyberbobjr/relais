import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Configure logging to standard output
_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout
)

from common.config_loader import resolve_config_path
from common.redis_client import RedisClient
from common.envelope import Envelope
from common.role_registry import RoleRegistry
from common.shutdown import GracefulShutdown
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
        self._user_registry: UserRegistry = UserRegistry()
        self._role_registry: RoleRegistry = RoleRegistry()
        self._load_security_config()

    def _load_security_config(self) -> None:
        """Load security policy settings from config.yaml.

        Reads ``security.unknown_user_policy`` and ``security.guest_profile``
        from the config cascade.  Falls back to fail-closed defaults
        (``deny`` / ``fast``) when the config file is absent or the keys
        are missing.

        Returns: None
        """
        try:
            config_path: Path = resolve_config_path("config.yaml")
            raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            security = raw.get("security") or {}
            policy = str(security.get("unknown_user_policy") or "deny").lower()
            guest_profile = str(security.get("guest_profile") or "fast")
        except FileNotFoundError:
            logger.warning(
                "Portail: config.yaml not found — defaulting unknown_user_policy=deny"
            )
            policy = "deny"
            guest_profile = "fast"
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Portail: failed to parse config.yaml (%s) — defaulting unknown_user_policy=deny",
                exc,
            )
            policy = "deny"
            guest_profile = "fast"

        if policy not in ("deny", "guest", "pending"):
            logger.warning(
                "Portail: unknown unknown_user_policy=%r — falling back to 'deny'",
                policy,
            )
            policy = "deny"

        self._unknown_user_policy: str = policy
        self._guest_profile: str = guest_profile
        logger.info(
            "Portail: unknown_user_policy=%s, guest_profile=%s",
            self._unknown_user_policy,
            self._guest_profile,
        )

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
            - ``skills_dirs``: list of skill directory names the role allows
              (``["*"]`` = unrestricted, ``[]`` = no skills).
            - ``allowed_mcp_tools``: list of permitted MCP tool identifiers
              (``["*"]`` = unrestricted, ``[]`` = no MCP tools).

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
        # Skill and MCP tool injection — stamp from role config (fail-closed:
        # unknown role → empty lists, never absent keys).
        role_cfg = self._role_registry.get_role(record.role)
        if role_cfg is not None:
            envelope.metadata["skills_dirs"] = list(role_cfg.skills_dirs)
            envelope.metadata["allowed_mcp_tools"] = list(role_cfg.allowed_mcp_tools)
        else:
            logger.warning("Unknown role %r for user %s — stamping empty access", record.role, record.display_name)
            envelope.metadata["skills_dirs"] = []
            envelope.metadata["allowed_mcp_tools"] = []

        # custom_prompt_path: user-level override takes priority; fallback to role-level prompt
        if record.custom_prompt_path is not None:
            envelope.metadata["custom_prompt_path"] = record.custom_prompt_path
        elif role_cfg is not None and role_cfg.prompt_path is not None:
            envelope.metadata["custom_prompt_path"] = role_cfg.prompt_path

    def _apply_guest_stamps(self, envelope: "Envelope") -> None:
        """Stamp synthetic guest identity onto an envelope for an unknown user.

        Applied when ``unknown_user_policy=guest`` and ``resolve_user()``
        returned ``None``.  The stamped fields mirror those written by
        ``_enrich_envelope`` for known users so that downstream bricks
        receive a consistent envelope regardless of user origin.

        Fields written:
            - ``user_role``: ``"guest"``
            - ``display_name``: ``"Guest"``
            - ``llm_profile``: value of ``self._guest_profile`` (overrides any
              previously stamped ``llm_profile``).
            - ``skills_dirs``: resolved from the ``"guest"`` role config, or
              ``[]`` when the role is absent (fail-closed).
            - ``allowed_mcp_tools``: same as above.

        Args:
            envelope: The incoming envelope to enrich in place.
        """
        envelope.metadata["user_role"] = "guest"
        envelope.metadata["display_name"] = "Guest"
        envelope.metadata["llm_profile"] = getattr(self, "_guest_profile", "fast")

        role_cfg = self._role_registry.get_role("guest")
        if role_cfg is not None:
            envelope.metadata["skills_dirs"] = list(role_cfg.skills_dirs)
            envelope.metadata["allowed_mcp_tools"] = list(role_cfg.allowed_mcp_tools)
            if role_cfg.prompt_path is not None:
                envelope.metadata["custom_prompt_path"] = role_cfg.prompt_path
        else:
            logger.warning(
                "guest role not found in registry — stamping empty access (fail-closed)"
            )
            envelope.metadata["skills_dirs"] = []
            envelope.metadata["allowed_mcp_tools"] = []

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

                            # Enrich envelope with user identity and LLM profile
                            self._enrich_envelope(envelope)

                            # Apply unknown-user policy when identity could not be resolved
                            if "user_role" not in envelope.metadata:
                                policy = getattr(self, "_unknown_user_policy", "deny")
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
                                    continue  # drop — finally:xack s'exécute
                                else:
                                    # deny (default) — drop silently
                                    logger.info(
                                        "unknown_user_policy=deny — dropping message from %s",
                                        envelope.sender_id,
                                    )
                                    continue  # drop — finally:xack s'exécute

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
