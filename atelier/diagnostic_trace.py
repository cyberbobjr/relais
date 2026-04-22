"""Diagnostic trace formatting for the Atelier agent executor.

Provides ``_DIAGNOSTIC_MAX_CHARS``, ``format_diagnostic_trace``, and
``_render_diagnostic_trace``, extracted from ``atelier/agent_executor.py``
to keep that module under the 800-line limit.

Re-exported in ``atelier/agent_executor.py`` for backward compatibility.
"""

from __future__ import annotations

from atelier.error_synthesizer import extract_tool_errors
from atelier.errors import DiagnosticTrace
from atelier.prompts import DIAGNOSTIC_MARKER

_DIAGNOSTIC_MAX_CHARS = 2000


def format_diagnostic_trace(
    error: str,
    messages_raw: list[dict],
    *,
    tool_call_count: int = 0,
    tool_error_count: int = 0,
    max_chars: int = _DIAGNOSTIC_MAX_CHARS,
) -> DiagnosticTrace:
    """Build a structured diagnostic summary for injection into the checkpointer.

    Combines the exception string, tool counters, and failing tool message
    previews into a ``DiagnosticTrace`` dataclass.  Callers that need a
    plain-text representation for injection into conversation history can
    call ``_render_diagnostic_trace(trace, error, max_chars)`` separately.

    Args:
        error: String representation of the AgentExecutionError.
        messages_raw: Serialised LangChain message dicts from the failed turn.
        tool_call_count: Total tool calls made during the turn.
        tool_error_count: Number of tool calls that returned errors.
        max_chars: Kept for backward-compatible signature; unused here but
            consumed by ``_render_diagnostic_trace()``.

    Returns:
        A ``DiagnosticTrace`` instance with structured failure metadata.
    """
    tool_errors = extract_tool_errors(messages_raw)
    last_tool: str | None = tool_errors[-1]["tool_name"] if tool_errors else None
    last_error: str | None = tool_errors[-1]["content_preview"] if tool_errors else None
    return DiagnosticTrace(
        messages_count=len(messages_raw),
        tool_count=tool_call_count,
        tool_errors=tool_error_count,
        last_tool=last_tool,
        last_error=last_error,
        tool_error_details=tuple(tool_errors),
    )


def _render_diagnostic_trace(
    trace: DiagnosticTrace,
    error: str,
    *,
    max_chars: int = _DIAGNOSTIC_MAX_CHARS,
) -> str:
    """Render a ``DiagnosticTrace`` to the plain-text format injected into conversation history.

    Args:
        trace: The structured trace produced by ``format_diagnostic_trace()``.
        error: String representation of the originating ``AgentExecutionError``.
        max_chars: Maximum character length of the returned string.

    Returns:
        A plain-text diagnostic summary prefixed with ``DIAGNOSTIC_MARKER``.
    """
    tool_errors = trace.tool_error_details
    lines: list[str] = [
        DIAGNOSTIC_MARKER,
        f"Exception: {error[:500]}",
        f"Tool calls: {trace.tool_count} total, {trace.tool_errors} errors",
    ]
    if tool_errors:
        lines.append("Failing tools:")
        for entry in tool_errors:
            lines.append(f"  - {entry['tool_name']}: {entry['content_preview']}")
    return "\n".join(lines)[:max_chars]
