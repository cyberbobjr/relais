"""User registry — loads portail.yaml for O(1) sender_id → UserRecord lookups.

Portail is the sole consumer of this module.  Downstream bricks only need
``UserRecord`` (from ``common.user_record``) to deserialize the pre-stamped
``envelope.context["portail"]["user_record"]`` dict.

Role data (actions, skills_dirs, allowed_mcp_tools) is merged into every
resolved ``UserRecord`` at load time.  Prompt paths are kept separate by
origin: ``prompt_path`` comes from the user entry only (no role fallback),
and ``role_prompt_path`` comes from the role entry only.

``llm_profile`` is NOT part of UserRecord — it is stamped directly into
``envelope.context["portail"]["llm_profile"]`` by Portail, derived from the
channel's ``channel_profile`` (or ``"default"``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import resolve_config_path
from common.user_record import UserRecord

logger = logging.getLogger(__name__)

_API_KEY_SALT: bytes = os.environ.get("RELAIS_API_KEY_SALT", "").encode()
if not _API_KEY_SALT:
    logger.warning(
        "RELAIS_API_KEY_SALT is not set — REST API key hashes use an empty salt. "
        "Set this env variable to a random secret for stronger protection."
    )


def _hash_api_key(raw_key: str) -> str:
    """Hash a raw REST API key using HMAC-SHA256 with the deployment salt."""
    return hmac.new(_API_KEY_SALT, raw_key.encode(), hashlib.sha256).hexdigest()



class UserRegistry:
    """Loads portail.yaml and exposes fast sender_id → UserRecord lookups.

    Resolves identity, merges role data into each ``UserRecord``, and provides
    ``build_guest_record()`` for unknown users under a ``guest`` policy.

    Falls back to permissive mode (returns ``None`` for every lookup) when
    ``portail.yaml`` cannot be found or parsed.

    Identity lookup uses a ``"channel:raw_id"`` key so that callers pass the
    same ``sender_id`` format used throughout the Envelope pipeline.

    Args:
        config_path: Explicit path to ``portail.yaml``.  When ``None``, the
            standard config-cascade is tried
            (``portail.yaml`` → ``portail.yaml.default``).
    """

    def __init__(self, config_path: Path | None = None) -> None:
        """Initialise the registry and load the user/role tables from disk.

        Args:
            config_path: Optional explicit path to portail.yaml.
        """
        self._config_path: Path | None = self._resolve_path(config_path)
        # "channel:raw_id" → UserRecord
        self._sender_index: dict[str, UserRecord] = {}
        # (channel, context, raw_id) → UserRecord
        self._by_identifier: dict[tuple[str, str, str], UserRecord] = {}
        # raw role dict for build_guest_record
        self._roles_raw: dict[str, dict[str, Any]] = {}
        # portail.yaml top-level policy fields
        self._unknown_user_policy: str = "deny"
        self._guest_role: str = "guest"
        self._permissive: bool = True
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_permissive(self) -> bool:
        """Return True when the registry has no loaded configuration.

        Returns:
            True if no portail.yaml was successfully loaded (permissive/pass-through
            mode); False once a valid config has been parsed.
        """
        return self._permissive

    def resolve_user(
        self,
        sender_id: str,
        channel: str,
        context: str = "dm",
    ) -> UserRecord | None:
        """Return the fully-merged UserRecord matching sender_id, channel, context.

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

        # Exact (channel, context, raw_id) match — highest precision.
        record = self._by_identifier.get((channel, context, raw_id))
        if record is not None:
            return record

        # sender_index fallback: works across contexts but within the channel.
        # REST: the auth middleware already resolved the API key and stamped
        # sender_id="rest:<user_id>".  Portail does a direct lookup — no
        # hashing.  The sender_index contains both "rest:<user_id>" and
        # "rest:<key_hash>" entries (built at load time).
        return self._sender_index.get(f"{channel}:{raw_id}")

    def resolve_rest_api_key(self, raw_key: str) -> UserRecord | None:
        """Resolve a raw REST API key to a UserRecord.

        Hashes the key with HMAC-SHA256 and looks it up in the
        sender_index.  Used exclusively by the REST auth middleware.
        ``resolve_user`` does NOT hash — it expects a pre-resolved
        sender_id (e.g. ``rest:usr_admin``).

        Args:
            raw_key: The raw API key from the Authorization header.

        Returns:
            A ``UserRecord`` if the key is valid, ``None`` otherwise.
        """
        key_hash = _hash_api_key(raw_key)
        return self._sender_index.get(f"rest:{key_hash}")

    def build_guest_record(self) -> UserRecord:
        """Build a synthetic guest UserRecord with role data from the configured guest role.

        Used by Portail when ``unknown_user_policy=guest``.  The role name is
        read from ``guest_role`` (portail.yaml) so it can be customised without
        code changes.  Actions, skills_dirs, and allowed_mcp_tools come from the
        matching role config, or fall back to empty lists (fail-closed).

        Returns:
            A valid, non-blocked ``UserRecord`` with the configured guest role.
        """
        role_name: str = self._guest_role
        role_def: dict[str, Any] = self._roles_raw.get(role_name) or {}
        return UserRecord(
            user_id="guest",
            display_name="Guest",
            role=role_name,
            blocked=False,
            actions=list(role_def.get("actions") or []),
            skills_dirs=list(role_def.get("skills_dirs") or []),
            allowed_mcp_tools=list(role_def.get("allowed_mcp_tools") or []),
            allowed_subagents=list(role_def.get("allowed_subagents") or []),
            prompt_path=None,
            role_prompt_path=role_def.get("prompt_path") or None,
        )

    @property
    def unknown_user_policy(self) -> str:
        """The configured unknown_user_policy from portail.yaml (or 'deny' default)."""
        return self._unknown_user_policy

    @property
    def guest_role(self) -> str:
        """The configured guest_role from portail.yaml (or 'guest' default)."""
        return self._guest_role

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

        for filename in ("portail.yaml", "portail.yaml.default"):
            try:
                return resolve_config_path(filename)
            except FileNotFoundError:
                continue

        return None

    def _load(self) -> None:
        """Parse portail.yaml and populate the lookup indexes.

        Merges role-level fields (actions, skills_dirs, allowed_mcp_tools,
        prompt_path) into every ``UserRecord`` at load time.

        Resolution priority:
        - ``prompt_path``: user-level only (no role fallback) — ``None`` when absent
        - ``role_prompt_path``: role-level only — ``None`` when absent
        - ``actions``, ``skills_dirs``, ``allowed_mcp_tools``: role-level only

        ``llm_profile`` is NOT loaded here — Portail stamps it directly into
        ``envelope.context["portail"]`` from the channel's ``channel_profile``.

        Enters permissive mode (empty indexes) when the file cannot be found
        or parsed.
        """
        if self._config_path is None:
            logger.warning(
                "UserRegistry: portail.yaml not found — running in permissive mode "
                "(all resolve_user calls will return None)."
            )
            return

        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data: dict[str, Any] = yaml.safe_load(raw) or {}
        except Exception as exc:  # noqa: BLE001
            logger.critical(
                "UserRegistry: failed to parse %s — %s — falling back to permissive mode "
                "(all users will appear unknown — review config immediately)",
                self._config_path,
                exc,
            )
            return

        # Read top-level portail policy fields
        raw_policy = str(data.get("unknown_user_policy") or "deny").lower()
        if raw_policy not in ("deny", "guest", "pending"):
            logger.warning(
                "UserRegistry: unknown unknown_user_policy=%r — falling back to 'deny'",
                raw_policy,
            )
            raw_policy = "deny"
        self._unknown_user_policy = raw_policy
        self._guest_role = str(data.get("guest_role") or "guest")

        roles_raw: dict[str, dict[str, Any]] = data.get("roles") or {}
        self._roles_raw = roles_raw

        new_sender_index: dict[str, UserRecord] = {}
        new_by_identifier: dict[tuple[str, str, str], UserRecord] = {}

        for user_id, user in (data.get("users") or {}).items():
            role_name = str(user.get("role") or "")
            role_def: dict[str, Any] = roles_raw.get(role_name) or {}

            # prompt_path: user-level only (no role fallback)
            raw_prompt_path = user.get("prompt_path") or None
            if raw_prompt_path is not None:
                raw_prompt_path = self._validate_path(raw_prompt_path)

            # role_prompt_path: role-level only
            raw_role_prompt_path = role_def.get("prompt_path") or None
            if raw_role_prompt_path is not None:
                raw_role_prompt_path = self._validate_path(raw_role_prompt_path)

            record = UserRecord(
                user_id=user_id,
                display_name=str(user.get("display_name") or ""),
                role=role_name,
                blocked=bool(user.get("blocked", False)),
                actions=list(role_def.get("actions") or []),
                skills_dirs=list(role_def.get("skills_dirs") or []),
                allowed_mcp_tools=list(role_def.get("allowed_mcp_tools") or []),
                allowed_subagents=list(role_def.get("allowed_subagents") or []),
                prompt_path=raw_prompt_path,
                role_prompt_path=raw_role_prompt_path,
            )

            identifiers: dict[str, Any] = user.get("identifiers") or {}
            for ch, contexts in identifiers.items():
                if not contexts:
                    continue
                if ch == "rest":
                    for key in contexts.get("api_keys") or []:
                        if key:
                            key_hash = _hash_api_key(str(key))
                            new_sender_index[f"rest:{key_hash}"] = record
                    # Also index by user_id so Portail can resolve
                    # sender_id="rest:usr_xxx" stamped by the REST auth
                    # middleware (which replaces the raw API key with the
                    # stable user_id before publishing to the pipeline).
                    new_sender_index[f"rest:{user_id}"] = record
                else:
                    for ctx, raw_id in contexts.items():
                        if raw_id:
                            sid = str(raw_id)
                            new_by_identifier[(ch, ctx, sid)] = record
                            new_sender_index[f"{ch}:{sid}"] = record

        self._sender_index = new_sender_index
        self._by_identifier = new_by_identifier
        self._permissive = False

        logger.info(
            "UserRegistry loaded: %d identifiers from %s",
            len(self._sender_index),
            self._config_path,
        )

    @staticmethod
    def _validate_path(raw_path: str) -> str | None:
        """Validate a prompt_path value, rejecting traversal or absolute paths.

        Args:
            raw_path: The raw path string from configuration.

        Returns:
            The original string if safe, ``None`` if rejected.
        """
        p = Path(raw_path)
        if p.is_absolute() or any(part == ".." for part in p.parts):
            logger.warning(
                "UserRegistry: prompt_path %r rejected — "
                "absolute path or directory traversal detected",
                raw_path,
            )
            return None
        return raw_path
