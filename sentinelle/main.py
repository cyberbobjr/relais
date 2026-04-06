"""Sentinelle brick — bidirectional security checkpoint.

Functional role
---------------
Guards both directions of the pipeline.  On the *incoming* path it performs
ACL validation and routes authorized messages toward the right processor:
slash commands go to Commandant, normal messages go to Atelier.  On the
*outgoing* path it receives fully assembled replies from the pending stream
and fans them out to the correct per-channel output stream.

Technical overview
------------------
``Sentinelle`` inherits from ``BrickBase`` and declares two ``StreamSpec``
instances:

* Incoming spec — consumes ``relais:security``, extracts the ``user_record``
  stamped by Portail, calls ``ACLManager`` for role-based access control,
  detects slash commands via ``is_command`` / ``extract_command_name`` /
  ``KNOWN_COMMANDS`` (common.command_utils), and either routes to
  ``relais:commands`` or rejects inline with a reply envelope.
* Outgoing spec — consumes ``relais:messages:outgoing_pending``, applies
  outgoing guardrails (currently a passthrough), and publishes the envelope to
  ``relais:messages:outgoing:{envelope.channel}``.

``ACLManager`` (sentinelle.acl) resolves role-based permissions from
sentinelle.yaml.

Redis channels
--------------
Consumed:
  - relais:security                   (consumer group: sentinelle_group)
  - relais:messages:outgoing_pending  (consumer group: sentinelle_outgoing_group)
  - relais:config:reload:sentinelle   (Pub/Sub channel for hot-reload trigger)

Produced:
  - relais:tasks                      — authorized normal messages → Atelier
  - relais:commands                   — authorized slash commands → Commandant
  - relais:messages:outgoing:{channel}— inline rejection replies + outgoing fwd
  - relais:logs                       — operational log entries

Configuration hot-reload
------------------------
Sentinelle watches sentinelle.yaml for ACL changes and reloads without
restarting:

* Watched files: sentinelle.yaml
* Reload trigger: File system change detected via watchfiles library
* Reload mechanism: safe_reload() performs atomic parse → lock → swap pattern;
  if new config is invalid YAML, previous ACL rules are preserved
* **Fail-closed guard**: once a valid non-permissive ACL has been loaded
  (``_config_loaded_once = True``), any reload that would result in a
  permissive ACLManager (empty/missing sentinelle.yaml) is rejected — prevents
  privilege escalation by config deletion or empty file
* Redis Pub/Sub channel: relais:config:reload:sentinelle (listens for external
  reload triggers from operator)
* Config backups: up to 5 versions stored in ~/.relais/config/backups/

Processing flow — incoming
--------------------------
  (1) Consume from relais:security (sentinelle_group).
  (2) Deserialize Envelope; extract user_record from context.portail.
  (3) ACL identity check via ACLManager.
  (4a) Authorized + is_command: validate against KNOWN_COMMANDS, check
       command-level ACL; route to relais:commands or send inline rejection.
  (4b) Authorized + normal message: forward to relais:tasks.
  (4c) Unauthorized: drop silently and write to relais:logs.
  (5) XACK.

Processing flow — outgoing
--------------------------
  (1) Consume from relais:messages:outgoing_pending (sentinelle_outgoing_group).
  (2) Deserialize Envelope.
  (3) Apply outgoing guardrails (passthrough today).
  (4) Publish to relais:messages:outgoing:{envelope.channel}.
  (5) XACK.

XACK contract:
  - Both loops ACK unconditionally after processing (errors are logged, not
    retried via PEL, to avoid blocking the outgoing path on transient issues).
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from common.brick_base import BrickBase, StreamSpec
from common.command_utils import KNOWN_COMMANDS, extract_command_name, is_command
from common.config_reload import safe_reload, watch_and_reload
from common.contexts import CTX_PORTAIL, PortailCtx
from common.envelope import Envelope
from common.redis_client import RedisClient  # noqa: F401 — kept for test-namespace patching
from common.streams import (
    STREAM_COMMANDS,
    STREAM_LOGS,
    STREAM_OUTGOING_PENDING,
    STREAM_SECURITY,
    STREAM_TASKS,
    stream_outgoing,
)
from common.user_record import UserRecord
from sentinelle.acl import ACLManager

logger = logging.getLogger("sentinelle")


class Sentinelle(BrickBase):
    """Bidirectional security checkpoint for the RELAIS pipeline.

    Inherits from ``BrickBase`` which provides the stream-loop lifecycle,
    hot-reload file watcher, Redis Pub/Sub reload listener, and structured
    logging.  Sentinelle declares two ``StreamSpec`` instances — one for
    the incoming ACL path and one for the outgoing passthrough path.
    """

    def __init__(self) -> None:
        """Initialise Sentinelle with stream, group and ACL configuration."""
        super().__init__("sentinelle")
        self.stream_in: str = STREAM_SECURITY
        self.stream_out: str = STREAM_TASKS
        self.stream_commands: str = STREAM_COMMANDS
        self.group_name: str = "sentinelle_group"
        self.consumer_name: str = "sentinelle_1"
        self.outgoing_group_name: str = "sentinelle_outgoing_group"
        self.outgoing_consumer_name: str = "sentinelle_outgoing_1"
        # Resolve config path once via ACLManager's own resolver for consistency.
        _initial_acl = ACLManager()
        self._config_path: Path | None = _initial_acl._config_path
        self._acl: ACLManager = _initial_acl
        self._config_loaded_once: bool = not _initial_acl.is_permissive
        if _initial_acl.is_permissive:
            logger.warning(
                "Sentinelle: starting in permissive mode — sentinelle.yaml not found"
            )

    # ------------------------------------------------------------------
    # BrickBase abstract interface
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Reload ACL configuration from sentinelle.yaml.

        Reconstructs ``self._acl`` from disk.  Called by ``__init__`` (via
        BrickBase) and (indirectly) by ``reload_config()``.

        This is the single authoritative entry point for loading Sentinelle's
        mutable configuration.  Does not touch Redis or any async resource.

        Raises:
            FileNotFoundError: If ``self._config_path`` is None or missing.
            yaml.YAMLError: If the config file cannot be parsed as valid YAML.
        """
        if self._config_path is None or not self._config_path.exists():
            raise FileNotFoundError(
                f"Sentinelle config file not found: {self._config_path}"
            )
        # Pre-validate YAML — ACLManager silently falls back to permissive mode
        # on parse errors; we want an explicit failure here so safe_reload can
        # preserve the previous ACL.
        raw = self._config_path.read_text(encoding="utf-8")
        yaml.safe_load(raw)  # raises yaml.YAMLError on malformed input
        self._acl = ACLManager(config_path=self._config_path)
        self._config_loaded_once = True
        logger.info("Sentinelle: ACL config loaded from %s", self._config_path)

    def stream_specs(self) -> list[StreamSpec]:
        """Declare the two Redis streams this brick consumes.

        Returns:
            A list of two ``StreamSpec`` instances: one for incoming security
            validation and one for outgoing passthrough.
        """
        # getattr fallbacks are intentional: unit tests construct Sentinelle via
        # __new__ (bypassing __init__) and set only the attributes they need.
        # Defaults here match the values that __init__ would have assigned.
        return [
            StreamSpec(
                stream=getattr(self, "stream_in", STREAM_SECURITY),
                group=getattr(self, "group_name", "sentinelle_group"),
                consumer=getattr(self, "consumer_name", "sentinelle_1"),
                handler=self._handle_incoming,
                ack_mode="always",
            ),
            StreamSpec(
                stream=STREAM_OUTGOING_PENDING,
                group=getattr(self, "outgoing_group_name", "sentinelle_outgoing_group"),
                consumer=getattr(self, "outgoing_consumer_name", "sentinelle_outgoing_1"),
                handler=self._handle_outgoing,
                ack_mode="always",
            ),
        ]

    # ------------------------------------------------------------------
    # BrickBase optional hooks
    # ------------------------------------------------------------------

    def _config_watch_paths(self) -> list[Path]:
        """Return the list of config file paths to watch for changes.

        Returns:
            A list containing the sentinelle.yaml config path, or an empty
            list if the path is not set.
        """
        cfg_path = getattr(self, "_config_path", None)
        return [cfg_path] if cfg_path is not None else []

    def _build_config_candidate(self) -> ACLManager:
        """Build a new ACLManager from disk without mutating self.

        Returns:
            A fresh ACLManager loaded from the current config file.

        Raises:
            FileNotFoundError: If ``self._config_path`` is None or missing.
            yaml.YAMLError: If the config file cannot be parsed as valid YAML.
        """
        if self._config_path is None or not self._config_path.exists():
            if self._config_loaded_once:
                raise RuntimeError(
                    "ACLManager: regression to permissive mode refused "
                    "(config was loaded once — fail-closed on reload)"
                )
            raise FileNotFoundError(
                f"Sentinelle config file not found: {self._config_path}"
            )
        raw = self._config_path.read_text(encoding="utf-8")
        yaml.safe_load(raw)  # raises yaml.YAMLError on malformed input
        candidate = ACLManager(config_path=self._config_path)
        if self._config_loaded_once and candidate.is_permissive:
            raise RuntimeError(
                "ACLManager: regression to permissive mode refused "
                "(config was loaded once — fail-closed on reload)"
            )
        return candidate

    def _apply_config(self, acl: ACLManager) -> None:
        """Swap in a freshly loaded ACLManager atomically.

        Args:
            acl: The new ACLManager instance to install.
        """
        self._acl = acl
        if not acl.is_permissive:
            self._config_loaded_once = True
        logger.info("Sentinelle: ACL config applied")

    # ------------------------------------------------------------------
    # Hot-reload — public overrides
    # ------------------------------------------------------------------

    async def reload_config(self) -> bool:
        """Hot-reload sentinelle.yaml without interrupting the processing loops.

        Uses ``safe_reload`` to guarantee that the previous ACL is preserved if
        the new file is malformed or cannot be parsed.

        Returns:
            True when the configuration was reloaded successfully.
            False when the reload failed (previous ACL preserved).
        """
        return await safe_reload(
            self._config_lock,
            "sentinelle",
            self._build_config_candidate,
            self._apply_config,
            checkpoint_paths=[self._config_path] if self._config_path else [],
        )

    def _start_file_watcher(self, shutdown_event: asyncio.Event | None = None) -> "asyncio.Task | None":
        """Create and return an asyncio.Task that watches config files for changes.

        Returns None when watchfiles is not installed (hot-reload gracefully
        degrades to Redis Pub/Sub only).

        Args:
            shutdown_event: Optional shutdown event (accepted for interface
                compatibility; the file watcher uses its own cancellation
                mechanism via asyncio.Task.cancel).

        Returns:
            An asyncio.Task running watch_and_reload, or None when watchfiles
            is unavailable.
        """
        from common.config_reload import watchfiles as _wf
        if _wf is None:
            logger.warning(
                "Sentinelle: watchfiles not installed — file-based hot-reload disabled. "
                "Install with: pip install watchfiles"
            )
            return None
        return asyncio.create_task(
            watch_and_reload(self._config_watch_paths(), self.reload_config, "sentinelle")
        )

    async def _config_reload_listener(
        self, redis_conn: Any, shutdown_event: asyncio.Event | None = None
    ) -> None:
        """Subscribe to ``relais:config:reload:sentinelle`` and trigger hot-reloads.

        Runs as a background asyncio task alongside the main processing loops.
        Only the exact string ``"reload"`` triggers a config reload; all other
        messages are silently ignored.

        Args:
            redis_conn: Active async Redis connection (must support pub/sub).
            shutdown_event: Optional shutdown event (accepted for interface
                compatibility; the listener exits when the async generator is
                exhausted or the task is cancelled).
        """
        pubsub = redis_conn.pubsub()
        channel = "relais:config:reload:sentinelle"
        await pubsub.subscribe(channel)
        logger.info("Sentinelle: subscribed to %s", channel)

        async for message in pubsub.listen():
            if shutdown_event is not None and shutdown_event.is_set():
                break
            if message.get("type") != "message":
                continue
            data = message.get("data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if data == "reload":
                logger.info("Sentinelle: received reload signal — reloading config")
                await self.reload_config()

    # ------------------------------------------------------------------
    # BrickBase stream handlers
    # ------------------------------------------------------------------

    async def _handle_incoming(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Validate and route one incoming envelope from ``relais:security``.

        Performs ACL identity check, then either routes the message to
        ``relais:tasks`` (normal message), ``relais:commands`` (authorised
        command), or sends an inline rejection reply.

        Args:
            envelope: The envelope to validate.
            redis_conn: Active Redis connection for publishing.

        Returns:
            Always True (ack_mode="always" — errors are logged and dropped).
        """
        portail_ctx: PortailCtx = envelope.context.get(CTX_PORTAIL, {})  # type: ignore[assignment]
        _raw_context = portail_ctx.get("access_context", "dm")
        acl_context: str = _raw_context if _raw_context in {"dm", "group"} else "dm"
        acl_scope: str | None = portail_ctx.get("access_scope")

        _ur_dict: dict | None = portail_ctx.get("user_record")
        user_record: UserRecord | None = (
            UserRecord.from_dict(_ur_dict) if _ur_dict else None
        )

        is_authorized = self._acl.is_allowed(
            envelope.sender_id,
            envelope.channel,
            context=acl_context,
            scope_id=acl_scope,
            user_record=user_record,
        )

        if is_authorized:
            envelope.add_trace("sentinelle", "ACL verified")

            if is_command(envelope.content):
                await self._handle_command(
                    redis_conn, envelope, acl_context, acl_scope,
                    user_record=user_record,
                )
            else:
                await asyncio.gather(
                    redis_conn.xadd(self.stream_out, {"payload": envelope.to_json()}),
                    redis_conn.xadd(STREAM_LOGS, {
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
                "Unauthorized message %s dropped.", envelope.correlation_id
            )
            await redis_conn.xadd(STREAM_LOGS, {
                "level": "WARN",
                "brick": "sentinelle",
                "correlation_id": envelope.correlation_id,
                "sender_id": envelope.sender_id,
                "message": (
                    f"Blocked unauthorized message {envelope.correlation_id}"
                ),
                "content_preview": envelope.content[:60] if envelope.content else "",
            })

        return True

    async def _handle_outgoing(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Apply outgoing guardrails and forward to the per-channel stream.

        Reads the destination channel from the envelope, applies the (currently
        passthrough) outgoing rule, and publishes to
        ``relais:messages:outgoing:{envelope.channel}``.

        Args:
            envelope: The outgoing envelope to forward.
            redis_conn: Active Redis connection for publishing.

        Returns:
            Always True (ack_mode="always" — errors are logged and dropped).
        """
        stream_out = stream_outgoing(envelope.channel)
        logger.debug(
            "Outgoing pass-through: %s → %s",
            envelope.correlation_id,
            stream_out,
        )
        # Outgoing rule — currently a pass-through.
        # Future: apply output content policy here.
        envelope.add_trace("sentinelle", "outgoing pass-through")
        await redis_conn.xadd(stream_out, {"payload": envelope.to_json()})
        return True

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    async def _reply_inline(self, redis_conn: Any, envelope: Envelope, message: str) -> None:
        """Send a short reply directly to the channel's outgoing stream.

        Args:
            redis_conn: Active Redis connection.
            envelope: The originating envelope (used to derive channel and routing context).
            message: Plain-text reply content.
        """
        reply = Envelope.create_response_to(envelope, message)
        out_stream = stream_outgoing(envelope.channel)
        await redis_conn.xadd(out_stream, {"payload": reply.to_json()})

    async def _handle_command(
        self,
        redis_conn: Any,
        envelope: Envelope,
        acl_context: str,
        acl_scope: str | None,
        user_record: UserRecord | None = None,
    ) -> None:
        """Route an authenticated command envelope after ACL identity check.

        Unknown commands receive an inline rejection.  Known commands are
        checked against the per-command action in the role's *actions* list
        (or the wildcard); unauthorised ones get a permission reply, authorised
        ones are forwarded to ``relais:commands``.

        Args:
            redis_conn: Active Redis connection.
            envelope: The command envelope (content starts with '/').
            acl_context: Already-sanitised access context ("dm" or "group").
            acl_scope: Optional scope_id from envelope context.
            user_record: Pre-hydrated UserRecord from envelope context.portail.
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
            user_record=user_record,
        )
        if cmd_authorized:
            await asyncio.gather(
                redis_conn.xadd(self.stream_commands, {"payload": envelope.to_json()}),
                redis_conn.xadd(STREAM_LOGS, {
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


if __name__ == "__main__":
    from common.init import initialize_user_dir
    initialize_user_dir()
    sentinelle = Sentinelle()
    import asyncio as _asyncio
    try:
        _asyncio.run(sentinelle.start())
    except KeyboardInterrupt:
        pass
