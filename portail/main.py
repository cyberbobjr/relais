"""Portail brick — user identity resolution and envelope enrichment.

Functional role
---------------
First processing stage after channel ingestion.  Validates each incoming
Envelope, resolves the sender's identity from portail.yaml via ``UserRegistry``,
and enriches the envelope's context before forwarding it to Sentinelle for
ACL enforcement.  Unknown senders are handled according to the configured
``unknown_user_policy`` (reject or allow as guest).

Technical overview
------------------
``Portail`` extends :class:`~common.brick_base.BrickBase` and runs a single
asyncio consumer loop.  Key helpers:

* ``UserRegistry`` — loads and caches user records from portail.yaml;
  resolves ``sender_id`` → ``UserRecord``.
* ``_enrich_envelope`` — writes the canonical ``user_record`` dict and
  ``llm_profile`` (from ``channel_profile`` or ``"default"``) into
  ``envelope.context[CTX_PORTAIL]``.
* ``_apply_guest_stamps`` — stamps minimal guest context when the sender
  is unknown and the guest policy is "allow".
* ``_update_active_sessions`` — maintains a Redis Hash used by Crieur to
  push proactive notifications to active users.

Redis channels
--------------
Consumed:
  - relais:messages:incoming  (consumer group: portail_group)
  - relais:config:reload:portail  (Pub/Sub channel for hot-reload trigger)

Produced:
  - relais:security           — enriched envelopes forwarded to Sentinelle
  - relais:logs               — operational log entries

Redis keys written:
  - relais:active_sessions:{sender_id}  (Hash, TTL 1 h)

Configuration hot-reload
------------------------
Portail watches portail.yaml for changes and reloads user registry without
restarting:

* Watched files: portail.yaml
* Reload trigger: File system change detected via watchfiles library
* Reload mechanism: safe_reload() performs atomic parse → lock → swap pattern;
  if new config is invalid YAML, previous config is preserved
* Redis Pub/Sub channel: relais:config:reload:portail (listens for external
  reload triggers from operator)
* Config backups: up to 5 versions stored in ~/.relais/config/backups/

Processing flow
---------------
  (1) Consume from relais:messages:incoming (portail_group).
  (2) Deserialize Envelope from JSON payload.
  (3) Resolve sender via UserRegistry.
  (4) Apply unknown_user_policy: drop silently or stamp guest context.
  (5) Enrich envelope.context[CTX_PORTAIL] with user_record, user_id, and
      llm_profile (from channel_profile or "default").
  (6) Update relais:active_sessions:{sender_id} hash.
  (7) Forward enriched envelope to relais:security.
  (8) XACK the message (unconditional — validation errors are logged and
      dropped, never left in PEL).
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from common.brick_base import BrickBase, StreamSpec
from common.config_reload import safe_reload
from common.contexts import CTX_AIGUILLEUR, CTX_PORTAIL, AiguilleurCtx, PortailCtx, ensure_ctx
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_VALIDATED
from common.redis_client import RedisClient
from common.user_record import UserRecord
from portail.user_registry import UserRegistry

logger = logging.getLogger("portail")


class Portail(BrickBase):
    """La brique Le Portail du système RELAIS.

    Responsible for consuming incoming messages from external relays, enriching
    envelopes with a single ``user_record`` dict (resolved from portail.yaml),
    and forwarding them to La Sentinelle for security validation.

    The ``user_record`` dict in ``envelope.context[CTX_PORTAIL]`` is the sole
    carrier of user identity and role data for all downstream bricks.

    Inherits the full lifecycle plumbing (connection, shutdown, hot-reload,
    logging) from :class:`~common.brick_base.BrickBase`.
    """

    def __init__(self) -> None:
        """Initialise Le Portail with Redis stream and group configurations."""
        super().__init__("portail")
        self.stream_in: str = "relais:messages:incoming"
        self.stream_out: str = "relais:security"
        self.group_name: str = "portail_group"
        self.consumer_name: str = "portail_1"
        # Discover config path via a temporary registry instantiation, then
        # delegate all state initialisation to _load() so __init__ and hot-reload
        # share a single authoritative code path.
        self._config_path: Path | None = UserRegistry()._config_path
        self._load()

    # ------------------------------------------------------------------
    # BrickBase abstract interface
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load (or reload) configuration state from portail.yaml.

        Reconstructs ``self._user_registry``, ``self._guest_role``, and
        ``self._unknown_user_policy`` from disk.  Called once by ``__init__``
        for initial load; hot-reload goes through the ``_build_config_candidate``
        → ``_apply_config`` path instead.

        This method is the single authoritative entry point for loading
        Portail's mutable configuration.  It never touches Redis or any
        async resource.

        Raises:
            Any exception raised by ``UserRegistry.__init__`` (e.g. YAML parse
            errors) propagates to the caller so ``safe_reload`` can intercept it
            and preserve the previous configuration.
        """
        registry = UserRegistry(config_path=self._config_path)
        self._user_registry = registry
        self._guest_role = registry.guest_role
        self._unknown_user_policy = registry.unknown_user_policy
        logger.info(
            "Portail: config loaded — unknown_user_policy=%s, guest_role=%s",
            self._unknown_user_policy,
            self._guest_role,
        )

    def stream_specs(self) -> list[StreamSpec]:
        """Return the StreamSpec for consuming ``relais:messages:incoming``.

        Returns:
            A list containing one :class:`~common.brick_base.StreamSpec` that
            describes the ``portail_group`` consumer on
            ``relais:messages:incoming``.  ``ack_mode="always"`` because every
            message is ACKed unconditionally — validation errors are logged and
            dropped, never left in the PEL.
        """
        return [
            StreamSpec(
                stream=self.stream_in,
                group=self.group_name,
                consumer=self.consumer_name,
                handler=self._handle_envelope,
                ack_mode="always",
            )
        ]

    # ------------------------------------------------------------------
    # BrickBase optional hooks
    # ------------------------------------------------------------------

    def _config_watch_paths(self) -> list[Path]:
        """Return the list of config file paths to watch for changes.

        Returns:
            A list containing the portail.yaml config path.
        """
        cfg_path = getattr(self, "_config_path", None)
        return [cfg_path] if cfg_path is not None else []

    def _build_config_candidate(self) -> UserRegistry:
        """Build a new UserRegistry from disk without mutating self.

        Pre-validates the YAML before handing off to UserRegistry so that
        malformed files raise an exception (``safe_reload`` then intercepts
        the error and preserves the current configuration).

        Returns:
            A fresh UserRegistry loaded from the current config file.

        Raises:
            FileNotFoundError: If ``self._config_path`` is None or does not exist.
            yaml.YAMLError: If the config file cannot be parsed as valid YAML.
        """
        if self._config_path is None or not self._config_path.exists():
            raise FileNotFoundError(
                f"Portail config file not found: {self._config_path}"
            )
        # Validate YAML before constructing the registry — UserRegistry catches
        # parse errors internally and falls back silently; we want hard failure here.
        raw = self._config_path.read_text(encoding="utf-8")
        yaml.safe_load(raw)  # raises yaml.YAMLError on malformed input
        return UserRegistry(config_path=self._config_path)

    def _apply_config(self, registry: UserRegistry) -> None:
        """Swap in a freshly loaded UserRegistry and update dependent fields.

        Args:
            registry: The new UserRegistry instance to install.
        """
        self._user_registry = registry
        self._guest_role = registry.guest_role
        self._unknown_user_policy = registry.unknown_user_policy
        logger.info(
            "Portail: config applied — unknown_user_policy=%s, guest_role=%s",
            self._unknown_user_policy,
            self._guest_role,
        )

    # ------------------------------------------------------------------
    # Hot-reload (override to delegate to safe_reload directly)
    # ------------------------------------------------------------------

    async def reload_config(self) -> bool:
        """Hot-reload portail.yaml without interrupting the processing loop.

        Uses ``safe_reload`` to guarantee that the previous configuration is
        preserved if the new file is malformed or cannot be parsed.

        Returns:
            True when the configuration was reloaded successfully.
            False when the reload failed (previous config preserved).
        """
        return await safe_reload(
            self._config_lock,
            "portail",
            self._build_config_candidate,
            self._apply_config,
            checkpoint_paths=[self._config_path] if self._config_path else [],
        )

    # ------------------------------------------------------------------
    # Envelope enrichment helpers
    # ------------------------------------------------------------------

    def _enrich_envelope(self, envelope: Envelope) -> None:
        """Stamp ``user_record`` dict and ``llm_profile`` into context[CTX_PORTAIL].

        Resolves the sender against the UserRegistry.  When found, writes the
        fully-merged ``UserRecord.to_dict()`` under ``context[CTX_PORTAIL]["user_record"]``
        and ``llm_profile`` as a sibling key in the same namespace.

        ``llm_profile`` is resolved as: ``context[CTX_AIGUILLEUR]["channel_profile"]`` > ``"default"``.

        Unknown users produce no ``CTX_PORTAIL`` namespace — the caller
        (``_handle_envelope``) handles them via the configured policy.
        Under ``deny`` policy, the envelope is dropped before forwarding, so
        downstream bricks never see an envelope without ``context[CTX_PORTAIL]``.

        Args:
            envelope: The incoming envelope to enrich in place.
        """
        record: UserRecord | None = self._user_registry.resolve_user(
            sender_id=envelope.sender_id,
            channel=envelope.channel,
        )
        if record is None:
            return

        aiguilleur_ctx: AiguilleurCtx = envelope.context.get(CTX_AIGUILLEUR, {})
        ctx = ensure_ctx(envelope, CTX_PORTAIL)
        ctx["user_record"] = record.to_dict()
        ctx["user_id"] = record.user_id
        ctx["llm_profile"] = aiguilleur_ctx.get("channel_profile") or "default"

    def _apply_guest_stamps(self, envelope: Envelope) -> None:
        """Stamp a synthetic guest ``user_record`` dict into context[CTX_PORTAIL].

        Applied when ``unknown_user_policy=guest`` and ``resolve_user()``
        returned ``None``.  Uses ``UserRegistry.build_guest_record()`` so that
        role-level fields (actions, skills_dirs, etc.) are taken from the
        ``guest`` role config.

        Args:
            envelope: The incoming envelope to enrich in place.
        """
        guest_record = self._user_registry.build_guest_record()
        aiguilleur_ctx: AiguilleurCtx = envelope.context.get(CTX_AIGUILLEUR, {})
        ctx = ensure_ctx(envelope, CTX_PORTAIL)
        ctx["user_record"] = guest_record.to_dict()
        ctx["user_id"] = "guest"
        ctx["llm_profile"] = aiguilleur_ctx.get("channel_profile") or "default"

    async def _update_active_sessions(self, redis_conn: Any, envelope: Envelope) -> None:
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

        portail_ctx: PortailCtx = envelope.context.get(CTX_PORTAIL, {})
        user_record_dict: dict[str, Any] = portail_ctx.get("user_record") or {}
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

    # ------------------------------------------------------------------
    # BrickBase handler
    # ------------------------------------------------------------------

    async def _handle_envelope(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Process one incoming envelope: enrich, apply policy, forward.

        This is the handler invoked by the BrickBase stream loop for each
        message consumed from ``relais:messages:incoming``.

        Args:
            envelope: Deserialized incoming envelope.
            redis_conn: Active async Redis connection.

        Returns:
            ``True`` always — Portail uses ``ack_mode="always"`` so the
            BrickBase loop ACKs unconditionally regardless of the return value.
        """
        logger.info(
            "Received message: %s from %s",
            envelope.correlation_id,
            envelope.channel,
        )

        # Enrich envelope with single user_record dict
        self._enrich_envelope(envelope)

        # Apply unknown-user policy when user_record is absent
        if CTX_PORTAIL not in envelope.context:
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
                return True  # drop — ACKed via ack_mode="always"
            else:
                # deny (default) — drop silently
                logger.info(
                    "unknown_user_policy=deny — dropping message from %s",
                    envelope.sender_id,
                )
                return True  # drop — ACKed via ack_mode="always"

        # Update active session tracking
        await self._update_active_sessions(redis_conn, envelope)

        # Add trace and stamp action
        envelope.add_trace("portail", "received and session updated")
        envelope.action = ACTION_MESSAGE_VALIDATED

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

        return True


if __name__ == "__main__":
    from common.init import initialize_user_dir
    initialize_user_dir()
    portail = Portail()
    try:
        import asyncio as _asyncio
        _asyncio.run(portail.start())
    except KeyboardInterrupt:
        pass
