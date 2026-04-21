"""LangChain callback handler that captures subagent messages for Forgeron tracing."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import LLMResult


def _normalize_ns(metadata: dict[str, Any] | None) -> str | None:
    """Extract and normalize langgraph_namespace from metadata.

    Returns dot-joined namespace string, or None if metadata is absent/root.
    """
    if not metadata:
        return None
    ns = metadata.get("langgraph_namespace")
    if not ns:
        return None
    if isinstance(ns, (list, tuple)):
        if len(ns) == 0:
            return None
        return ".".join(str(part) for part in ns)
    return str(ns)


class SubagentMessageCapture(BaseCallbackHandler):
    """Captures LLM messages and tool stats for non-root (subagent) namespaces.

    Designed to be injected into the parent RunnableConfig so LangGraph
    propagates it automatically to all child invocations including subagents.
    """

    def __init__(self) -> None:
        """Initialize empty capture state."""
        super().__init__()
        self._messages_by_ns: dict[str, list[BaseMessage]] = {}
        self._tool_counts_by_ns: dict[str, int] = {}
        self._tool_errors_by_ns: dict[str, int] = {}
        self._run_to_ns: dict[UUID, str] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Record input messages and associate run_id with namespace.

        Args:
            serialized: Serialized model info (unused).
            messages: Batched lists of input messages.
            run_id: Unique ID for this LLM call.
            parent_run_id: Parent run ID (unused here).
            metadata: LangGraph metadata carrying langgraph_namespace.
            **kwargs: Additional keyword arguments.
        """
        ns = _normalize_ns(metadata)
        if ns is None:
            return
        self._run_to_ns[run_id] = ns
        ns_list = self._messages_by_ns.setdefault(ns, [])
        for batch in messages:
            ns_list.extend(batch)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Append AI response messages to the captured namespace.

        Args:
            response: LLM result containing generated messages.
            run_id: Unique ID matching on_chat_model_start.
            parent_run_id: Parent run ID (unused).
            **kwargs: Additional keyword arguments.
        """
        ns = self._run_to_ns.get(run_id)
        if ns is None:
            return
        ns_list = self._messages_by_ns.setdefault(ns, [])
        for generation_batch in response.generations:
            for generation in generation_batch:
                msg = getattr(generation, "message", None)
                if msg is None:
                    msg = AIMessage(content=str(generation.text))
                ns_list.append(msg)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Register the tool run's namespace and initialise counters.

        Args:
            serialized: Serialized tool info (unused).
            input_str: Tool input string (unused).
            run_id: Unique ID for this tool call.
            parent_run_id: Parent LLM run ID used as fallback for ns lookup.
            metadata: LangGraph metadata carrying langgraph_namespace.
            **kwargs: Additional keyword arguments.
        """
        ns = _normalize_ns(metadata)
        if ns is None and parent_run_id is not None:
            ns = self._run_to_ns.get(parent_run_id)
        if ns is None:
            return
        self._run_to_ns[run_id] = ns
        self._tool_counts_by_ns[ns] = self._tool_counts_by_ns.get(ns, 0) + 1
        self._tool_errors_by_ns.setdefault(ns, 0)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Detect tool errors from output status or error string prefix.

        Args:
            output: Tool output — ToolMessage or plain string.
            run_id: Unique ID matching on_tool_start.
            parent_run_id: Parent run ID (unused).
            **kwargs: Additional keyword arguments.
        """
        ns = self._run_to_ns.get(run_id)
        if ns is None:
            return
        is_error = (
            getattr(output, "status", None) == "error"
            or (isinstance(output, str) and output.startswith("Error:"))
        )
        if is_error:
            self._tool_errors_by_ns[ns] = self._tool_errors_by_ns.get(ns, 0) + 1

    def get_subagent_data(
        self, ns_id: str
    ) -> tuple[list[BaseMessage], int, int]:
        """Return captured data for a given namespace.

        Args:
            ns_id: The normalized namespace string (dot-joined).

        Returns:
            Tuple of (messages, tool_call_count, tool_error_count).
            All values are zero/empty if nothing was captured for this ns.
        """
        messages = list(self._messages_by_ns.get(ns_id, []))
        tool_count = self._tool_counts_by_ns.get(ns_id, 0)
        tool_errors = self._tool_errors_by_ns.get(ns_id, 0)
        return messages, tool_count, tool_errors
