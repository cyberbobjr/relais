"""Role registry — loads the ``roles:`` section of users.yaml.

Provides fast role-name → RoleConfig lookups for ``skills_dirs`` and
``allowed_mcp_tools`` fields used by Portail (stamping) and Sentinelle
(cross-check).

The registry is intentionally separate from ``ACLManager`` (which owns
access-control decisions) and from ``UserRegistry`` (which owns identity
resolution).  Keeping them separate avoids coupling and lets each consumer
depend only on what it needs.

Falls back to permissive mode (returns ``None`` for every ``get_role`` call)
when ``users.yaml`` cannot be found or parsed, matching the same convention
used by ``UserRegistry``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import resolve_config_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoleConfig:
    """Immutable snapshot of a single role's configuration from users.yaml.

    Attributes:
        actions: Permitted action names (e.g. ``"send"``, ``"admin"``).
        skills_dirs: Skill directory names (or ``("*",)`` for unrestricted)
            relative to the configured ``paths.skills`` root.
        allowed_mcp_tools: Allowed MCP tool identifiers, supporting fnmatch
            glob patterns (e.g. ``"search__*"``).  ``("*",)`` means all tools.
        prompt_path: Optional relative path to a role-level prompt overlay
            (e.g. ``"roles/admin.md"``).  Used as a fallback when the user has
            no ``custom_prompt_path`` of their own.  ``None`` when absent.
    """

    actions: tuple[str, ...] = field(default_factory=tuple)
    skills_dirs: tuple[str, ...] = field(default_factory=tuple)
    allowed_mcp_tools: tuple[str, ...] = field(default_factory=tuple)
    prompt_path: str | None = None


class RoleRegistry:
    """Loads the ``roles:`` section of users.yaml and exposes role lookups.

    Falls back to permissive mode (returns ``None`` for every ``get_role``
    call) when the config file cannot be found or parsed.

    Args:
        config_path: Explicit path to ``users.yaml``.  When ``None``, the
            standard config-cascade is tried
            (``users.yaml`` → ``users.yaml.default``).
    """

    def __init__(self, config_path: Path | None = None) -> None:
        """Initialise the registry and load roles from disk.

        Args:
            config_path: Optional explicit path to users.yaml.
        """
        self._config_path: Path | None = self._resolve_path(config_path)
        self._roles: dict[str, RoleConfig] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_role(self, role_name: str) -> RoleConfig | None:
        """Return the RoleConfig for the given role name.

        Args:
            role_name: The role identifier as defined in users.yaml
                (e.g. ``"admin"``, ``"user"``).

        Returns:
            A ``RoleConfig`` when the role is defined, ``None`` otherwise
            (including permissive mode where the config file is absent).
        """
        return self._roles.get(role_name)

    def reload(self) -> None:
        """Reload users.yaml from disk, replacing the roles table atomically.

        Args: (none)

        Returns: None
        """
        self._roles = {}
        self._load()
        logger.info("RoleRegistry reloaded from %s", self._config_path)

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

    def _normalize_tuple(self, raw: Any) -> tuple[str, ...]:
        """Convert a raw YAML value to a normalised ``tuple[str, ...]``.

        Mapping rules:
        - ``None`` / empty list → ``()``
        - Bare string → ``(string,)``
        - Non-empty list → ``tuple(str(x) for x in raw)``

        Args:
            raw: The raw value from the parsed YAML dict.

        Returns:
            A normalised immutable tuple of strings.
        """
        if not raw:
            return ()
        if isinstance(raw, str):
            return (raw,)
        return tuple(str(x) for x in raw)

    def _load(self) -> None:
        """Parse the ``roles:`` section of users.yaml and populate the table.

        Enters permissive mode (empty table) when the file cannot be found
        or parsed.  In permissive mode every ``get_role`` call returns ``None``.

        Returns: None
        """
        if self._config_path is None:
            logger.warning(
                "RoleRegistry: users.yaml not found — running in permissive mode "
                "(all get_role calls will return None)."
            )
            return

        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data: dict[str, Any] = yaml.safe_load(raw) or {}
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "RoleRegistry: failed to parse %s — %s — falling back to permissive mode",
                self._config_path,
                exc,
            )
            return

        new_roles: dict[str, RoleConfig] = {}
        for role_name, role_data in (data.get("roles") or {}).items():
            rd: dict[str, Any] = role_data or {}
            raw_prompt = rd.get("prompt_path") or None
            if raw_prompt is not None:
                p = Path(raw_prompt)
                if p.is_absolute() or any(part == ".." for part in p.parts):
                    logger.warning(
                        "RoleRegistry: prompt_path %r rejected for role %r — "
                        "absolute path or directory traversal detected",
                        raw_prompt,
                        role_name,
                    )
                    raw_prompt = None
            new_roles[str(role_name)] = RoleConfig(
                actions=self._normalize_tuple(rd.get("actions")),
                skills_dirs=self._normalize_tuple(rd.get("skills_dirs")),
                allowed_mcp_tools=self._normalize_tuple(rd.get("allowed_mcp_tools")),
                prompt_path=raw_prompt,
            )

        self._roles = new_roles
        logger.info(
            "RoleRegistry loaded: %d roles from %s",
            len(self._roles),
            self._config_path,
        )
