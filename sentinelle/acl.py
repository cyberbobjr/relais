"""ACL manager for La Sentinelle — context-aware access control from users.yaml."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import resolve_config_path
from common.user_registry import UserRecord, UserRegistry

logger = logging.getLogger("sentinelle.acl")

_VALID_UNKNOWN_USER_POLICIES: frozenset[str] = frozenset({"deny", "guest", "pending"})


class ACLManager:
    """Loads users.yaml and verifies context-aware access rights.

    Falls back to permissive mode (allow all) when no config file is found,
    logging a WARNING to alert operators.

    Identity is resolved via a ``(channel, context, raw_id)`` tuple. *context*
    is the access point within a channel (e.g. ``"dm"`` vs ``"server"`` for
    Discord). Group access (WhatsApp/Telegram) is resolved by ``scope_id``
    (the group_id), not by the individual sender.

    Two global ACL modes:

    - ``"allowlist"`` (default): only explicitly declared, non-blocked users/
      groups are admitted.  Unknown senders are handled by
      ``unknown_user_policy``.
    - ``"blocklist"``: everybody is admitted except explicitly blocked users/
      groups.  Unknown senders are admitted silently.

    The mode can be overridden per channel in ``access_control.channels``.

    The ``unknown_user_policy`` parameter controls what happens when a sender
    is not listed in users.yaml (allowlist mode only):

    - ``"deny"`` (default): reject the message, return False.
    - ``"guest"``: allow the message using ``guest_profile``; no memory, no tools.
    - ``"pending"``: reject and publish a notification to
      ``relais:admin:pending_users`` via :meth:`notify_pending`.

    Args:
        config_path: Explicit path to users.yaml. When None, falls back to
            ``~/.relais/config/users.yaml`` then ``config/users.yaml.default``.
        unknown_user_policy: One of ``"deny"``, ``"guest"``, or ``"pending"``.
            Defaults to ``"deny"``.
        guest_profile: LLM profile name assigned to guest users. Only used
            when ``unknown_user_policy`` is ``"guest"``. Defaults to ``"fast"``.

    Raises:
        ValueError: ``unknown_user_policy`` is not one of the accepted values.
    """

    def __init__(
        self,
        config_path: Path | None = None,
        unknown_user_policy: str = "deny",
        guest_profile: str = "fast",
        user_registry: UserRegistry | None = None,
    ) -> None:
        """Initialises ACLManager, validates policy, and loads user configuration.

        User identity resolution is delegated to ``UserRegistry``.  A registry
        instance is created automatically when ``user_registry`` is not supplied
        (dependency injection is supported for testing).

        Args:
            config_path: Optional explicit path to users.yaml.
            unknown_user_policy: Policy applied to senders absent from users.yaml.
            guest_profile: Profile name for guest users (only used with ``"guest"``
                policy).
            user_registry: Optional pre-constructed ``UserRegistry`` to delegate
                sender-identity lookups to.  When ``None`` a new instance is
                created using the resolved ``config_path``.

        Raises:
            ValueError: ``unknown_user_policy`` is not ``"deny"``, ``"guest"``,
                or ``"pending"``.
        """
        if unknown_user_policy not in _VALID_UNKNOWN_USER_POLICIES:
            raise ValueError(
                f"Invalid unknown_user_policy '{unknown_user_policy}'. "
                f"Must be one of: {sorted(_VALID_UNKNOWN_USER_POLICIES)}"
            )

        self._unknown_user_policy: str = unknown_user_policy
        self._guest_profile: str = guest_profile
        self._config_path: Path | None = self._resolve_path(config_path)

        # User identity lookups are delegated to UserRegistry.
        self._user_registry: UserRegistry = (
            user_registry if user_registry is not None
            else UserRegistry(config_path=config_path)
        )

        # ACL-only lookup tables populated by _load()
        # (channel, group_id) → group dict
        self._groups: dict[tuple[str, str], dict[str, Any]] = {}
        self._roles: dict[str, dict[str, Any]] = {}
        self._access_control: dict[str, Any] = {"default_mode": "allowlist"}
        self._permissive: bool = False
        self._load()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def unknown_user_policy(self) -> str:
        """The configured policy for unknown senders."""
        return self._unknown_user_policy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_allowed(
        self,
        sender_id: str,
        channel: str,
        context: str = "dm",
        scope_id: str | None = None,
        action: str = "send",
    ) -> bool:
        """Checks whether sender_id may perform action in the given channel/context.

        For group messages (``context="group"``), authorization is based on the
        group identified by ``scope_id``, not the individual sender.

        For unknown senders in allowlist mode, the ``unknown_user_policy`` applies:

        - ``"deny"``: returns False.
        - ``"guest"``: returns True.
        - ``"pending"``: returns False; caller must follow up with
          :meth:`notify_pending`.

        In blocklist mode, unknown senders are admitted unless explicitly blocked.
        In permissive mode (no users.yaml found), always returns True.

        Args:
            sender_id: The sender identifier, e.g. ``"discord:123456789"``.
                For REST, this is ``"rest:{api_key}"``.
            channel: The originating channel, e.g. ``"discord"`` or ``"telegram"``.
            context: Access context within the channel. For Discord: ``"dm"`` or
                ``"server"``. For WhatsApp/Telegram: ``"dm"`` or ``"group"``.
                Defaults to ``"dm"``.
            scope_id: Group identifier when ``context`` is ``"group"``.
            action: The action to authorize. Defaults to ``"send"``.

        Returns:
            True if the request is authorized, False otherwise.
        """
        if self._permissive:
            return True

        mode = self._get_mode(channel)

        if context == "group" and scope_id is not None:
            return self._check_group(channel, scope_id, mode)

        user = self._resolve_user(sender_id, channel, context)

        if user is None:
            if mode == "blocklist":
                return True
            return self._apply_unknown_user_policy(sender_id, channel)

        if user.blocked:
            logger.warning("ACL deny — blocked user %s on %s", sender_id, channel)
            return False

        role_name: str = user.role
        role_def: dict[str, Any] = self._roles.get(role_name, {})
        allowed_actions: list[str] = role_def.get("actions", [])
        if action not in allowed_actions:
            logger.debug(
                "ACL deny — role %s does not allow action %s", role_name, action
            )
            return False

        return True

    def get_user_role(self, sender_id: str) -> str:
        """Returns the role assigned to sender_id, or 'unknown' if not found.

        Args:
            sender_id: The sender identifier, e.g. ``"discord:123456789"``.

        Returns:
            Role name string: ``"admin"``, ``"user"``, or ``"unknown"``.
        """
        if self._permissive:
            return "admin"

        # Derive channel from the "channel:raw_id" sender_id format.
        channel = sender_id.split(":", 1)[0] if ":" in sender_id else ""
        record: UserRecord | None = self._user_registry.resolve_user(
            sender_id, channel
        )
        if record is None:
            return "unknown"
        return record.role or "unknown"

    async def notify_pending(
        self,
        redis_conn: Any,
        sender_id: str,
        channel: str,
    ) -> None:
        """Publishes an unknown-user notification to relais:admin:pending_users.

        Should be called by Sentinelle after :meth:`is_allowed` returns False
        for a sender under the ``"pending"`` policy.

        Args:
            redis_conn: Active async Redis connection exposing ``xadd``.
            sender_id: The sender identifier that was not found in users.yaml.
            channel: The channel on which the unknown sender sent a message.
        """
        payload: dict[str, str] = {
            "user_id": sender_id,
            "channel": channel,
            "timestamp": str(time.time()),
            "policy": "pending",
        }
        await redis_conn.xadd(
            "relais:admin:pending_users",
            {"payload": json.dumps(payload)},
        )
        logger.info(
            "ACL [pending]: published unknown user %s on %s to admin review stream",
            sender_id,
            channel,
        )

    def reload(self) -> None:
        """Reloads users.yaml from disk.

        Delegates user-index reload to ``UserRegistry`` and reloads its own
        ACL-specific tables (groups, roles, access_control).
        Useful for hot-reload triggered by the admin channel.
        """
        self._user_registry.reload()
        self._groups = {}
        self._roles = {}
        self._access_control = {"default_mode": "allowlist"}
        self._permissive = False
        self._load()
        logger.info("ACL configuration reloaded from %s", self._config_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_user(
        self,
        sender_id: str,
        channel: str,
        context: str = "dm",
    ) -> UserRecord | None:
        """Resolves a UserRecord from sender_id, channel, and context.

        Delegates the lookup entirely to ``UserRegistry``.

        Args:
            sender_id: Full sender identifier (``"discord:123456789"``).
            channel: The originating channel.
            context: Access context within the channel.

        Returns:
            ``UserRecord`` if found, ``None`` otherwise.
        """
        return self._user_registry.resolve_user(sender_id, channel, context)

    def _check_group(self, channel: str, scope_id: str, mode: str) -> bool:
        """Evaluates whether a group is authorized.

        Args:
            channel: The originating channel.
            scope_id: The group identifier.
            mode: Current ACL mode (``"allowlist"`` or ``"blocklist"``).

        Returns:
            True if the group is authorized, False otherwise.
        """
        group = self._groups.get((channel, scope_id))
        if group is None:
            if mode == "blocklist":
                return True
            logger.warning(
                "ACL deny — unknown group %s on %s (allowlist mode)", scope_id, channel
            )
            return False

        if group.get("blocked"):
            logger.warning("ACL deny — blocked group %s on %s", scope_id, channel)
            return False

        return bool(group.get("allowed", True))

    def _get_mode(self, channel: str) -> str:
        """Returns the effective ACL mode for the given channel.

        Channel-level overrides in ``access_control.channels`` take precedence
        over the global ``default_mode``.

        Args:
            channel: The originating channel.

        Returns:
            ``"allowlist"`` or ``"blocklist"``.
        """
        channels_override: dict[str, Any] = self._access_control.get("channels") or {}
        channel_cfg: dict[str, Any] = channels_override.get(channel) or {}
        return (
            channel_cfg.get("mode")
            or self._access_control.get("default_mode", "allowlist")
        )

    def _apply_unknown_user_policy(self, sender_id: str, channel: str) -> bool:
        """Applies the configured unknown_user_policy for an absent sender.

        Args:
            sender_id: The sender identifier not found in users.yaml.
            channel: The originating channel.

        Returns:
            True when the policy permits the message (``"guest"``),
            False otherwise (``"deny"`` or ``"pending"``).
        """
        if self._unknown_user_policy == "guest":
            logger.info(
                "ACL [guest]: unknown user %s on %s allowed with profile '%s'",
                sender_id,
                channel,
                self._guest_profile,
            )
            return True

        if self._unknown_user_policy == "pending":
            logger.warning(
                "ACL [pending]: unknown user %s on %s — queued for admin review",
                sender_id,
                channel,
            )
            return False

        logger.warning(
            "ACL [deny]: unknown user %s on %s — rejected", sender_id, channel
        )
        return False

    def _resolve_path(self, config_path: Path | None) -> Path | None:
        """Resolves which config file to use.

        Args:
            config_path: Caller-supplied path, or None for auto-discovery.

        Returns:
            The first existing path found, or None if none exist.
        """
        if config_path is not None:
            return config_path if config_path.exists() else None

        for filename in ("users.yaml", "users.yaml.default"):
            try:
                return resolve_config_path(filename)
            except FileNotFoundError:
                continue

        return None

    def _load(self) -> None:
        """Parses users.yaml and populates ACL-specific lookup tables.

        Loads ``access_control``, ``roles``, and ``groups`` from the config
        file.  User-identity indexes are owned by ``UserRegistry``; this method
        does not touch them.

        Enters permissive mode when the file cannot be found or parsed.

        Expected users.yaml schema (dict-keyed users):

        .. code-block:: yaml

            access_control:
              default_mode: allowlist   # or blocklist
              channels:                 # optional per-channel overrides
                discord:
                  mode: blocklist

            groups:
              - channel: whatsapp
                group_id: "120363@g.us"
                allowed: true
                blocked: false

            roles:
              admin:
                actions: ["send", "command", "admin"]
        """
        if self._config_path is None:
            logger.warning(
                "ACL: users.yaml not found in any config search path — "
                "running in PERMISSIVE mode (all requests allowed). "
                "Create %s/config/users.yaml to enable ACL enforcement.",
                "~/.relais",
            )
            self._permissive = True
            return

        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data: dict[str, Any] = yaml.safe_load(raw) or {}
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "ACL: failed to parse %s — %s — falling back to PERMISSIVE mode",
                self._config_path,
                exc,
            )
            self._permissive = True
            return

        self._access_control = data.get("access_control") or {"default_mode": "allowlist"}
        self._roles = data.get("roles") or {}

        # Index groups by (channel, group_id)
        for group in data.get("groups") or []:
            ch = group.get("channel")
            gid = group.get("group_id")
            if ch and gid:
                self._groups[(ch, str(gid))] = group

        logger.info(
            "ACL loaded: %d groups from %s",
            len(self._groups),
            self._config_path,
        )
