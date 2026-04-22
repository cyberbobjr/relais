"""Prompt constants and builder functions for the Atelier agent executor.

Centralises all prompt-related logic extracted from ``atelier/agent_executor.py``
so that each module stays under the 800-line file size limit.

Exported symbols are re-imported in ``atelier/agent_executor.py`` for
backward-compatibility.
"""

from __future__ import annotations

from pathlib import Path

from common.contexts import CTX_AIGUILLEUR, AiguilleurCtx
from common.envelope import Envelope


LONG_TERM_MEMORY_PROMPT = """
Long-term memory:
- Any information about the user must be stored in the `memories` directory.
- This includes the user's preferences, needs, goals, projects, and any other user-related details.
- If the user asks you to remember anything, save it in `memories`.
- Always use paths like `/memories/...` to create, read, update, or organize persistent memories.
- Do not write long-term information outside `/memories/`.
- Before answering any question about the user or long-term memory, first check `/memories/` for relevant user and long-term information.
- CRITICAL: `/memories/` is a virtual filesystem. NEVER use the `execute` tool to run shell commands (mkdir, touch, ls, cat, etc.) on `/memories/` paths — they will fail because `/memories/` does not exist on disk. Always use the dedicated file tools (write_file, read_file, list_files, edit_file) for all operations under `/memories/`.
""".strip()

SELF_DIAGNOSIS_PROMPT = """
Self-diagnosis on tool errors (IMPORTANT):
If you encounter repeated tool errors (3+ in a row for the same tool, or 5+ total):
1. STOP retrying the same approach immediately.
2. Re-read the relevant SKILL.md troubleshooting section for the skill you are using.
3. Analyze ALL error messages you have received to identify the root cause.
4. Form a hypothesis about what is wrong (wrong syntax, wrong config key, wrong flag position, etc.).
5. Try ONE corrected approach based on your diagnosis.
Never blindly retry a failing command with minor variations — diagnose first.
""".strip()

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

DIAGNOSTIC_MARKER = "[DIAGNOSTIC — internal]"

DIAGNOSTIC_AWARENESS_PROMPT = f"""
Diagnostic awareness:
If the user asks what went wrong in a previous turn (e.g. "what error did you encounter?",
"why did you fail?", "what happened?"), look for a {DIAGNOSTIC_MARKER} message in the
conversation history. That message contains a technical summary of the failure — use it to
give the user a clear, honest explanation in plain language.
Do NOT repeat the diagnostic verbatim; summarise it for the user.
""".strip()


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


def _enrich_system_prompt(
    soul_prompt: str,
    *,
    delegation_prompt: str = "",
    project_context: str = "",
) -> str:
    """Append operational rules to the assembled system prompt.

    Appends long-term memory instructions, self-diagnosis instructions
    for tool error recovery, project environment anchors, and the delegation
    prompt (assembled by ``SubagentRegistry``) when non-empty.

    The self-diagnosis instructions tell the agent to stop and re-read
    the relevant SKILL.md troubleshooting section when encountering
    repeated tool errors, rather than blindly retrying.

    Args:
        soul_prompt: The base system prompt assembled by SoulAssembler.
        delegation_prompt: Pre-assembled delegation prompt from the
            subagent registry.  Empty string means no subagents.
        project_context: Pre-built project environment block (RELAIS_HOME,
            RELAIS_PROJECT_DIR).  Empty string skips injection.

    Returns:
        The enriched system prompt string.
    """
    parts = [soul_prompt.rstrip()]
    if LONG_TERM_MEMORY_PROMPT not in soul_prompt:
        parts.append(LONG_TERM_MEMORY_PROMPT)
    if SELF_DIAGNOSIS_PROMPT not in soul_prompt:
        parts.append(SELF_DIAGNOSIS_PROMPT)
    if DIAGNOSTIC_AWARENESS_PROMPT not in soul_prompt:
        parts.append(DIAGNOSTIC_AWARENESS_PROMPT)
    if project_context and project_context not in soul_prompt:
        parts.append(project_context)
    if delegation_prompt:
        parts.append(delegation_prompt)
    return "\n\n".join(parts)
