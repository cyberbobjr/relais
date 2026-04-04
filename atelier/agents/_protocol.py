"""Protocol defining the contract for subagent modules.

Each subagent is a Python module under ``atelier/agents/`` that exposes
the names defined by this protocol. The ``SubagentRegistry`` validates
modules against this contract at discovery time via duck-typing
(``hasattr`` checks), and type-checkers can verify it statically.

A valid subagent module exposes exactly three names:

- ``SPEC_NAME`` — unique identifier (used for fnmatch filtering)
- ``build_spec()`` — returns the deepagents SubAgent dict
- ``delegation_snippet()`` — returns the prompt snippet for delegation
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SubagentModule(Protocol):
    """Protocol for subagent modules discovered by ``SubagentRegistry``.

    This is a structural (duck-typed) protocol — modules do not need to
    inherit from it.  It exists for documentation, type-checking, and
    the ``isinstance()`` guard in the registry's ``discover()`` method.
    """

    SPEC_NAME: str
    """Unique subagent identifier. Lowercase, hyphens allowed.

    Used for fnmatch filtering against ``allowed_subagents`` patterns
    in the user record (e.g. ``["*"]``, ``["config-*"]``).
    """

    def build_spec(self) -> dict[str, Any]:
        """Build the SubAgent spec dict for ``create_deep_agent(subagents=...)``.

        Returns:
            A dict with at least ``name``, ``description``, and
            ``system_prompt`` keys.
        """
        ...

    def delegation_snippet(self) -> str:
        """Return a short markdown snippet describing when to delegate.

        This text is assembled into the main agent's delegation prompt
        by the ``SubagentRegistry``. Keep it to 2-4 lines.

        Returns:
            A markdown string, e.g. ``"- **name**: Use when ..."``.
        """
        ...
