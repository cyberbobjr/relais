"""Stream-loop state and pure helper functions for AgentExecutor._stream().

Extracted from ``atelier/agent_executor.py`` to keep that module under the
800-line limit and to make the helpers independently testable.

Re-exported in ``atelier/agent_executor.py`` for backward compatibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from atelier.streaming import REPLY_PLACEHOLDER

logger = logging.getLogger(__name__)


@dataclass
class StreamLoopState:
    """Mutable accumulator for a single ``_stream()`` invocation.

    Attributes:
        full_reply: All AI text tokens concatenated during the loop.
        last_tool_result: The content of the most recent tool result message.
        pending_tool_name: Name of the tool call currently being assembled.
        current_section: AI text tokens accumulated since the last tool call.
            Used by ``final_only`` mode to return only the post-last-tool text.
    """

    full_reply: str = ""
    last_tool_result: str = ""
    pending_tool_name: str = ""
    current_section: str = ""


def compute_reply_text(
    *,
    full_reply: str,
    current_section: str,
    last_tool_result: str,
    final_only: bool,
) -> str:
    """Compute the final reply string after the streaming loop completes.

    Applies two fallback strategies when no AI text was emitted:
    1. ``last_tool_result`` — for models (e.g. nemotron-mini) that do not emit
       a final AI text token and use the tool result as the reply.
    2. ``REPLY_PLACEHOLDER`` — when neither AI text nor tool results exist.

    When ``final_only`` is True, returns ``current_section`` (the AI text after
    the last tool call) rather than the full accumulated reply.

    Args:
        full_reply: All AI text tokens concatenated during the loop.
        current_section: AI text since the last tool call.
        last_tool_result: Content of the most recent tool result message.
        final_only: Whether to return only the post-last-tool text.

    Returns:
        The reply text to deliver to the caller.
    """
    # Fallback resolution when no AI text was emitted.
    effective_full = full_reply
    effective_section = current_section
    if not effective_full:
        if last_tool_result:
            logger.warning(
                "No AI text token emitted — using last tool result as reply "
                "(nemotron fallback). preview=%s",
                last_tool_result[:80],
            )
            effective_full = last_tool_result
            effective_section = last_tool_result
        else:
            logger.warning("No AI text token and no tool result — returning placeholder reply.")
            effective_full = REPLY_PLACEHOLDER
            effective_section = REPLY_PLACEHOLDER

    return effective_section if final_only else effective_full


def build_subagent_traces(
    *,
    capture: object,
    ns_to_name: dict[str, str],
    subagent_skill_map: dict[str, list[str]],
    serialize_messages_fn: Callable[[list], list],
) -> tuple:
    """Build per-subagent execution traces from LangChain callback data.

    Returns an empty tuple when *capture* is ``None`` or *ns_to_name* is empty.

    Args:
        capture: ``SubagentMessageCapture`` instance (or ``None``).
        ns_to_name: Mapping from DeepAgents namespace IDs to subagent names.
        subagent_skill_map: Mapping from subagent name to list of skill names.
        serialize_messages_fn: Callable that serialises LangChain messages to
            a list of dicts (passed in to avoid a circular import).

    Returns:
        Tuple of ``SubagentTrace`` instances, one per entry in *ns_to_name*.
    """
    if capture is None or not ns_to_name:
        return ()

    # Lazy import to avoid a circular dependency at module level.
    from atelier.agent_executor import SubagentTrace  # noqa: PLC0415

    traces: list[SubagentTrace] = []
    for ns_id, subagent_name in ns_to_name.items():
        sa_data = capture.get_subagent_data(ns_id)
        traces.append(
            SubagentTrace(
                subagent_name=subagent_name,
                skill_names=subagent_skill_map.get(subagent_name, []),
                tool_call_count=sa_data.tool_calls,
                tool_error_count=sa_data.tool_errors,
                messages_raw=serialize_messages_fn(sa_data.messages),
            )
        )
    return tuple(traces)
