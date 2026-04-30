"""Custom exceptions and error-tracking utilities for the Atelier brick.

Provides ``AgentExecutionError``, ``ExhaustedRetriesError``, ``ToolErrorGuard``,
and ``DiagnosticTrace`` — the error-related types previously defined inline in
``atelier/agent_executor.py``.  They are extracted here so that
``agent_executor.py`` stays below the 800-line file size limit while all
existing import sites (``from atelier.agent_executor import ...``) continue
to work via re-exports in that module.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass


class AgentExecutionError(Exception):
    """Raised for permanent/unknown agent execution failures.

    Transient errors (RateLimitError, InternalServerError, APIConnectionError)
    are retried by ``AgentExecutor.execute()`` before being surfaced as
    ``ExhaustedRetriesError``.  Non-transient errors are wrapped immediately.

    Args:
        message: Human-readable error description.
        response_body: Optional raw response body from the LLM provider.
        tool_call_count: Total tool invocations during the failed turn.
        tool_error_count: Number of tool errors during the failed turn.
        messages_raw: Serialized LangChain messages captured before abort.
        subagent_traces: Per-subagent execution traces collected before abort.
            Each element is a ``SubagentTrace`` instance.  Empty tuple when no
            subagents were invoked or traces were not yet captured.
    """

    def __init__(
        self,
        message: str,
        response_body: str | None = None,
        *,
        tool_call_count: int = 0,
        tool_error_count: int = 0,
        messages_raw: list[dict] | None = None,
        subagent_traces: tuple = (),
    ) -> None:
        super().__init__(message)
        self.response_body = response_body
        self.tool_call_count = tool_call_count
        self.tool_error_count = tool_error_count
        self.messages_raw: list[dict] = messages_raw if messages_raw is not None else []
        self.subagent_traces: tuple = subagent_traces


class ExhaustedRetriesError(AgentExecutionError):
    """Raised when all retry attempts for a transient error are exhausted.

    Subclasses ``AgentExecutionError`` so ``_handle_envelope`` routes it to
    the DLQ and ACKs the message (avoids poisoning the PEL indefinitely).
    """


@dataclass
class DiagnosticTrace:
    """Structured diagnostic summary produced by ``format_diagnostic_trace()``.

    Captures the key counters and identifiers from a failed agent turn so
    callers can either format a human-readable string (``str(trace)``) or
    access individual fields for structured logging / error synthesis.

    Attributes:
        messages_count: Total number of serialised messages in the turn.
        tool_count: Total number of tool invocations during the turn.
        tool_errors: Number of tool invocations that returned errors.
        last_tool: Name of the last tool that was called, or ``None``.
        last_error: Short preview of the last tool error content, or ``None``.
        tool_error_details: Extracted tool-error entries from the turn, each a
            dict with ``tool_name`` and ``content_preview`` keys.  Stored here
            so callers do not need to re-iterate ``messages_raw``.
    """

    messages_count: int
    tool_count: int
    tool_errors: int
    last_tool: str | None
    last_error: str | None
    tool_error_details: tuple[dict, ...] = dataclasses.field(default_factory=tuple)


class ToolErrorGuard:
    """Tracks consecutive and total tool errors to abort runaway loops.

    Raises ``AgentExecutionError`` when either the consecutive-error limit for
    a single tool or the total-error limit across all tools is exceeded.

    The default ``max_total=8`` (up from 5) gives the agent three additional
    attempts after hitting the diagnostic threshold.  Combined with the
    self-diagnosis instructions in the system prompt, this allows the agent
    to read SKILL.md troubleshooting sections and correct its approach
    before being aborted.

    Args:
        max_consecutive: Maximum number of consecutive errors allowed for the
            same tool before aborting.
        max_total: Maximum number of total tool errors allowed across all tools.
    """

    def __init__(self, max_consecutive: int, max_total: int) -> None:
        self._max_consecutive = max_consecutive
        self._max_total = max_total
        self._total_calls: int = 0
        self._total_errors: int = 0
        self._consecutive_name: str = ""
        self._consecutive_count: int = 0

    @property
    def total_calls(self) -> int:
        """Total number of tool invocations recorded."""
        return self._total_calls

    @property
    def total_errors(self) -> int:
        """Total number of tool error invocations recorded."""
        return self._total_errors

    def _check_total_limit(self, tool_name: str) -> None:
        """Raise ``AgentExecutionError`` if the total error limit has been reached.

        Compares the current ``_total_errors`` counter against ``_max_total``.
        Must be called *after* ``_total_errors`` has been incremented for the
        current invocation.

        Args:
            tool_name: The name of the tool that triggered the error
                (included in the error message for diagnostics).

        Raises:
            AgentExecutionError: When ``_total_errors >= _max_total``.
        """
        if self._total_errors >= self._max_total:
            raise AgentExecutionError(
                f"Aborting: {self._total_errors} total tool errors exceeded limit. "
                f"Last tool: '{tool_name}'",
                tool_call_count=self._total_calls,
                tool_error_count=self._total_errors,
            )

    def _check_consecutive_limit(self, tool_name: str) -> None:
        """Raise ``AgentExecutionError`` if the consecutive error limit has been reached.

        Unnamed tools (``tool_name == "?"``) are silently skipped to avoid
        false positives when different tools all lack a name attribute.

        Updates the consecutive tracking state (``_consecutive_name`` and
        ``_consecutive_count``) as a side effect before checking the limit.
        Must be called *after* ``_check_total_limit`` so that total-limit
        errors take precedence when both limits are hit simultaneously.

        Args:
            tool_name: The name of the tool that triggered the error.

        Raises:
            AgentExecutionError: When the same named tool has errored
                ``_max_consecutive`` times in a row.
        """
        if tool_name == "?":
            return
        if tool_name == self._consecutive_name:
            self._consecutive_count += 1
        else:
            self._consecutive_name = tool_name
            self._consecutive_count = 1
        if self._consecutive_count >= self._max_consecutive:
            raise AgentExecutionError(
                f"Tool '{tool_name}' errored {self._consecutive_count} "
                f"consecutive times — aborting to prevent infinite loop.",
                tool_call_count=self._total_calls,
                tool_error_count=self._total_errors,
            )

    def record(self, tool_name: str, is_error: bool) -> None:
        """Record the result of a tool invocation and raise if limits exceeded.

        Orchestrates the two limit checks in a defined order:
        1. ``_check_total_limit`` — fires first when the global error budget is
           exhausted, regardless of which tool caused the errors.
        2. ``_check_consecutive_limit`` — fires when the same named tool keeps
           failing in a row (skipped for unnamed ``"?"`` tools).

        Unnamed tools (``tool_name == "?"``) are excluded from the consecutive
        error check to avoid false positives when different tools all lack a
        name attribute.

        Args:
            tool_name: The name of the tool that was called.
            is_error: True if the tool returned a logical error.

        Raises:
            AgentExecutionError: If the consecutive or total error limit is hit.
        """
        self._total_calls += 1
        if not is_error:
            self._consecutive_name = ""
            self._consecutive_count = 0
            return

        self._total_errors += 1
        self._check_total_limit(tool_name)
        self._check_consecutive_limit(tool_name)
