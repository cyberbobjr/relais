"""ErrorSynthesizer — produce a user-visible error message from a failed agent turn.

When an ``AgentExecutionError`` is caught by Atelier, the agent loop ended
abnormally (e.g. too many consecutive tool errors).  Instead of silently
dropping the request, we perform a lightweight LLM call that examines the
partial conversation history and generates an empathetic, actionable reply
for the user.
"""

import logging
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

from common.profile_loader import ProfileConfig

logger = logging.getLogger("atelier.error_synthesizer")

_SYSTEM_PROMPT = (
    "You are a helpful assistant. The agent encountered an unrecoverable error "
    "while processing the user's request. Based on the conversation history below, "
    "write a short, empathetic reply (2-4 sentences) that:\n"
    "1. Acknowledges what the user asked for.\n"
    "2. Honestly explains that you ran into a technical problem.\n"
    "3. Suggests a concrete next step when possible (e.g. check the configuration, "
    "   try again later, or contact support).\n"
    "Reply only with the message to show the user — no meta-commentary.\n"
    "IMPORTANT: do NOT reveal file paths, stack traces, internal tool names, or "
    "technical identifiers verbatim — summarise the affected functionality area "
    "in plain language (e.g. 'the scheduling tool', 'the memory lookup')."
)

_FALLBACK_MESSAGE = (
    "I'm sorry, I ran into an unexpected technical problem while processing your "
    "request and was unable to complete it. Please try again, or contact support "
    "if the issue persists."
)

# Substrings that indicate a tool message contains a logical error.
_TOOL_ERROR_MARKERS: tuple[str, ...] = (
    "[Command failed with exit code",
    "Error:",
    "error:",
    "Exception:",
    "Traceback (most recent call last)",
    '{"error"',
    "failed:",
    "Failed:",
)

_MAX_TOOL_ERROR_ENTRIES = 5
_MAX_TOOL_ERROR_PREVIEW = 300
_MAX_MESSAGES_SCAN = 20


def extract_tool_errors(messages_raw: list[dict]) -> list[dict]:
    """Extract tool messages that appear to contain logical errors.

    Scans the last ``_MAX_MESSAGES_SCAN`` messages for ``role='tool'`` entries
    whose content matches any of ``_TOOL_ERROR_MARKERS``.  Returns at most
    ``_MAX_TOOL_ERROR_ENTRIES`` results, each with ``tool_name`` and a
    truncated ``content_preview``.

    Args:
        messages_raw: Serialised LangChain message dicts (as produced by
            ``serialize_messages()``).

    Returns:
        List of ``{"tool_name": str, "content_preview": str}`` dicts for
        messages that appear to be errors.  Empty list if none found.
    """
    errors: list[dict] = []
    for msg in messages_raw[-_MAX_MESSAGES_SCAN:]:
        if msg.get("role") != "tool":
            continue
        content = str(msg.get("content", ""))
        tool_name = str(msg.get("name") or "unknown")
        if any(marker in content for marker in _TOOL_ERROR_MARKERS):
            errors.append({
                "tool_name": tool_name,
                "content_preview": content[:_MAX_TOOL_ERROR_PREVIEW],
            })
    return errors[:_MAX_TOOL_ERROR_ENTRIES]


class ErrorSynthesizer:
    """Produce a user-visible error reply from a failed agent turn.

    Uses the same LLM profile as the original request so that the error
    message stays consistent with the configured model/temperature.
    """

    async def synthesize(
        self,
        messages_raw: list[dict],
        error: str,
        profile: ProfileConfig,
    ) -> str:
        """Generate an error explanation for the user.

        Injects the exception string and a summary of failing tools into the
        system prompt so the LLM can produce a precise, user-friendly message
        rather than a generic fallback.

        Args:
            messages_raw: Partial conversation history from the failed turn
                (serialised LangChain message dicts).
            error: String representation of the exception.
            profile: LLM profile to use for the synthesis call.

        Returns:
            A non-empty string suitable for sending to the user.
        """
        try:
            tool_errors = extract_tool_errors(messages_raw)

            context_parts = [f"Final exception: {error[:500]}"]
            if tool_errors:
                tool_lines = ["Failing tools:"]
                for entry in tool_errors:
                    tool_lines.append(f"  - {entry['tool_name']}: {entry['content_preview']}")
                context_parts.append("\n".join(tool_lines))

            technical_context = "\n".join(context_parts)
            enriched_system = (
                _SYSTEM_PROMPT
                + "\n\nTechnical context (use this to understand what failed, "
                "but summarise for the user — do not quote verbatim):\n"
                + technical_context
            )

            llm = init_chat_model(profile.model, temperature=0)
            prompt: list[Any] = [SystemMessage(content=enriched_system)]

            for msg in messages_raw:
                role = msg.get("type") or msg.get("role", "")
                content = msg.get("content", "")
                if role in ("human", "user"):
                    prompt.append(HumanMessage(content=str(content)))

            response = await llm.ainvoke(prompt)
            text = getattr(response, "content", "") or ""
            if text.strip():
                return text.strip()
            return _FALLBACK_MESSAGE

        except Exception as exc:
            logger.warning("ErrorSynthesizer LLM call failed: %s", exc)
            return _FALLBACK_MESSAGE
