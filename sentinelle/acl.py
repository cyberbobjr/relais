"""ACL manager for La Sentinelle — context-aware access control from sentinelle.yaml.

The ACLManager no longer resolves user identity internally. Instead, callers
(Sentinelle._process_stream) pass a ``user_record`` hydrated from
``envelope.context["portail"]["user_record"]`` — populated upstream by Le Portail.

This enforces single-responsibility:
- Le Portail: user identity, role merging, guest policy → stamps ``user_record``
- La Sentinelle / ACLManager: access-control mode (allowlist/blocklist),
  group authorization, blocked check, command action check
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import resolve_config_path
from common.user_record import UserRecord

logger = logging.getLogger("sentinelle.acl")


class ACLManager:
    """Loads sentinelle.yaml and verifies context-aware access rights.

    Falls back to permissive mode (allow all) when no config file is found,
    logging a WARNING to alert operators.

    Two global ACL modes:

    - ``"allowlist"`` (default): envelopes without a valid ``user_record``
      are denied.  Group authorization uses the ``groups`` config.
    - ``"blocklist"``: admits all envelopes unless the ``user_record`` is
      explicitly blocked.

    The mode can be overridden per channel in ``access_control.channels``.

    ``user_record`` is passed in by the caller (Sentinelle) rather than
    resolved internally.  When ``user_record=None`` in allowlist mode, the
    call returns ``False`` (fail-closed).

    Args:
        config_path: Explicit path to sentinelle.yaml.  When ``None``, the
            standard config-cascade is tried
            (``sentinelle.yaml`` → ``sentinelle.yaml.default``).
    """

    def __init__(
        self,
        config_path: Path | None = None,
    ) -> None:
        """Initialise ACLManager and load ACL configuration from sentinelle.yaml.

        Args:
            config_path: Optional explicit path to sentinelle.yaml.
        """
        self._config_path: Path | None = self._resolve_path(config_path)
        # (channel, group_id) → group dict
        self._groups: dict[tuple[str, str], dict[str, Any]] = {}
        self._access_control: dict[str, Any] = {"default_mode": "allowlist"}
        self._permissive: bool = False
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_permissive(self) -> bool:
        """Return True when the ACL has no loaded configuration.

        Returns:
            True if no sentinelle.yaml was successfully loaded (all requests
            pass through); False once a valid config has been parsed.
        """
        return self._permissive

    def is_allowed(
        self,
        sender_id: str,
        channel: str,
        context: str = "dm",
        scope_id: str | None = None,
        action: str | None = None,
        user_record: UserRecord | None = None,
    ) -> bool:
        """Check whether sender_id may send a message or execute a command.

        The ``user_record`` parameter carries the pre-hydrated user identity
        stamped by Le Portail into ``envelope.context["portail"]["user_record"]``.
        When ``user_record`` is ``None`` and the mode is ``"allowlist"``,
        the call returns ``False`` (fail-closed).

        For normal messages (``action=None``), only blocked/mode checks apply.

        For slash commands (``action=<command_name>``), ``user_record.actions``
        is checked.  A record with ``"*"`` in actions may execute any command;
        otherwise the command name must appear explicitly.

        For group messages (``context="group"``), authorization is based on the
        group identified by ``scope_id``, not the individual sender.

        In permissive mode (no sentinelle.yaml found), always returns ``True``.

        Args:
            sender_id: The sender identifier, e.g. ``"discord:123456789"``.
            channel: The originating channel, e.g. ``"discord"``.
            context: Access context within the channel.  ``"dm"`` or ``"group"``.
                Defaults to ``"dm"``.
            scope_id: Group identifier when ``context`` is ``"group"``.
            action: Slash command name to authorize (e.g. ``"clear"``), or
                ``None`` for normal message sending.
            user_record: Pre-hydrated user record from ``envelope.context["portail"]``.
                When ``None`` in allowlist mode, the call returns ``False``.

        Returns:
            True if the request is authorized, False otherwise.
        """
        if self._permissive:
            return True

        mode = self._get_mode(channel)

        if context == "group" and scope_id is not None:
            return self._check_group(channel, scope_id, mode)

        if user_record is None:
            if mode == "blocklist":
                return True
            logger.warning(
                "ACL deny — no user_record for sender %s on %s (allowlist mode)",
                sender_id,
                channel,
            )
            return False

        if user_record.blocked:
            logger.warning("ACL deny — blocked user %s on %s", sender_id, channel)
            return False

        # Normal messages: identity check is sufficient.
        if action is None:
            return True

        # Command authorization: check user_record.actions directly.
        allowed_actions: list[str] = list(user_record.actions)
        if "*" in allowed_actions or action in allowed_actions:
            return True

        logger.debug(
            "ACL deny — role %s does not allow command /%s", user_record.role, action
        )
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_group(self, channel: str, scope_id: str, mode: str) -> bool:
        """Evaluate whether a group is authorized.

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
        """Return the effective ACL mode for the given channel.

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

    def _resolve_path(self, config_path: Path | None) -> Path | None:
        """Resolve which config file to use.

        Args:
            config_path: Caller-supplied path, or None for auto-discovery.

        Returns:
            The first existing path found, or None if none exist.
        """
        if config_path is not None:
            return config_path if config_path.exists() else None

        for filename in ("sentinelle.yaml", "sentinelle.yaml.default"):
            try:
                return resolve_config_path(filename)
            except FileNotFoundError:
                continue

        return None

    def _load(self) -> None:
        """Parse sentinelle.yaml and populate ACL-specific lookup tables.

        Loads ``access_control`` and ``groups`` from the config file.
        User identity is NOT loaded here — that is Le Portail's responsibility.

        Enters permissive mode when the file cannot be found or parsed.

        Expected sentinelle.yaml schema:

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
        """
        if self._config_path is None:
            logger.warning(
                "ACL: sentinelle.yaml not found in any config search path — "
                "running in PERMISSIVE mode (all requests allowed). "
                "Create %s/config/sentinelle.yaml to enable ACL enforcement.",
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
