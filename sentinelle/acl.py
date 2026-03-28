"""ACL manager for La Sentinelle — loads users.yaml and verifies access rights."""

import logging
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import resolve_config_path

logger = logging.getLogger("sentinelle.acl")


class ACLManager:
    """Loads users.yaml and verifies access rights per user_id and channel.

    Falls back to permissive mode (allow all) when no config file is found,
    logging a WARNING to alert operators.

    Args:
        config_path: Explicit path to users.yaml. When None, falls back to
            ~/.relais/config/users.yaml then config/users.yaml.default.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        """Initializes ACLManager and loads user configuration.

        Args:
            config_path: Optional explicit path to users.yaml.
        """
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
            logger.debug("ACL deny — unknown user: %s", user_id)
            return False

        # Channel check
        allowed_channels: list[str] = user.get("channels", [])
        if channel not in allowed_channels:
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
