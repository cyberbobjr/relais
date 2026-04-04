"""Subagent registry with auto-discovery and role-based filtering.

Scans ``atelier/agents/`` at startup, loads all valid subagent modules,
and provides methods to filter specs and assemble delegation prompts
based on the user record stamped by Portail.
"""

from __future__ import annotations

import fnmatch
import importlib
import logging
import pkgutil
from dataclasses import dataclass
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

_DELEGATION_PREAMBLE = """
Subagent delegation:
- You have access to specialized subagents via the task() tool.
- When a request matches one of the subagents below, delegate via task() \
instead of doing it yourself. Delegating keeps your context clean and \
produces better results.
""".strip()

_REQUIRED_ATTRS = ("SPEC_NAME", "build_spec", "delegation_snippet")


def _is_valid_subagent_module(mod: ModuleType) -> bool:
    """Check whether *mod* exposes all required subagent attributes.

    Args:
        mod: An imported Python module.

    Returns:
        True if the module has ``SPEC_NAME``, ``build_spec``, and
        ``delegation_snippet``.
    """
    return all(hasattr(mod, attr) for attr in _REQUIRED_ATTRS)


def _parse_subagent_patterns(raw: object) -> tuple[str, ...]:
    """Normalise an ``allowed_subagents`` value into a tuple of strings.

    Follows the same boundary-sanitisation pattern as
    ``ToolPolicy._parse_policy``: only ``list`` or ``tuple`` inputs are
    accepted; anything else (None, str, int, dict) returns an empty tuple
    (fail-closed).

    Args:
        raw: The raw value from ``user_record.get("allowed_subagents")``.

    Returns:
        A tuple of strings suitable for fnmatch filtering.
    """
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    return ()


def _matches_patterns(name: str, patterns: tuple[str, ...]) -> bool:
    """Return True if *name* matches any fnmatch pattern in *patterns*.

    Args:
        name: The subagent ``SPEC_NAME`` to test.
        patterns: Tuple of fnmatch-style glob patterns.

    Returns:
        True if at least one pattern matches.
    """
    return any(fnmatch.fnmatch(name, p) for p in patterns)


@dataclass(frozen=True)
class SubagentRegistry:
    """Immutable registry of discovered subagent modules.

    Built once at ``Atelier.__init__`` time via ``discover()``, then
    queried per-request to filter specs and assemble delegation prompts
    based on the user record.
    """

    _modules: tuple[ModuleType, ...]

    @classmethod
    def discover(cls) -> SubagentRegistry:
        """Scan ``atelier.agents`` and load all valid subagent modules.

        Skips modules whose names start with ``_`` (internal convention).
        Logs a warning and skips any module that fails to import or does
        not expose the required attributes.

        Returns:
            A frozen ``SubagentRegistry`` containing all valid modules.
        """
        import atelier.agents as package

        modules: list[ModuleType] = []
        for finder, name, _ispkg in pkgutil.iter_modules(package.__path__):
            if name.startswith("_"):
                continue
            fqn = f"atelier.agents.{name}"
            try:
                mod = importlib.import_module(fqn)
            except Exception:
                logger.warning("Failed to import subagent module %s", fqn, exc_info=True)
                continue
            if not _is_valid_subagent_module(mod):
                logger.warning(
                    "Skipping %s — missing required attributes %s",
                    fqn,
                    [a for a in _REQUIRED_ATTRS if not hasattr(mod, a)],
                )
                continue
            logger.info("Discovered subagent: %s (module=%s)", mod.SPEC_NAME, fqn)
            modules.append(mod)

        return cls(_modules=tuple(modules))

    @property
    def all_names(self) -> frozenset[str]:
        """Return all discovered subagent SPEC_NAME values.

        Returns:
            A frozenset of subagent identifier strings.
        """
        return frozenset(mod.SPEC_NAME for mod in self._modules)

    def specs_for_user(self, user_record: dict[str, Any]) -> list[dict[str, Any]]:
        """Return subagent specs allowed for the given user.

        Reads ``user_record["allowed_subagents"]``, parses the patterns,
        and filters discovered modules by fnmatch on ``SPEC_NAME``.

        Args:
            user_record: The user record dict stamped by Portail.

        Returns:
            A list of SubAgent spec dicts for ``create_deep_agent(subagents=...)``.
        """
        patterns = _parse_subagent_patterns(user_record.get("allowed_subagents"))
        if not patterns:
            return []
        return [
            mod.build_spec()
            for mod in self._modules
            if _matches_patterns(mod.SPEC_NAME, patterns)
        ]

    def delegation_prompt_for_user(self, user_record: dict[str, Any]) -> str:
        """Assemble the delegation prompt for the given user.

        Prepends a generic preamble, then appends one snippet per
        allowed subagent. Returns an empty string if no subagents
        match — the caller should skip appending to the system prompt.

        Args:
            user_record: The user record dict stamped by Portail.

        Returns:
            The full delegation prompt string, or ``""`` if no subagents.
        """
        patterns = _parse_subagent_patterns(user_record.get("allowed_subagents"))
        if not patterns:
            return ""
        snippets = [
            mod.delegation_snippet()
            for mod in self._modules
            if _matches_patterns(mod.SPEC_NAME, patterns)
        ]
        if not snippets:
            return ""
        return f"{_DELEGATION_PREAMBLE}\n\n" + "\n".join(snippets)
