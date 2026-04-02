"""UserRecord — immutable user profile shared across all bricks.

Downstream bricks (Sentinelle, Atelier) import this dataclass to deserialize
``envelope.metadata["user_record"]``.  Portail is the sole writer; all other
bricks are read-only consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UserRecord:
    """Immutable snapshot of a single user's fully-merged profile.

    All role-level fields (actions, skills_dirs, allowed_mcp_tools,
    llm_profile, prompt_path) are already merged in by ``UserRegistry._load()``
    so that callers never need to consult a separate registry.

    Attributes:
        display_name: Human-readable name for the user.
        role: Role name (e.g. ``"admin"``, ``"user"``, ``"guest"``).
        blocked: Whether the user account is blocked.
        actions: List of allowed slash command names; ``["*"]`` grants all.
        skills_dirs: List of allowed skill directory names; ``["*"]`` = all.
        allowed_mcp_tools: List of allowed MCP tool identifiers; ``["*"]`` = all.
        llm_profile: LLM profile name (e.g. ``"default"``, ``"fast"``).
        prompt_path: Relative path to a prompt overlay file, or ``None``.
    """

    display_name: str
    role: str
    blocked: bool
    actions: list[str]
    skills_dirs: list[str]
    allowed_mcp_tools: list[str]
    llm_profile: str
    prompt_path: str | None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this record into a plain JSON-safe dict.

        Returns:
            A dict with all 8 fields, suitable for storing in
            ``envelope.metadata["user_record"]``.
        """
        return {
            "display_name": self.display_name,
            "role": self.role,
            "blocked": self.blocked,
            "actions": list(self.actions),
            "skills_dirs": list(self.skills_dirs),
            "allowed_mcp_tools": list(self.allowed_mcp_tools),
            "llm_profile": self.llm_profile,
            "prompt_path": self.prompt_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserRecord":
        """Reconstruct a UserRecord from a plain dict (reverse of to_dict).

        Args:
            data: Dict produced by :meth:`to_dict` or deserialized from JSON.

        Returns:
            A new frozen ``UserRecord`` instance.
        """
        return cls(
            display_name=str(data.get("display_name") or ""),
            role=str(data.get("role") or ""),
            blocked=bool(data.get("blocked", False)),
            actions=list(data.get("actions") or []),
            skills_dirs=list(data.get("skills_dirs") or []),
            allowed_mcp_tools=list(data.get("allowed_mcp_tools") or []),
            llm_profile=str(data.get("llm_profile") or "default"),
            prompt_path=data.get("prompt_path") or None,
        )
