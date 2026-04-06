"""SkillCreator — génère un nouveau SKILL.md depuis des sessions récurrentes.

Uses the 'precise' LLM profile to produce a high-quality SKILL.md that
describes the task, lists required tools, and provides step-by-step
instructions so future agent turns can execute without trial-and-error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from common.profile_loader import ProfileConfig

logger = logging.getLogger(__name__)


@dataclass
class SkillCreationResult:
    """Result of a successful skill creation.

    Attributes:
        skill_name: The directory name of the created skill (e.g. "send-email").
        skill_path: Absolute path to the written SKILL.md file.
        skill_content: Full generated SKILL.md content.
        description: One-line description extracted from the ## Description section.
    """

    skill_name: str
    skill_path: Path
    skill_content: str
    description: str


class SkillCreator:
    """Generate a new SKILL.md from representative sessions.

    Creates ``skills_dir/{skill_name}/SKILL.md`` atomically. Idempotent: if
    the file already exists, returns None without overwriting.

    Args:
        profile: The LLM ProfileConfig to use (typically "precise").
        skills_dir: Base directory where skill subdirectories are created.
    """

    _SYSTEM_PROMPT = """You are an expert at writing reusable AI agent skill documents.
A SKILL.md file describes a recurring task type and tells an AI agent exactly how to perform it efficiently.

Structure your SKILL.md as:
# {Skill Name}

## Description
One sentence describing what this skill does.

## When to use
Bullet list of triggers / user request patterns.

## Required tools
List of tools needed (e.g. bash, read_file, send_email).

## Step-by-step instructions
Numbered steps the agent should follow to complete the task without errors.

## Common mistakes to avoid
Bullet list of pitfalls observed in past executions.

## Example
A brief example of a successful execution.

Write complete, actionable instructions. Be specific. Avoid vague guidance."""

    def __init__(self, profile: ProfileConfig, skills_dir: Path) -> None:
        self._profile = profile
        self._skills_dir = skills_dir

    async def create(
        self,
        intent_label: str,
        session_examples: list[dict],
    ) -> SkillCreationResult | None:
        """Generate and write a SKILL.md for the given intent label.

        Args:
            intent_label: Normalized intent label (e.g. "send_email").
            session_examples: List of dicts with key ``user_content_preview``.

        Returns:
            SkillCreationResult on success, None if the skill already exists,
            the LLM call fails, or the generated content is too short.
        """
        skill_name = intent_label.replace("_", "-")

        # Check idempotency before calling the LLM
        skill_dir = self._skills_dir / skill_name
        skill_path = skill_dir / "SKILL.md"
        if skill_path.exists():
            logger.warning("SkillCreator: skill '%s' already exists, skipping.", skill_name)
            return None

        examples_text = "\n\n".join(
            f"Session {i + 1}:\n{ex.get('user_content_preview', '')}"
            for i, ex in enumerate(session_examples)
        )
        user_prompt = (
            f"Task type: {intent_label}\n\n"
            f"Here are {len(session_examples)} real examples of user requests for this task:\n\n"
            f"{examples_text}\n\n"
            f"Write a complete SKILL.md for skill named '{skill_name}'."
        )

        try:
            from common.profile_loader import build_chat_model  # noqa: PLC0415
            model = build_chat_model(self._profile)
            from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415
            response = await model.ainvoke([
                SystemMessage(content=self._SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ])
            skill_content = response.content.strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("SkillCreator LLM call failed for '%s': %s", intent_label, exc)
            return None

        if len(skill_content) < 100:
            logger.warning("SkillCreator: generated content too short for '%s'", intent_label)
            return None

        description = (
            self._extract_description(skill_content) or f"Auto-generated skill for: {intent_label}"
        )

        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(skill_content, encoding="utf-8")
        logger.info("SkillCreator: created '%s' at %s", skill_name, skill_path)

        return SkillCreationResult(
            skill_name=skill_name,
            skill_path=skill_path,
            skill_content=skill_content,
            description=description,
        )

    @staticmethod
    def _extract_description(content: str) -> str | None:
        """Extract the first non-empty line after '## Description'.

        Args:
            content: Full SKILL.md content string.

        Returns:
            The description line, or None if the section is missing or empty.
        """
        in_section = False
        for line in content.splitlines():
            if line.strip().lower().startswith("## description"):
                in_section = True
                continue
            if in_section and line.strip():
                if line.startswith("#"):
                    break
                return line.strip()
        return None
