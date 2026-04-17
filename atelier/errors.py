"""Custom exceptions and error-tracking utilities for the Atelier brick.

Provides ``AgentExecutionError``, ``ExhaustedRetriesError``, and
``ToolErrorGuard`` — the three error-related types previously defined
inline in ``atelier/agent_executor.py``.  They are extracted here so that
``agent_executor.py`` stays below the 800-line file size limit while all
existing import sites (``from atelier.agent_executor import ...``) continue
to work via re-exports in that module.
"""

from __future__ import annotations


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
    """

    def __init__(
        self,
        message: str,
        response_body: str | None = None,
        *,
        tool_call_count: int = 0,
        tool_error_count: int = 0,
        messages_raw: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.response_body = response_body
        self.tool_call_count = tool_call_count
        self.tool_error_count = tool_error_count
        self.messages_raw: list[dict] = messages_raw if messages_raw is not None else []


class ExhaustedRetriesError(AgentExecutionError):
    """Raised when all retry attempts for a transient error are exhausted.

    Subclasses ``AgentExecutionError`` so ``_handle_envelope`` routes it to
    the DLQ and ACKs the message (avoids poisoning the PEL indefinitely).
    """


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

    def record(self, tool_name: str, is_error: bool) -> None:
        """Record the result of a tool invocation and raise if limits exceeded.

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
        if self._total_errors >= self._max_total:
            raise AgentExecutionError(
                f"Aborting: {self._total_errors} total tool errors exceeded limit. "
                f"Last tool: '{tool_name}'",
                tool_call_count=self._total_calls,
                tool_error_count=self._total_errors,
            )

        named_tool = tool_name if tool_name != "?" else None
        if named_tool is not None:
            if named_tool == self._consecutive_name:
                self._consecutive_count += 1
            else:
                self._consecutive_name = named_tool
                self._consecutive_count = 1
            if self._consecutive_count >= self._max_consecutive:
                raise AgentExecutionError(
                    f"Tool '{tool_name}' errored {self._consecutive_count} "
                    f"consecutive times — aborting to prevent infinite loop.",
                    tool_call_count=self._total_calls,
                    tool_error_count=self._total_errors,
                )
