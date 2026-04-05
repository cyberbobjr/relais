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
``Sentinelle`` runs two concurrent asyncio loops:

* ``_process_stream`` (incoming) — consumes ``relais:security``, extracts the
  ``user_record`` stamped by Portail, calls ``ACLManager`` for role-based
  access control, detects slash commands via ``is_command`` /
  ``extract_command_name`` / ``KNOWN_COMMANDS`` (common.command_utils), and
  either routes to ``relais:commands`` or rejects inline with a reply
  envelope.
* ``_process_outgoing_stream`` (outgoing) — consumes
  ``relais:messages:outgoing_pending``, applies outgoing guardrails (currently
  a passthrough), and publishes the envelope to
  ``relais:messages:outgoing:{channel}``.

``ACLManager`` (sentinelle.acl) resolves role-based permissions from
sentinelle.yaml.

Redis channels
--------------
Consumed:
  - relais:security                   (consumer group: sentinelle_group)
  - relais:messages:outgoing_pending  (consumer group: sentinelle_outgoing_group)

Produced:
  - relais:tasks                      — authorized normal messages → Atelier
  - relais:commands                   — authorized slash commands → Commandant
  - relais:messages:outgoing:{channel}— inline rejection replies + outgoing fwd
  - relais:logs                       — operational log entries

Processing flow — incoming
--------------------------
  (1) Consume from relais:security (sentinelle_group).
  (2) Deserialize Envelope; extract user_record from metadata.
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
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

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
from common.user_record import UserRecord
from common.config_reload import safe_reload, watch_and_reload
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
        # Resolve config path once via ACLManager's own resolver for consistency.
        _initial_acl = ACLManager()
        self._config_path: Path | None = _initial_acl._config_path
        # Config reload lock — guards self._acl
        self._config_lock: asyncio.Lock = asyncio.Lock()
        self._acl: ACLManager = _initial_acl

    def _load(self) -> None:
        """Reload ACL configuration from sentinelle.yaml.

        Reconstructs ``self._acl`` from disk.  Called by ``__init__`` and
        (indirectly) by ``reload_config()``.

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
        logger.info("Sentinelle: ACL config loaded from %s", self._config_path)

    def _build_acl_candidate(self) -> ACLManager:
        """Build a new ACLManager from disk without mutating self.

        Returns:
            A fresh ACLManager loaded from the current config file.

        Raises:
            FileNotFoundError: If ``self._config_path`` is None or missing.
            yaml.YAMLError: If the config file cannot be parsed as valid YAML.
        """
        if self._config_path is None or not self._config_path.exists():
            raise FileNotFoundError(
                f"Sentinelle config file not found: {self._config_path}"
            )
        raw = self._config_path.read_text(encoding="utf-8")
        yaml.safe_load(raw)  # raises yaml.YAMLError on malformed input
        return ACLManager(config_path=self._config_path)

    def _apply_acl(self, acl: ACLManager) -> None:
        """Swap in a freshly loaded ACLManager.

        Args:
            acl: The new ACLManager instance to install.
        """
        self._acl = acl
        logger.info("Sentinelle: ACL config applied")

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
            self._build_acl_candidate,
            self._apply_acl,
            checkpoint_paths=[self._config_path] if self._config_path else [],
        )

    def _config_watch_paths(self) -> list[Path]:
        """Return the list of config file paths to watch for changes.

        Returns:
            A list containing the sentinelle.yaml config path.
        """
        cfg_path = getattr(self, "_config_path", None)
        return [cfg_path] if cfg_path is not None else []

    def _start_file_watcher(self) -> "asyncio.Task | None":
        """Create and return an asyncio.Task that watches config files for changes.

        Returns None when watchfiles is not installed (hot-reload gracefully
        degrades to Redis Pub/Sub only).

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

    async def _config_reload_listener(self, redis_conn: Any) -> None:
        """Subscribe to ``relais:config:reload:sentinelle`` and trigger hot-reloads.

        Runs as a background asyncio task alongside the main processing loops.
        Only the exact string ``"reload"`` triggers a config reload; all other
        messages are silently ignored.

        Args:
            redis_conn: Active async Redis connection (must support pub/sub).
        """
        pubsub = redis_conn.pubsub()
        channel = "relais:config:reload:sentinelle"
        await pubsub.subscribe(channel)
        logger.info("Sentinelle: subscribed to %s", channel)

        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if data == "reload":
                logger.info("Sentinelle: received reload signal — reloading config")
                await self.reload_config()

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
            acl_scope: Optional scope_id from envelope metadata.
            user_record: Pre-hydrated UserRecord from envelope metadata.
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

                            # Deserialize user_record from envelope metadata (stamped by Portail).
                            # Fail-closed: missing user_record → deny (do not forward).
                            _raw_context = envelope.metadata.get("access_context", "dm")
                            acl_context: str = _raw_context if _raw_context in {"dm", "group"} else "dm"
                            acl_scope: str | None = envelope.metadata.get("access_scope")

                            _ur_dict: dict | None = envelope.metadata.get("user_record")
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

        reload_listener_task = asyncio.create_task(
            self._config_reload_listener(redis_conn)
        )
        watcher_task = self._start_file_watcher()
        try:
            await asyncio.gather(
                self._process_stream(redis_conn, shutdown=shutdown),
                self._process_outgoing_stream(redis_conn, shutdown=shutdown),
            )
        except asyncio.CancelledError:
            logger.info("Sentinelle shutting down...")
        finally:
            reload_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reload_listener_task
            if watcher_task is not None:
                watcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher_task
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
