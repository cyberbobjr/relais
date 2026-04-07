"""SkillAnnotator — lightweight per-turn inline skill annotation.

After a turn where a skill was used and produced tool errors, the annotator
calls a fast LLM to generate a concise inline annotation block and appends it
to the SKILL.md file under a ``## Lessons Learned`` section.

This is complementary to Forgeron's statistical trace analysis pipeline:
  - Annotations fire immediately, on every error-laden turn (low latency).
  - Forgeron fires only after statistical thresholds are met (high precision).

Rate-limiting is done via a per-skill Redis key with a short TTL so repeated
errors in rapid succession don't flood the annotation section.
"""

from __future__ import annotations

import json
import logging
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from common.profile_loader import ProfileConfig, build_chat_model

if TYPE_CHECKING:
    from forgeron.config import ForgeonConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt (intentionally concise — fast model)
# ---------------------------------------------------------------------------

_ANNOTATION_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert at writing concise improvement notes for AI skill files.

    Given a SKILL.md and a conversation where tool errors occurred, produce a
    SINGLE short annotation (2-5 bullet points) that would help an AI agent
    avoid those specific errors next time.

    Rules:
    - Focus ONLY on what went wrong in this specific conversation.
    - Be concrete: wrong command flags, missing preconditions, wrong order, etc.
    - Do NOT restate the full skill instructions — only add what's missing.
    - Use imperative second-person language: "Run X before Y", "Always include --flag".
    - Keep each bullet to one sentence.

    Respond with ONLY the bullet list (no headings, no JSON, no fences):
    - <bullet 1>
    - <bullet 2>
    ...
""")


class SkillAnnotator:
    """Append per-turn error lessons to a skill file.

    Args:
        profile: A ``ProfileConfig`` instance for the fast annotation model.
        skills_dir: Root directory containing skill subdirectories.
    """

    def __init__(self, profile: ProfileConfig, skills_dir: Path | None) -> None:
        self._profile = profile
        self._skills_dir = skills_dir

    async def maybe_annotate(
        self,
        skill_name: str,
        tool_error_count: int,
        messages_raw: list[dict],
        config: "ForgeonConfig",
        redis_conn: Any,
    ) -> bool:
        """Annotate a skill file if errors occurred and cooldown allows.

        Args:
            skill_name: Directory name of the skill.
            tool_error_count: Number of tool errors in this turn.
            messages_raw: Full LangChain message list for this turn.
            config: Forgeron config (annotation thresholds and model).
            redis_conn: Redis connection for cooldown key management.

        Returns:
            ``True`` if an annotation was appended, ``False`` otherwise.
        """
        if tool_error_count < config.annotation_min_tool_errors:
            return False

        cooldown_key = f"relais:skill:annotation_cooldown:{skill_name}"
        ttl = await redis_conn.ttl(cooldown_key)
        if ttl > 0:
            logger.debug(
                "Annotation cooldown active for skill '%s' (%ds remaining)",
                skill_name,
                ttl,
            )
            return False

        skill_path = self._resolve_skill_path(skill_name)
        if skill_path is None:
            return False

        try:
            skill_content = skill_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not read skill file '%s' for annotation: %s", skill_path, exc
            )
            return False

        annotation = await self._generate_annotation(
            skill_name, skill_content, messages_raw
        )
        if not annotation:
            return False

        self._append_annotation(skill_path, annotation)

        # Set cooldown to avoid annotation spam
        await redis_conn.set(
            cooldown_key, "1", ex=config.annotation_cooldown_seconds
        )

        logger.info(
            "Annotation appended to skill '%s' (%d error(s) this turn)",
            skill_name,
            tool_error_count,
        )
        return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_skill_path(self, skill_name: str) -> Path | None:
        """Resolve the SKILL.md path for a skill directory name.

        Args:
            skill_name: Directory name of the skill.

        Returns:
            Path to the SKILL.md file, or ``None`` if not found.
        """
        if self._skills_dir is None:
            return None
        candidate = self._skills_dir / skill_name / "SKILL.md"
        if not candidate.exists():
            logger.debug("Skill file not found for annotation: %s", candidate)
            return None
        return candidate

    async def _generate_annotation(
        self,
        skill_name: str,
        skill_content: str,
        messages_raw: list[dict],
    ) -> str:
        """Call the LLM to generate a concise annotation bullet list.

        Args:
            skill_name: Skill name (for logging).
            skill_content: Current SKILL.md text.
            messages_raw: Conversation messages for this turn.

        Returns:
            Annotation text (raw bullet list), or empty string on failure.
        """
        conversation_summary = self._format_conversation(messages_raw)
        user_message = (
            f"# Skill: {skill_name}\n\n"
            "## Current SKILL.md\n"
            f"```markdown\n{skill_content}\n```\n\n"
            "## Conversation with errors\n"
            f"{conversation_summary}\n\n"
            "What lessons should be added to this skill file?"
        )

        try:
            llm = build_chat_model(self._profile)
            response = await llm.ainvoke(
                [
                    SystemMessage(content=_ANNOTATION_SYSTEM_PROMPT),
                    HumanMessage(content=user_message),
                ]
            )
            annotation = str(response.content).strip()
            if not annotation:
                logger.warning(
                    "LLM returned empty annotation for skill '%s'", skill_name
                )
                return ""
            return annotation
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Annotation LLM call failed for skill '%s': %s", skill_name, exc
            )
            return ""

    @staticmethod
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

    @staticmethod
    def _append_annotation(skill_path: Path, annotation: str) -> None:
        """Append an annotation block under the ``## Lessons Learned`` section.

        Creates the section header if it doesn't exist yet.

        Args:
            skill_path: Absolute path to the SKILL.md file.
            annotation: Raw bullet-point annotation text from the LLM.
        """
        current = skill_path.read_text(encoding="utf-8")
        timestamp = time.strftime("%Y-%m-%d")
        block = f"\n### Auto-annotation ({timestamp})\n{annotation}\n"

        if "## Lessons Learned" in current:
            updated = current + block
        else:
            updated = current + f"\n## Lessons Learned\n{block}"

        skill_path.write_text(updated, encoding="utf-8")
