"""ACL manager for La Sentinelle — loads users.yaml and verifies access rights."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import resolve_config_path

logger = logging.getLogger("sentinelle.acl")

_VALID_UNKNOWN_USER_POLICIES: frozenset[str] = frozenset({"deny", "guest", "pending"})


class ACLManager:
    """Loads users.yaml and verifies access rights per user_id and channel.

    Falls back to permissive mode (allow all) when no config file is found,
    logging a WARNING to alert operators.

    The ``unknown_user_policy`` parameter controls what happens when a
    sender is not listed in users.yaml:

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
    ) -> None:
        """Initialises ACLManager, validates policy, and loads user configuration.

        Args:
            config_path: Optional explicit path to users.yaml.
            unknown_user_policy: Policy applied to senders absent from users.yaml.
            guest_profile: Profile name for guest users (only used with ``"guest"``
                policy).

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
        self._users: dict[str, dict[str, Any]] = {}
        self._roles: dict[str, dict[str, Any]] = {}
        self._permissive: bool = False
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_allowed(self, user_id: str, channel: str, action: str = "send") -> bool:
        """Checks whether user_id may perform action on channel.

        For users absent from users.yaml the ``unknown_user_policy`` applies:

        - ``"deny"``: returns False immediately.
        - ``"guest"``: returns True (caller must pass ``guest_profile`` to Atelier).
        - ``"pending"``: returns False; caller must follow up with
          :meth:`notify_pending` to publish to the admin review stream.

        In permissive mode (no users.yaml found), always returns True.

        Args:
            user_id: The sender identifier, e.g. "discord:123456789".
            channel: The target channel, e.g. "discord" or "telegram".
            action: The action to authorize. Defaults to "send".

        Returns:
            True if the user is authorized, False otherwise.
        """
        if self._permissive:
            return True

        user = self._users.get(user_id)
        if user is None:
            return self._apply_unknown_user_policy(user_id, channel)

        # Channel check — "*" wildcard allows all channels
        allowed_channels: list[str] = user.get("channels", [])
        if "*" not in allowed_channels and channel not in allowed_channels:
            logger.debug(
                "ACL deny — user %s not allowed on channel %s", user_id, channel
            )
            return False

        # Action check via role
        role_name: str = user.get("role", "")
        role_def: dict[str, Any] = self._roles.get(role_name, {})
        allowed_actions: list[str] = role_def.get("actions", [])
        if action not in allowed_actions:
            logger.debug(
                "ACL deny — role %s does not allow action %s", role_name, action
            )
            return False

        return True

    def get_user_role(self, user_id: str) -> str:
        """Returns the role assigned to user_id, or 'unknown' if not found.

        Args:
            user_id: The sender identifier, e.g. "discord:123456789".

        Returns:
            Role name string: "admin", "user", or "unknown".
        """
        if self._permissive:
            return "admin"

        user = self._users.get(user_id)
        if user is None:
            return "unknown"
        return user.get("role", "unknown")

    def get_effective_profile(self, user_id: str) -> str:
        """Returns the LLM profile name that should be used for user_id.

        For known users, returns their configured ``llm_profile``. For unknown
        users under the ``"guest"`` policy, returns the configured
        ``guest_profile``. All other cases return ``"default"``.

        Args:
            user_id: The sender identifier, e.g. "discord:123456789".

        Returns:
            Profile name string such as "default", "fast", or "precise".
        """
        if self._permissive:
            return "default"

        user = self._users.get(user_id)
        if user is not None:
            return user.get("llm_profile", "default")

        if self._unknown_user_policy == "guest":
            return self._guest_profile

        return "default"

    async def notify_pending(
        self,
        redis_conn: Any,
        user_id: str,
        channel: str,
    ) -> None:
        """Publishes an unknown-user notification to relais:admin:pending_users.

        Should be called by Sentinelle after :meth:`is_allowed` returns False
        for a user under the ``"pending"`` policy.

        Args:
            redis_conn: Active async Redis connection exposing ``xadd``.
            user_id: The sender identifier that was not found in users.yaml.
            channel: The channel on which the unknown user sent a message.
        """
        payload: dict[str, str] = {
            "user_id": user_id,
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
            user_id,
            channel,
        )

    def reload(self) -> None:
        """Reloads users.yaml from disk.

        Useful for hot-reload triggered by the admin channel (Le Vigile).
        """
        self._users = {}
        self._roles = {}
        self._permissive = False
        self._load()
        logger.info("ACL configuration reloaded from %s", self._config_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_unknown_user_policy(self, user_id: str, channel: str) -> bool:
        """Applies the configured unknown_user_policy for an absent user.

        Args:
            user_id: The sender identifier not found in users.yaml.
            channel: The originating channel.

        Returns:
            True when the policy permits the message (``"guest"``),
            False otherwise (``"deny"`` or ``"pending"``).
        """
        if self._unknown_user_policy == "guest":
            logger.info(
                "ACL [guest]: unknown user %s on %s allowed with profile '%s'",
                user_id,
                channel,
                self._guest_profile,
            )
            return True

        if self._unknown_user_policy == "pending":
            logger.warning(
                "ACL [pending]: unknown user %s on %s — queued for admin review",
                user_id,
                channel,
            )
            return False

        # default: "deny"
        logger.warning("ACL [deny]: unknown user %s on %s — rejected", user_id, channel)
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
        """Parses users.yaml and populates internal lookup tables.

        Enters permissive mode when the file cannot be found or parsed.
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

        # Index users by id for O(1) lookup
        for user in data.get("users", []):
            uid = user.get("id")
            if uid:
                self._users[uid] = user

        self._roles = data.get("roles", {})
        logger.info(
            "ACL loaded: %d users, %d roles from %s",
            len(self._users),
            len(self._roles),
            self._config_path,
        )
