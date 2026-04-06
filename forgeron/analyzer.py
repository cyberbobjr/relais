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

from langchain_core.messages import HumanMessage, SystemMessage

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

    Respond in this EXACT JSON format (no markdown fences, raw JSON only):
    {
      "patched_content": "<full improved SKILL.md content>",
      "rationale": "<1-3 sentence explanation of what was changed and why>"
    }
""")


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

        response = await llm.ainvoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=user_message),
            ]
        )

        raw_text: str = response.content  # type: ignore[assignment]
        return self._parse_response(skill_name, skill_content, raw_text)

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

    def _parse_response(
        self,
        skill_name: str,
        original_content: str,
        raw_text: str,
    ) -> SkillPatchProposal:
        """Parse the LLM JSON response into a SkillPatchProposal.

        Args:
            skill_name: Skill name (for error messages).
            original_content: Original SKILL.md for diff generation.
            raw_text: Raw LLM response text (expected to be JSON).

        Returns:
            Parsed ``SkillPatchProposal``.

        Raises:
            ValueError: If the response is not valid JSON or is missing
                ``patched_content`` or ``rationale`` keys.
        """
        # Strip markdown fences if the model wrapped its output anyway
        text = raw_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove opening and closing fence lines
            text = "\n".join(
                line for line in lines
                if not line.startswith("```")
            ).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"LLM response for skill '{skill_name}' is not valid JSON: {exc}\n"
                f"Raw response (first 500 chars): {raw_text[:500]}"
            ) from exc

        patched_content = data.get("patched_content", "")
        rationale = data.get("rationale", "")

        if not patched_content:
            raise ValueError(
                f"LLM response for skill '{skill_name}' missing 'patched_content'. "
                f"Keys found: {list(data.keys())}"
            )
        if not rationale:
            raise ValueError(
                f"LLM response for skill '{skill_name}' missing 'rationale'. "
                f"Keys found: {list(data.keys())}"
            )

        diff = "\n".join(
            difflib.unified_diff(
                original_content.splitlines(),
                patched_content.splitlines(),
                fromfile=f"{skill_name}/SKILL.md (original)",
                tofile=f"{skill_name}/SKILL.md (patched)",
                lineterm="",
            )
        )

        logger.info(
            "LLM analysis complete for skill '%s': %d diff lines, rationale=%r",
            skill_name,
            len(diff.splitlines()),
            rationale[:120],
        )

        return SkillPatchProposal(
            patched_content=patched_content,
            rationale=rationale,
            diff=diff,
        )
