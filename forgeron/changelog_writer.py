"""ChangelogWriter — Phase 1 of S3 changelog-based skill improvement.

Extracts concrete observations from agent conversation traces and appends
them to a per-skill ``CHANGELOG.md`` file.  Each observation entry is
timestamped and records the error count and trigger type.

Trigger types:
- ``error`` — tool errors detected during the turn.
- ``aborted`` — turn was aborted before completion (``tool_error_count == -1``
  sentinel published by Atelier on the DLQ path).
- ``usage`` — cumulative call threshold reached with no errors.

Two Redis TTL keys are used:
- ``relais:skill:annotation_cooldown:{skill_name}`` — prevents annotation spam.
- ``relais:skill:consolidation_cooldown:{skill_name}`` — checked (not set) to
  decide whether consolidation is allowed.

Atomic writes use a ``.tmp`` file + ``Path.replace()`` (POSIX-atomic).
"""

from __future__ import annotations

import logging
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage

from common.profile_loader import ProfileConfig, build_chat_model

if TYPE_CHECKING:
    from forgeron.config import ForgeonConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM system prompt
# ---------------------------------------------------------------------------

_OBSERVATION_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert at extracting concrete observations from AI agent conversations.

    Given a SKILL.md and a conversation trace, produce 1-3 dated observations.

    Rules:
    - Each observation must be concrete and actionable.
    - Do NOT restate existing SKILL.md content — only capture what is NEW.
    - If tool errors occurred, focus on root causes and prevention.
    - If no errors, note successful patterns or shortcuts not in the skill file.
    - If nothing new was learned, respond with exactly: No new observations.
    - Use imperative language: "Run X before Y", "Always pass --flag".

    Output format (no headings, no fences, no JSON):
    - <observation 1>
    - <observation 2>
""")


class ChangelogWriter:
    """Append per-turn observations to a skill's CHANGELOG.md.

    Args:
        profile: ProfileConfig for the annotation LLM (fast model recommended).
        skills_dir: Root directory containing per-skill subdirectories.
    """

    def __init__(self, profile: ProfileConfig, skills_dir: Path | None) -> None:
        self._profile = profile
        self._skills_dir = skills_dir

    async def observe(
        self,
        skill_name: str,
        tool_error_count: int,
        messages_raw: list[dict],
        config: "ForgeonConfig",
        redis_conn: Any,
        *,
        force: bool = False,
    ) -> bool:
        """Extract observations and append to CHANGELOG.md.

        Fires when ``tool_error_count >= annotation_min_tool_errors`` (error
        trigger) OR when ``force=True`` (usage-frequency trigger).  Both
        paths are rate-limited via a per-skill Redis cooldown key.

        Args:
            skill_name: Directory name of the skill under ``skills_dir``.
            tool_error_count: Number of tool errors in this turn (may be 0
                when ``force=True``).
            messages_raw: Serialised LangChain message list for this turn.
            config: Forgeron runtime config (thresholds, cooldown TTLs).
            redis_conn: Active async Redis connection.
            force: Skip the ``annotation_min_tool_errors`` guard.  Pass
                ``True`` when the usage-count threshold has been reached.

        Returns:
            ``True`` if an entry was appended to CHANGELOG.md, ``False``
            otherwise.
        """
        if not force and tool_error_count < config.annotation_min_tool_errors:
            return False

        cooldown_key = f"relais:skill:annotation_cooldown:{skill_name}"
        ttl = await redis_conn.ttl(cooldown_key)
        if ttl > 0:
            logger.debug(
                "Annotation cooldown active for skill '%s' (%ds remaining).",
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
            logger.warning("Could not read SKILL.md for '%s': %s", skill_name, exc)
            return False

        observations = await self._generate_observations(
            skill_name, skill_content, messages_raw, tool_error_count
        )
        if not observations or observations.strip().lower() == "no new observations.":
            logger.debug("No new observations for skill '%s'.", skill_name)
            return False

        changelog_path = skill_path.parent / "CHANGELOG.md"
        line_count = self._write_entry(changelog_path, observations, tool_error_count, force)

        await redis_conn.set(
            cooldown_key, "1", ex=config.annotation_cooldown_seconds
        )

        trigger = f"{tool_error_count} error(s)" if tool_error_count > 0 else "usage threshold"
        logger.info(
            "Changelog entry appended for skill '%s' (trigger: %s, changelog lines: %d).",
            skill_name,
            trigger,
            line_count,
        )
        return True

    async def should_consolidate(
        self,
        changelog_path: Path,
        config: "ForgeonConfig",
        redis_conn: Any,
        skill_name: str,
    ) -> bool:
        """Decide whether the changelog warrants consolidation.

        Consolidation is triggered when:
        1. ``CHANGELOG.md`` exists and has at least
           ``config.consolidation_line_threshold`` lines, AND
        2. The consolidation cooldown key has no active TTL.

        Args:
            changelog_path: Absolute path to the CHANGELOG.md file.
            config: Forgeron runtime config.
            redis_conn: Active async Redis connection.
            skill_name: Skill name (for the Redis cooldown key lookup).

        Returns:
            ``True`` if consolidation should run, ``False`` otherwise.
        """
        if not changelog_path.exists():
            return False

        try:
            line_count = len(changelog_path.read_text(encoding="utf-8").splitlines())
        except OSError:
            return False

        if line_count < config.consolidation_line_threshold:
            return False

        cooldown_key = f"relais:skill:consolidation_cooldown:{skill_name}"
        ttl = await redis_conn.ttl(cooldown_key)
        if ttl > 0:
            logger.debug(
                "Consolidation cooldown active for skill '%s' (%ds remaining).",
                skill_name,
                ttl,
            )
            return False

        return True

    def changelog_path(self, skill_name: str) -> Path | None:
        """Return the CHANGELOG.md path for a skill, or None if unresolvable.

        Args:
            skill_name: Directory name of the skill.

        Returns:
            Absolute Path to CHANGELOG.md, or None when ``skills_dir`` is
            unset or the skill directory does not exist.
        """
        if self._skills_dir is None:
            return None
        skill_dir = (self._skills_dir / skill_name).resolve()
        if not skill_dir.is_relative_to(self._skills_dir.resolve()):
            return None
        if not skill_dir.is_dir():
            return None
        return skill_dir / "CHANGELOG.md"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_skill_path(self, skill_name: str) -> Path | None:
        """Resolve the SKILL.md path for a skill directory name.

        Args:
            skill_name: Directory name of the skill.

        Returns:
            Path to SKILL.md if it exists, otherwise None.
        """
        if self._skills_dir is None:
            return None
        skill_dir = (self._skills_dir / skill_name).resolve()
        if not skill_dir.is_relative_to(self._skills_dir.resolve()):
            logger.warning("Path traversal blocked for skill '%s'.", skill_name)
            return None
        candidate = skill_dir / "SKILL.md"
        if not candidate.exists():
            logger.debug("SKILL.md not found for changelog observation: %s", candidate)
            return None
        return candidate

    async def _generate_observations(
        self,
        skill_name: str,
        skill_content: str,
        messages_raw: list[dict],
        tool_error_count: int,
    ) -> str:
        """Call the LLM to generate concise changelog observations.

        Args:
            skill_name: Skill name (for logging).
            skill_content: Current SKILL.md text.
            messages_raw: Conversation messages for this turn.
            tool_error_count: Number of tool errors (0 on usage-frequency trigger).

        Returns:
            Raw bullet-list string from the LLM, or empty string on failure.
        """
        conversation = self._format_conversation(messages_raw)
        if tool_error_count == -1:
            context_line = "This turn was ABORTED before completion (agent execution error)."
        elif tool_error_count > 0:
            context_line = f"Tool errors in this turn: {tool_error_count}."
        else:
            context_line = "No tool errors — observation triggered by usage frequency."
        user_message = (
            f"# Skill: {skill_name}\n\n"
            "## Current SKILL.md\n"
            f"```markdown\n{skill_content}\n```\n\n"
            f"## Context\n{context_line}\n\n"
            "## Conversation\n"
            f"{conversation}\n\n"
            "Extract new observations for the changelog."
        )

        try:
            llm = build_chat_model(self._profile)
            response = await llm.ainvoke(
                [
                    SystemMessage(content=_OBSERVATION_SYSTEM_PROMPT),
                    HumanMessage(content=user_message),
                ]
            )
            raw = str(response.content).strip()
            if not raw:
                logger.warning(
                    "LLM returned empty observations for skill '%s'.", skill_name
                )
            return raw
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Observation LLM call failed for skill '%s': %s", skill_name, exc
            )
            return ""

    def _write_entry(
        self,
        changelog_path: Path,
        observations: str,
        tool_error_count: int,
        force: bool,
    ) -> int:
        """Append an observation entry to CHANGELOG.md atomically.

        Reads existing content (if any), appends a new timestamped section,
        and writes via a ``.tmp`` file + ``Path.replace()`` (POSIX-atomic).

        Args:
            changelog_path: Target CHANGELOG.md path.
            observations: Raw bullet-list text from the LLM.
            tool_error_count: Number of tool errors for this entry.
            force: Whether the entry was triggered by usage frequency.

        Returns:
            Total number of lines in the updated CHANGELOG.md.
        """
        existing = ""
        if changelog_path.exists():
            try:
                existing = changelog_path.read_text(encoding="utf-8")
            except OSError:
                existing = ""

        if tool_error_count == -1:
            error_display = "aborted"
            trigger_label = "aborted"
        else:
            error_display = str(tool_error_count)
            trigger_label = "usage" if force and tool_error_count == 0 else "error"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        header = f"## {timestamp} (errors={error_display}, trigger={trigger_label})\n"
        entry = f"{header}{observations}\n"

        separator = "\n" if existing and not existing.endswith("\n\n") else ""
        updated = existing + separator + entry

        tmp_path = changelog_path.with_suffix(".tmp")
        tmp_path.write_text(updated, encoding="utf-8")
        tmp_path.replace(changelog_path)

        return len(updated.splitlines())

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
