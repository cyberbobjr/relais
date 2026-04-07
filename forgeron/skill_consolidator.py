"""SkillConsolidator — Phase 2 of S3 changelog-based skill improvement.

Periodically rewrites a SKILL.md by absorbing accumulated observations from
its CHANGELOG.md.  Produces an audit-trail entry in CHANGELOG_DIGEST.md and
clears the changelog so future observations start fresh.

Consolidation is triggered by the caller (Forgeron main loop) when
``ChangelogWriter.should_consolidate()`` returns True — i.e. the changelog
exceeds the configured line threshold and the per-skill cooldown has expired.

Atomic writes use a ``.tmp`` file + ``Path.replace()`` (POSIX-atomic).
"""

from __future__ import annotations

import logging
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from common.profile_loader import ProfileConfig, build_chat_model

if TYPE_CHECKING:
    from forgeron.config import ForgeonConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM system prompt
# ---------------------------------------------------------------------------

_CONSOLIDATION_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert at consolidating AI skill files.

    You receive two files:
    1. SKILL.md — the current skill instructions.
    2. CHANGELOG.md — a list of dated observations collected during usage.

    Your job:
    - Read every changelog entry carefully.
    - Rewrite SKILL.md so that all actionable lessons from the changelog are
      absorbed into the appropriate sections.  Remove redundant or outdated
      advice.  Keep the file concise.
    - Produce a short digest summarizing what was absorbed and what was
      discarded.

    Rules:
    - The ``updated_skill`` field must contain the COMPLETE rewritten SKILL.md.
    - Keep the original structure and tone of SKILL.md.
    - The ``digest`` field must use format: "Absorbed: N\\n- item\\nDiscarded: M\\n- item".
    - Be concise — each bullet in the digest should be one sentence.
""")

# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------


class ConsolidationResult(BaseModel):
    """Structured output schema for the consolidation LLM call."""

    updated_skill: str = Field(
        description="Full rewritten SKILL.md content with all actionable "
        "changelog observations absorbed into the appropriate sections."
    )
    digest: str = Field(
        description=(
            "Short audit summary listing what was absorbed and what was "
            "discarded.  Format: 'Absorbed: N\\n- item\\nDiscarded: M\\n- item'."
        )
    )


class SkillConsolidator:
    """Rewrite SKILL.md by absorbing CHANGELOG.md observations.

    Args:
        profile: ProfileConfig for the precise consolidation model.
        skills_dir: Root directory containing per-skill subdirectories.
    """

    def __init__(self, profile: ProfileConfig, skills_dir: Path) -> None:
        self._profile = profile
        self._skills_dir = skills_dir

    async def consolidate(
        self,
        skill_name: str,
        redis_conn: Any,
        config: "ForgeonConfig",
    ) -> bool:
        """Consolidate a skill by absorbing its changelog into SKILL.md.

        Reads SKILL.md and CHANGELOG.md, calls the LLM to produce a rewritten
        skill file and a digest, then atomically writes all three files and
        sets the Redis cooldown key.

        Args:
            skill_name: Directory name of the skill under ``skills_dir``.
            redis_conn: Active async Redis connection.
            config: Forgeron runtime config (cooldown TTL).

        Returns:
            ``True`` if consolidation succeeded, ``False`` otherwise (missing
            files, empty changelog, LLM failure, or parse failure).
        """
        skill_dir = (self._skills_dir / skill_name).resolve()
        if not skill_dir.is_relative_to(self._skills_dir.resolve()):
            logger.warning("Path traversal blocked for skill '%s'.", skill_name)
            return False
        skill_path = skill_dir / "SKILL.md"
        changelog_path = skill_dir / "CHANGELOG.md"

        if not skill_path.exists():
            logger.debug("SKILL.md not found for consolidation: %s", skill_path)
            return False

        if not changelog_path.exists():
            logger.debug("CHANGELOG.md not found for consolidation: %s", changelog_path)
            return False

        try:
            changelog_content = changelog_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read CHANGELOG.md for '%s': %s", skill_name, exc)
            return False

        if not changelog_content.strip():
            logger.debug("CHANGELOG.md is empty for skill '%s', skipping.", skill_name)
            return False

        try:
            skill_content = skill_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read SKILL.md for '%s': %s", skill_name, exc)
            return False

        # Call LLM with structured output
        try:
            llm = build_chat_model(self._profile)
            structured_llm = llm.with_structured_output(ConsolidationResult)
            user_message = (
                f"# Skill: {skill_name}\n\n"
                "## SKILL.md\n"
                f"```markdown\n{skill_content}\n```\n\n"
                "## CHANGELOG.md\n"
                f"```markdown\n{changelog_content}\n```\n"
            )
            parsed = cast(ConsolidationResult, await structured_llm.ainvoke(
                [
                    SystemMessage(content=_CONSOLIDATION_SYSTEM_PROMPT),
                    HumanMessage(content=user_message),
                ]
            ))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Consolidation LLM call failed for skill '%s': %s", skill_name, exc
            )
            return False

        if not parsed.updated_skill.strip():
            logger.warning(
                "LLM returned empty updated_skill for skill '%s'.", skill_name
            )
            return False

        # Write files atomically
        self._atomic_write(skill_path, parsed.updated_skill)

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        digest_entry = f"\n## Consolidation {timestamp}\n{parsed.digest}\n"
        digest_path = skill_dir / "CHANGELOG_DIGEST.md"
        existing_digest = ""
        if digest_path.exists():
            try:
                existing_digest = digest_path.read_text(encoding="utf-8")
            except OSError:
                pass
        self._atomic_write(digest_path, existing_digest + digest_entry)

        self._atomic_write(changelog_path, "")

        # Set cooldown
        cooldown_key = f"relais:skill:consolidation_cooldown:{skill_name}"
        await redis_conn.set(
            cooldown_key, "1", ex=config.consolidation_cooldown_seconds
        )

        logger.info("Skill '%s' consolidated successfully.", skill_name)
        return True

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write content to a file atomically via a temporary file.

        Args:
            path: Target file path.
            content: Content to write.
        """
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
