"""SkillAnalyzer — LLM-based analysis of skill execution traces.

Given the current SKILL.md content and a list of recent error-laden traces,
calls an LLM to produce an improved version of the skill file plus a rationale
and a unified diff.

The analyzer is intentionally stateless: every call is a fresh LLM conversation.
Retries and cooldowns are handled by the caller (Forgeron).
"""

from __future__ import annotations

import difflib
import json
import logging
import textwrap
from dataclasses import dataclass
from typing import cast

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from common.profile_loader import ProfileConfig, build_chat_model
from forgeron.models import SkillTrace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert AI assistant specialising in improving SKILL.md files for
    autonomous agents (the RELAIS system).

    A SKILL.md file is a structured instruction document that tells an AI agent
    how to use a specific skill (set of CLI tools / APIs). When the skill has a
    high tool error rate it usually means:
      - The documented commands are slightly wrong (wrong flags, wrong order).
      - Required preconditions or environment details are missing.
      - The agent wastes turns on trial-and-error that could be avoided with
        better up-front instructions.

    Your task:
    1. Read the current SKILL.md content.
    2. Read the execution traces (agent conversations) that show where errors
       occurred.
    3. Produce an IMPROVED version of SKILL.md that would have prevented those
       errors.

    Rules:
    - Keep the same overall structure and length; do NOT rewrite sections that
      were not related to the observed errors.
    - Add ONLY information that directly addresses the observed failures.
    - Do NOT add speculative warnings about things that never went wrong.
    - If the root cause is a missing prerequisite step, add it clearly.
    - If a command had wrong flags or syntax, correct them with evidence from
      the traces.
    - Keep the tone concise and imperative (this is agent instruction, not
      documentation for humans).

    ## SKILL.md format specification (MANDATORY — respect these when rewriting)

    Frontmatter (YAML between triple dashes):
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

    Body (Markdown after frontmatter):
      Recommended sections:
        - Step-by-step instructions (numbered, imperative)
        - Examples of inputs and outputs
        - Common edge cases / mistakes to avoid
      Keep SKILL.md under 500 lines.
      Write for agents, not humans — concise and imperative.
      Move detailed reference material to a references/ subdirectory if needed.
""")


# ---------------------------------------------------------------------------
# LLM output schema
# ---------------------------------------------------------------------------


class SkillPatchLLMResponse(BaseModel):
    """Structured output schema for the skill patch LLM call."""

    patched_content: str = Field(
        description="Full improved SKILL.md content (including frontmatter)"
    )
    rationale: str = Field(
        description="1-3 sentence explanation of what was changed and why"
    )


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillPatchProposal:
    """LLM-produced improvement for a skill file.

    Attributes:
        patched_content: The full improved SKILL.md text.
        rationale: Human-readable explanation of the changes.
        diff: Unified diff between original and patched content.
    """

    patched_content: str
    rationale: str
    diff: str


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class SkillAnalyzer:
    """Calls an LLM to produce a patched SKILL.md from execution traces.

    Args:
        profile: A ``ProfileConfig`` instance describing the LLM to use for
            analysis (model, temperature, api_key_env, base_url, …).
    """

    def __init__(self, profile: ProfileConfig) -> None:
        self._profile = profile

    async def analyze(
        self,
        skill_name: str,
        skill_content: str,
        traces: list[SkillTrace],
    ) -> SkillPatchProposal:
        """Analyse recent traces and produce an improved SKILL.md.

        Args:
            skill_name: Directory name of the skill (used for logging only).
            skill_content: Current SKILL.md text.
            traces: Recent ``SkillTrace`` records for this skill — all will be
                included in the LLM context.

        Returns:
            A ``SkillPatchProposal`` with patched content, rationale, and diff.

        Raises:
            ValueError: If the LLM response cannot be parsed as valid JSON with
                the expected keys.
        """
        user_message = self._build_user_message(skill_name, skill_content, traces)
        llm = build_chat_model(self._profile)

        logger.debug(
            "Sending %d traces to LLM for skill '%s' analysis (model=%s)",
            len(traces),
            skill_name,
            self._profile.model,
        )

        structured_llm = llm.with_structured_output(SkillPatchLLMResponse)
        result = cast(SkillPatchLLMResponse, await structured_llm.ainvoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=user_message),
            ]
        ))

        if not result.patched_content:
            raise ValueError(
                f"LLM structured output for skill '{skill_name}' returned empty patched_content."
            )
        if not result.rationale:
            raise ValueError(
                f"LLM structured output for skill '{skill_name}' returned empty rationale."
            )

        diff = "\n".join(
            difflib.unified_diff(
                skill_content.splitlines(),
                result.patched_content.splitlines(),
                fromfile=f"{skill_name}/SKILL.md (original)",
                tofile=f"{skill_name}/SKILL.md (patched)",
                lineterm="",
            )
        )

        logger.info(
            "LLM analysis complete for skill '%s': %d diff lines, rationale=%r",
            skill_name,
            len(diff.splitlines()),
            result.rationale[:120],
        )

        return SkillPatchProposal(
            patched_content=result.patched_content,
            rationale=result.rationale,
            diff=diff,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_user_message(
        self,
        skill_name: str,
        skill_content: str,
        traces: list[SkillTrace],
    ) -> str:
        """Build the human message containing skill content and traces.

        Args:
            skill_name: Directory name of the skill.
            skill_content: Current SKILL.md text.
            traces: Trace records to include in context.

        Returns:
            Formatted string ready to send to the LLM.
        """
        parts: list[str] = [
            f"# Skill: {skill_name}",
            "",
            "## Current SKILL.md",
            "```markdown",
            skill_content,
            "```",
            "",
            f"## Execution Traces ({len(traces)} most recent turns with errors)",
        ]

        for i, trace in enumerate(traces, start=1):
            parts.append(
                f"\n### Trace {i} "
                f"(tool_calls={trace.tool_call_count}, "
                f"tool_errors={trace.tool_error_count})"
            )
            try:
                messages: list[dict] = json.loads(trace.messages_raw)
                for msg in messages:
                    role = msg.get("type", msg.get("role", "unknown"))
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        # Handle LangChain structured content blocks
                        content = " ".join(
                            c.get("text", "") if isinstance(c, dict) else str(c)
                            for c in content
                        )
                    parts.append(f"**{role}**: {str(content)[:2000]}")
            except (json.JSONDecodeError, TypeError):
                parts.append("*(trace content unavailable)*")

        parts.append(
            "\n\nPlease produce the improved SKILL.md as specified in the system prompt."
        )
        return "\n".join(parts)

