"""Shared user registry — loads users.yaml for O(1) sender_id lookups.

This module provides a lightweight view of the user database that any brick
can import without pulling in the full ACL logic from ``sentinelle/acl.py``.
The ACLManager continues to own access-control decisions; UserRegistry only
resolves identity and basic profile metadata.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import resolve_config_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserRecord:
    """Immutable snapshot of a single user's metadata from users.yaml.

    Attributes:
        display_name: Human-readable name for the user.
        role: Role name (e.g. ``"admin"``, ``"user"``).
        custom_prompt_path: Relative path to a per-user prompt override, or
            ``None`` when absent.
        blocked: Whether the user account is blocked.
    """

    display_name: str
    role: str
    custom_prompt_path: str | None
    blocked: bool


class UserRegistry:
    """Loads users.yaml and exposes fast sender_id → UserRecord lookups.

    Only resolves identity and profile metadata — access-control decisions
    (groups, roles, actions) remain the responsibility of ACLManager.

    Falls back to permissive mode (returns ``None`` for every lookup) when
    ``users.yaml`` cannot be found or parsed.  This mode is intentionally
    non-blocking: missing config is not an error for read-only consumers.

    Identity lookup uses a ``"channel:raw_id"`` key so that callers pass the
    same ``sender_id`` format used throughout the Envelope pipeline (e.g.
    ``"discord:123456789"``).

    Args:
        config_path: Explicit path to ``users.yaml``.  When ``None``, the
            standard config-cascade is tried
            (``users.yaml`` → ``users.yaml.default``).
    """

    def __init__(self, config_path: Path | None = None) -> None:
        """Initialise the registry and load the user table from disk.

        Args:
            config_path: Optional explicit path to users.yaml.
        """
        self._config_path: Path | None = self._resolve_path(config_path)
        # "channel:raw_id" → UserRecord
        self._sender_index: dict[str, UserRecord] = {}
        # (channel, context, raw_id) → UserRecord
        self._by_identifier: dict[tuple[str, str, str], UserRecord] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_user(
        self,
        sender_id: str,
        channel: str,
        context: str = "dm",
    ) -> UserRecord | None:
        """Return the UserRecord matching sender_id, channel, and context.

        First attempts an exact ``(channel, context, raw_id)`` lookup, then
        falls back to the faster ``"channel:raw_id"`` sender-index.

        Args:
            sender_id: Full sender identifier, e.g. ``"discord:123456789"``.
            channel: Originating channel, e.g. ``"discord"`` or ``"telegram"``.
            context: Access context within the channel (``"dm"`` or
                ``"server"``).  Defaults to ``"dm"``.

        Returns:
            A ``UserRecord`` when the sender is found, ``None`` otherwise
            (including permissive mode where the config file is absent).
        """
        if not sender_id or ":" not in sender_id:
            return None

        raw_id = sender_id.split(":", 1)[1]

        # Exact (channel, context, raw_id) match — highest precision
        record = self._by_identifier.get((channel, context, raw_id))
        if record is not None:
            return record

        # sender_index fallback: works across contexts but within the channel
        return self._sender_index.get(f"{channel}:{raw_id}")

    def reload(self) -> None:
        """Reload users.yaml from disk, rebuilding all lookup tables atomically.

        Useful for hot-reload triggered by an admin command.  The tables are
        replaced atomically so concurrent readers see either the old or the new
        view — never a partial state (within CPython's GIL).
        """
        self._sender_index = {}
        self._by_identifier = {}
        self._load()
        logger.info("UserRegistry reloaded from %s", self._config_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_path(self, config_path: Path | None) -> Path | None:
        """Resolve which config file to use, following the standard cascade.

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
        """Parse users.yaml and populate the lookup indexes.

        Enters permissive mode (empty indexes) when the file cannot be found
        or parsed.  Permissive mode means every ``resolve_user`` call returns
        ``None`` — callers must handle the None case gracefully.
        """
        if self._config_path is None:
            logger.warning(
                "UserRegistry: users.yaml not found — running in permissive mode "
                "(all resolve_user calls will return None)."
            )
            return

        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data: dict[str, Any] = yaml.safe_load(raw) or {}
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "UserRegistry: failed to parse %s — %s — falling back to permissive mode",
                self._config_path,
                exc,
            )
            return

        new_sender_index: dict[str, UserRecord] = {}
        new_by_identifier: dict[tuple[str, str, str], UserRecord] = {}

        for _key, user in (data.get("users") or {}).items():
            record = UserRecord(
                display_name=str(user.get("display_name") or ""),
                role=str(user.get("role") or ""),
                custom_prompt_path=user.get("custom_prompt_path") or None,
                blocked=bool(user.get("blocked", False)),
            )

            identifiers: dict[str, Any] = user.get("identifiers") or {}
            for ch, contexts in identifiers.items():
                if not contexts:
                    continue
                if ch == "rest":
                    for key in contexts.get("api_keys") or []:
                        if key:
                            new_sender_index[f"rest:{key}"] = record
                else:
                    for ctx, raw_id in contexts.items():
                        if raw_id:
                            sid = str(raw_id)
                            new_by_identifier[(ch, ctx, sid)] = record
                            new_sender_index[f"{ch}:{sid}"] = record

        self._sender_index = new_sender_index
        self._by_identifier = new_by_identifier

        logger.info(
            "UserRegistry loaded: %d identifiers from %s",
            len(self._sender_index),
            self._config_path,
        )
