"""SkillCreator — generates a new SKILL.md from recurring sessions.

Uses the 'precise' LLM profile to produce a high-quality SKILL.md that
describes the task, lists required tools, and provides step-by-step
instructions so future agent turns can execute without trial-and-error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from common.profile_loader import ProfileConfig
from forgeron.llm_factory import build_chat_model

logger = logging.getLogger(__name__)


class SkillContentLLMResponse(BaseModel):
    """Structured output schema for the skill creation LLM call."""

    skill_content: str = Field(
        description="Full SKILL.md content including YAML frontmatter and body"
    )
    description: str = Field(
        description=(
            "One-line description of what the skill does and when to use it "
            "(max 1024 chars, used for skill discovery)"
        )
    )


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

    _SYSTEM_PROMPT = """\
You are an expert at writing reusable AI agent skill documents (SKILL.md format).

## Mandatory SKILL.md format

### Frontmatter (YAML between triple dashes)

  name:          Required. 1-64 chars. Lowercase letters, digits, hyphens only.
                 No leading/trailing/consecutive hyphens. Must match directory name.
  description:   Required. 1-1024 chars. Describe WHAT the skill does AND when to
                 use it. Include specific keywords for agent discovery.
                 Good: "Extracts text from PDFs, fills forms, merges files.
                        Use when the user mentions PDFs, forms, or document extraction."
                 Poor: "Helps with PDFs."
  license:       Optional.
  compatibility: Optional (max 500 chars). Only if env-specific requirements exist.
  metadata:      Optional. Key-value map (author, version…).
  allowed-tools: Optional. Space-delimited list of pre-approved tools.

### Body sections (recommended)

1. Step-by-step instructions (numbered, imperative)
2. Examples of inputs and outputs
3. Common edge cases / mistakes to avoid

Keep SKILL.md under 500 lines. Write for agents, not humans — concise and imperative.
Move detailed reference material to a references/ subdirectory if needed.

## Your task

Given the user request examples provided, generate a complete SKILL.md for the
identified recurring task. Write complete, actionable instructions. Be specific.
Avoid vague guidance."""

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
            model = build_chat_model(self._profile)
            structured_model = model.with_structured_output(SkillContentLLMResponse)
            result = cast(SkillContentLLMResponse, await structured_model.ainvoke([
                SystemMessage(content=self._SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ]))
            skill_content = result.skill_content.strip()
        # LLM SDKs raise heterogeneous exceptions; returning None lets the caller
        # skip creation rather than crashing the correction pipeline.
        except Exception as exc:  # noqa: BLE001
            logger.error("SkillCreator LLM call failed for '%s': %s", intent_label, exc)
            return None

        if len(skill_content) < 100:
            logger.warning("SkillCreator: generated content too short for '%s'", intent_label)
            return None

        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(skill_content, encoding="utf-8")
        logger.info("SkillCreator: created '%s' at %s", skill_name, skill_path)

        return SkillCreationResult(
            skill_name=skill_name,
            skill_path=skill_path,
            skill_content=skill_content,
            description=result.description,
        )
