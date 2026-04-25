"""Prompt builder functions for the Atelier agent executor.

Centralises all prompt-related logic extracted from ``atelier/agent_executor.py``
so that each module stays under the 800-line file size limit.
"""

from __future__ import annotations

from pathlib import Path

from common.config_loader import get_relais_project_dir
from common.contexts import CTX_AIGUILLEUR, AiguilleurCtx
from common.envelope import Envelope

# Runtime constant — used to tag diagnostic messages injected into conversation history.
# Do NOT move this to SYSTEM_PROMPT.md; it is referenced by diagnostic_trace.py at runtime.
DIAGNOSTIC_MARKER = "[DIAGNOSTIC — internal]"

# Operational rules injected into subagent system prompts (not the main agent — the main
# agent receives equivalent content via SYSTEM_PROMPT.md through memory=).
SUBAGENT_OPERATIONAL_RULES = """
Operational rules (apply to every tool call):

**On timeout (exit code 124 / "Command timed out"):**
- DO NOT retry the same command.
- Diagnose the root cause first:
  1. Read the error message and any preceding tool results carefully.
  2. Re-read the SKILL.md troubleshooting section for the skill you are using.
  3. Form a hypothesis (wrong argument, wrong address, wrong flag, connectivity issue, …).
  4. Try ONE corrected command based on your diagnosis.
  5. If the diagnosis requires a preliminary command (e.g. fetching the correct value to use),
     run that first, then rebuild the failing command with the correct value.

**On repeated tool errors (3+ in a row for the same tool, or 5+ total):**
- STOP retrying immediately.
- Apply the same diagnosis steps above before attempting anything else.

Never blindly retry a failing command — diagnose first, then act.
""".strip()

_SYSTEM_PROMPT_PATH = get_relais_project_dir() / "atelier" / "SYSTEM_PROMPT.md"
_SYSTEM_PROMPT_CACHE: str | None = None


def build_project_context_prompt(relais_home: str, project_dir: str) -> str:
    """Build the project environment context block injected into every agent.

    Provides concrete filesystem anchors so agents never need to search from
    the root (``find /``, ``ls /``).  Always use these paths as starting
    points when looking for source files, skills, or configuration.

    Args:
        relais_home: Absolute path to the RELAIS data/config directory.
        project_dir: Absolute path to the RELAIS Python source root.

    Returns:
        A formatted string ready to append to a system prompt.
    """
    tui_dir = str(Path(project_dir) / "tools" / "tui-ts")
    return (
        "Project environment — ALWAYS use these anchors, NEVER search from /:\n"
        f"- RELAIS_HOME (config / skills / data): {relais_home}\n"
        f"  ├── config/          — user YAML overrides (aiguilleur, portail, atelier, …)\n"
        f"  ├── config/atelier/subagents/  — user-defined subagents (one dir per subagent)\n"
        f"  ├── skills/          — user skills (one dir per skill, each contains SKILL.md)\n"
        f"  ├── bundles/         — installed bundles (one dir per bundle)\n"
        f"  └── vendor/          — portable package installs\n"
        f"       (pip: pip install --target={relais_home}/vendor <pkg>)\n"
        f"       (npm: npm install --prefix={relais_home}/vendor <pkg>)\n"
        f"- RELAIS_PROJECT_DIR (all source code — Python AND TypeScript): {project_dir}\n"
        f"- TUI TypeScript project (npm / playwright / Node.js): {tui_dir}\n"
        "CRITICAL: Never run `find /`, `ls /`, or any command that explores from the "
        "filesystem root. All RELAIS files live under RELAIS_PROJECT_DIR or RELAIS_HOME. "
        "Start every file search from one of the anchors above.\n"
        "PORTABLE INSTALLS: When installing packages or tools for reuse, always use "
        f"{relais_home}/vendor as the install target — never install system-wide or in "
        "a temporary directory."
    ).strip()


def _build_execution_context(envelope: Envelope) -> str:
    """Render envelope routing metadata as a plain-text context block.

    The block is prepended to the first user message on every agent turn so
    that skills which need routing information (e.g. ``channel-setup`` for
    WhatsApp pairing) can read the current ``sender_id``, ``channel``,
    ``session_id``, ``correlation_id`` and ``reply_to`` directly from the
    conversation state.

    The block is wrapped in explicit ``<relais_execution_context>`` tags so
    that the LLM recognises it as technical metadata and does NOT echo it
    back to the user.  Skills reference the tag by name.

    Args:
        envelope: The inbound envelope being processed.

    Returns:
        A formatted string ready to prepend to the user message, or an
        empty string if no metadata is available.
    """
    aig_ctx: AiguilleurCtx = envelope.context.get(CTX_AIGUILLEUR, {})  # type: ignore
    reply_to = aig_ctx.get("reply_to", "")
    lines = [
        "<relais_execution_context>",
        "This block is RELAIS pipeline metadata, not user input. Do NOT echo it.",
        "Use it only when a skill explicitly requires routing information.",
        f"sender_id: {envelope.sender_id}",
        f"channel: {envelope.channel}",
        f"session_id: {envelope.session_id}",
        f"correlation_id: {envelope.correlation_id}",
        f"reply_to: {reply_to}",
        "</relais_execution_context>",
    ]
    return "\n".join(lines)


def _build_core_system_prompt(
    *,
    delegation_prompt: str = "",
    project_context: str = "",
) -> str:
    """Build the fixed system prompt from SYSTEM_PROMPT.md plus dynamic runtime sections.

    Reads ``atelier/SYSTEM_PROMPT.md`` as the non-user-editable RELAIS core identity
    and appends the dynamic per-request sections (project environment anchors and
    delegation instructions) when non-empty.

    User-editable personality files (SOUL.md, role/user/channel overlays) are passed
    separately as ``memory=`` to ``create_deep_agent()`` — they are NOT included here.

    Args:
        delegation_prompt: Pre-assembled delegation prompt from the subagent registry.
            Empty string means no subagents available for delegation.
        project_context: Pre-built project environment block (RELAIS_HOME, RELAIS_PROJECT_DIR).
            Empty string skips injection.

    Returns:
        The core system prompt string ready to pass as ``system_prompt=`` to
        ``create_deep_agent()``.

    Raises:
        FileNotFoundError: If ``atelier/SYSTEM_PROMPT.md`` cannot be read.
    """
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        _SYSTEM_PROMPT_CACHE = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    parts = [_SYSTEM_PROMPT_CACHE]
    if project_context:
        parts.append(project_context)
    if delegation_prompt:
        parts.append(delegation_prompt)
    return "\n\n".join(parts)
