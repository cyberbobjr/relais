"""SkillEditor — single-step direct SKILL.md improvement.

Replaces the two-phase ChangelogWriter + SkillConsolidator pipeline with a
single LLM call that rewrites SKILL.md directly from conversation traces,
scoped to one specific skill.

The scope filter is a best-effort heuristic that keeps only ToolMessage entries
relevant to the target skill, reducing cross-contamination when multiple skills
are used in the same conversation.

Atomic writes use a ``.tmp`` file + ``os.replace()`` (POSIX-atomic).
"""

from __future__ import annotations

import logging
import os
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from common.profile_loader import ProfileConfig
from forgeron.llm_factory import build_chat_model

if TYPE_CHECKING:
    from forgeron.config import ForgeonConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------


class SkillEditResult(BaseModel):
    """Structured output schema for the skill edit LLM call."""

    updated_skill: str = Field(
        description="Full rewritten SKILL.md content with improvements integrated."
    )
    changed: bool = Field(
        description="True if the file was meaningfully changed, False if no new lessons found."
    )
    reason: str = Field(
        default="",
        description="Short explanation of what was changed or why nothing changed.",
    )


# ---------------------------------------------------------------------------
# Message scoping
# ---------------------------------------------------------------------------


def scope_messages_to_skill(messages_raw: list[dict], skill_name: str) -> list[dict]:
    """Filter conversation messages to those relevant to a specific skill.

    Keeps all HumanMessage and AIMessage entries for intent context.
    Keeps ToolMessage entries only when the immediately preceding AIMessage's
    tool_calls contained a call to ``read_skill`` with args referencing the
    target skill name.

    Falls back to the full list when the filtered result has fewer than 3 messages,
    which avoids sending an empty or near-empty context to the LLM.

    Args:
        messages_raw: Serialized LangChain message list for a conversation turn.
        skill_name: Skill directory name (e.g. ``"mail-summary"``).

    Returns:
        Filtered list of messages, or the full list as fallback.
    """
    if not messages_raw:
        return messages_raw

    # Identify which tool call IDs are relevant to this skill.
    # A tool call is relevant when it calls read_skill and references skill_name.
    relevant_tool_call_ids: set[str] = set()
    for msg in messages_raw:
        msg_type = msg.get("type", msg.get("role", ""))
        if msg_type not in ("ai", "AIMessage", "assistant"):
            continue
        tool_calls = msg.get("tool_calls") or []
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name", "")
                args = tc.get("args") or tc.get("arguments") or {}
                if isinstance(args, str):
                    # Sometimes args is a JSON string; treat it as text for matching
                    if name == "read_skill" and skill_name in args:
                        tc_id = tc.get("id") or tc.get("tool_call_id", "")
                        if tc_id:
                            relevant_tool_call_ids.add(tc_id)
                elif isinstance(args, dict):
                    skill_arg = args.get("skill_name") or args.get("name") or args.get("skill") or ""
                    if name == "read_skill" and skill_name in str(skill_arg):
                        tc_id = tc.get("id") or tc.get("tool_call_id", "")
                        if tc_id:
                            relevant_tool_call_ids.add(tc_id)

    filtered: list[dict] = []
    for msg in messages_raw:
        msg_type = msg.get("type", msg.get("role", ""))
        if msg_type in ("tool", "ToolMessage"):
            tc_id = msg.get("tool_call_id", "")
            if tc_id and relevant_tool_call_ids and tc_id not in relevant_tool_call_ids:
                # Skip tool results not relevant to this skill
                continue
        filtered.append(msg)

    if len(filtered) < 3:
        return messages_raw
    return filtered


# ---------------------------------------------------------------------------
# SkillEditor
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are improving the skill documentation file for ONE specific skill: '{skill_name}'.

    STRICT SCOPE RULE: Only incorporate observations that are DIRECTLY and SPECIFICALLY about '{skill_name}'.
    Ignore any content about other skills, unrelated tool calls, or off-topic conversations.
    If the conversation contains no new lesson specifically about '{skill_name}', set changed=false and return the current file unchanged.

    When you do find relevant improvements:
    - Preserve file structure (front-matter, headings, existing sections)
    - Add only concrete, imperative instructions
    - Do not add dates, metadata, or changelog entries to the skill body
    - Integrate new knowledge into existing sections where appropriate
""")


class SkillEditor:
    """Rewrite SKILL.md directly from conversation traces, scoped to one skill.

    Args:
        profile: ProfileConfig for the edit LLM.
        skills_dir: Root directory containing per-skill subdirectories.
    """

    def __init__(self, profile: ProfileConfig, skills_dir: Path | None) -> None:
        self._profile = profile
        self._skills_dir = skills_dir

    async def edit(
        self,
        skill_name: str,
        messages_raw: list[dict],
        config: "ForgeonConfig",
        redis_conn: Any,
        *,
        trigger_reason: str = "",
        force: bool = False,
        skill_path: Path | None = None,
    ) -> bool:
        """Rewrite SKILL.md using scoped conversation traces.

        Args:
            skill_name: Directory name of the skill under ``skills_dir``.
            messages_raw: Serialized LangChain message list for this turn.
            config: Forgeron runtime config (thresholds, cooldown TTLs).
            redis_conn: Active async Redis connection.
            trigger_reason: Human-readable reason string for logging.
            force: Skip cooldown check (e.g. for usage-threshold triggers).
            skill_path: Explicit skill directory path override.

        Returns:
            ``True`` if SKILL.md was rewritten, ``False`` otherwise.
        """
        cooldown_key = f"relais:skill:edit_cooldown:{skill_name}"
        if not force:
            ttl = await redis_conn.ttl(cooldown_key)
            if ttl > 0:
                logger.debug(
                    "Edit cooldown active for skill '%s' (%ds remaining).",
                    skill_name,
                    ttl,
                )
                return False

        skill_md = self._resolve_skill_md(skill_name, skill_path)
        if skill_md is None:
            return False

        try:
            current_content = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read SKILL.md for '%s': %s", skill_name, exc)
            return False

        scoped = scope_messages_to_skill(messages_raw, skill_name)

        result = await self._call_llm(skill_name, current_content, scoped)
        if result is None:
            return False

        updated = result.updated_skill.strip()
        if not result.changed or not updated:
            logger.debug(
                "No changes for skill '%s': %s",
                skill_name,
                result.reason or "LLM found nothing new",
            )
            return False

        if updated == current_content.strip():
            logger.debug(
                "Skill '%s' content unchanged after strip — skipping write.",
                skill_name,
            )
            return False

        tmp_path = skill_md.with_suffix(".tmp")
        try:
            tmp_path.write_text(updated, encoding="utf-8")
            os.replace(str(tmp_path), str(skill_md))
        except OSError as exc:
            logger.error("Atomic write failed for skill '%s': %s", skill_name, exc)
            return False

        await redis_conn.setex(cooldown_key, config.edit_cooldown_seconds, "1")

        logger.info(
            "[FORGERON] edited skill '%s' (trigger=%s): %s",
            skill_name,
            trigger_reason,
            result.reason,
        )
        return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_skill_md(
        self, skill_name: str, skill_path: Path | None
    ) -> Path | None:
        """Return the SKILL.md path for a skill, or None if not found.

        Args:
            skill_name: Directory name of the skill.
            skill_path: Explicit override directory (bypasses ``skills_dir``).

        Returns:
            Absolute Path to SKILL.md if it exists, else None.
        """
        relais_home = os.environ.get("RELAIS_HOME")
        if skill_path is not None:
            candidate = skill_path / "SKILL.md"
            if candidate.exists():
                return candidate
            logger.debug("SKILL.md not found at override path: %s", candidate)
            return None

        # Explicit skills_dir takes priority over RELAIS_HOME so that callers
        # (including tests) can control resolution without the environment
        # variable interfering.
        if self._skills_dir is not None:
            skill_dir = (self._skills_dir / skill_name).resolve()
            if not skill_dir.is_relative_to(self._skills_dir.resolve()):
                logger.warning("Path traversal blocked for skill '%s'.", skill_name)
                return None
            candidate = skill_dir / "SKILL.md"
            if candidate.exists():
                return candidate

        if relais_home:
            candidate = Path(relais_home) / "skills" / skill_name / "SKILL.md"
            if candidate.exists():
                return candidate

        logger.debug("SKILL.md not found for skill '%s'.", skill_name)
        return None

    async def _call_llm(
        self,
        skill_name: str,
        current_content: str,
        messages_raw: list[dict],
    ) -> SkillEditResult | None:
        """Call the LLM with structured output to produce a rewritten SKILL.md.

        Args:
            skill_name: Skill name for prompt context and logging.
            current_content: Current SKILL.md content.
            messages_raw: Scoped conversation messages.

        Returns:
            ``SkillEditResult`` on success, ``None`` on LLM failure.
        """
        conversation = _format_conversation(messages_raw)
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(skill_name=skill_name)
        user_message = (
            f"# Skill: {skill_name}\n\n"
            "## Current SKILL.md\n"
            f"```markdown\n{current_content}\n```\n\n"
            "## Conversation trace\n"
            f"{conversation}\n\n"
            "Rewrite SKILL.md with any improvements specifically about this skill."
        )

        try:
            llm = build_chat_model(self._profile)
            structured_llm = llm.with_structured_output(SkillEditResult)
            return cast(
                SkillEditResult,
                await structured_llm.ainvoke(
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_message),
                    ]
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Edit LLM call failed for skill '%s': %s", skill_name, exc)
            return None


def _format_conversation(messages_raw: list[dict]) -> str:
    """Render a LangChain message list as readable text.

    Args:
        messages_raw: List of LangChain message dicts.

    Returns:
        Formatted string with role and truncated content.
    """
    parts: list[str] = []
    for msg in messages_raw:
        role = msg.get("type", msg.get("role", "unknown"))
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content
            )
        parts.append(f"**{role}**: {str(content)[:1500]}")
    return "\n".join(parts)
