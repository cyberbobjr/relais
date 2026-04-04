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

    Prompt-path fields are kept separate by origin so that the soul assembler
    can load each layer independently:

    - ``role_prompt_path``: explicit path from ``roles[*].prompt_path`` in
      portail.yaml.  Applied as the role overlay layer.
    - ``prompt_path``: explicit path from ``users[*].prompt_path`` in
      portail.yaml.  Applied as the per-user override layer.  ``None`` when the
      user has no ``prompt_path`` configured — the role-level path is **not**
      used as a fallback here.

    Attributes:
        user_id: Stable cross-channel identifier, equal to the YAML key in
            portail.yaml (e.g. ``"usr_admin"``).  ``"guest"`` for synthetic
            guest records.  Used by downstream bricks to resume conversations
            across channels without knowing the channel-specific sender_id.
        display_name: Human-readable name for the user.
        role: Role name (e.g. ``"admin"``, ``"user"``, ``"guest"``).
        blocked: Whether the user account is blocked.
        actions: List of allowed slash command names; ``["*"]`` grants all.
        skills_dirs: List of allowed skill directory names; ``["*"]`` = all.
        allowed_mcp_tools: List of allowed MCP tool identifiers; ``["*"]`` = all.
        allowed_subagents: List of allowed subagent names; ``["*"]`` = all.
            Filtered by fnmatch in the SubagentRegistry.
        prompt_path: Relative path to the per-user prompt overlay, or ``None``.
        role_prompt_path: Relative path to the role-level prompt overlay, or
            ``None``.

    Note:
        ``llm_profile`` is NOT a UserRecord field.  It is stamped directly
        into ``envelope.metadata["llm_profile"]`` by Portail, derived from
        the channel's ``channel_profile`` (or ``"default"``).
    """

    user_id: str
    display_name: str
    role: str
    blocked: bool
    actions: list[str]
    skills_dirs: list[str]
    allowed_mcp_tools: list[str]
    allowed_subagents: list[str]
    prompt_path: str | None
    role_prompt_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this record into a plain JSON-safe dict.

        Returns:
            A dict with all fields, suitable for storing in
            ``envelope.metadata["user_record"]``.
        """
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "role": self.role,
            "blocked": self.blocked,
            "actions": list(self.actions),
            "skills_dirs": list(self.skills_dirs),
            "allowed_mcp_tools": list(self.allowed_mcp_tools),
            "allowed_subagents": list(self.allowed_subagents),
            "prompt_path": self.prompt_path,
            "role_prompt_path": self.role_prompt_path,
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
            user_id=str(data.get("user_id") or ""),
            display_name=str(data.get("display_name") or ""),
            role=str(data.get("role") or ""),
            blocked=bool(data.get("blocked", False)),
            actions=list(data.get("actions") or []),
            skills_dirs=list(data.get("skills_dirs") or []),
            allowed_mcp_tools=list(data.get("allowed_mcp_tools") or []),
            allowed_subagents=list(data.get("allowed_subagents") or []),
            prompt_path=data.get("prompt_path") or None,
            role_prompt_path=data.get("role_prompt_path") or None,
        )
