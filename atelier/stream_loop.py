"""Stream-loop state and pure helper functions for AgentExecutor._stream().

Extracted from ``atelier/agent_executor.py`` to keep that module under the
800-line limit and to make the helpers independently testable.

Re-exported in ``atelier/agent_executor.py`` for backward compatibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from atelier.streaming import (
    REPLY_PLACEHOLDER,
    StreamBuffer,
    TaskArgsTracker,
    _EXECUTE_FAILURE_MARKER,
    _extract_thinking,
    _has_tool_use_block,
    _normalise_content,
)

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


async def handle_updates_chunk(
    *,
    ns: list[str],
    data: dict,
    source: str,
    tracker: TaskArgsTracker,
    progress_callback: Callable[[str, str], Awaitable[None]] | None,
) -> None:
    """Process an ``updates`` chunk: log step transitions and detect subagent launches.

    Args:
        ns: Namespace list from the chunk (empty = root, non-empty = subagent).
        data: The ``data`` dict from the updates chunk (node_name → state).
        source: Human-readable source label for logging.
        tracker: The ``TaskArgsTracker`` accumulating subagent name info.
        progress_callback: Optional async callable for progress events.
    """
    for node_name in data:
        if node_name in ("model", "tools"):
            logger.debug("[%s] step: %s", source, node_name)
        if node_name == "model" and ns:
            ns_id = ns[0]
            if not tracker.has_ns(ns_id):
                name = tracker.try_parse_name()
                if name:
                    tracker.register_ns(ns_id, name)
            subagent_label = tracker.get_name_for_ns(ns_id)
            logger.info("[SUBAGENT] launched — name=%s", subagent_label)
            if progress_callback is not None:
                await progress_callback("subagent_start", subagent_label)


async def handle_tool_call_chunks(
    *,
    token: Any,
    source: str,
    state: StreamLoopState,
    tracker: TaskArgsTracker,
    final_only: bool,
    progress_callback: Callable[[str, str], Awaitable[None]] | None,
) -> StreamLoopState:
    """Process tool call chunks from a messages token.

    Args:
        token: The LangChain message token (AIMessageChunk).
        source: Human-readable source label for logging.
        state: Current loop state (updated immutably — a new instance is returned).
        tracker: The ``TaskArgsTracker`` for the current stream.
        final_only: Whether the display is in final-only mode.
        progress_callback: Optional async callable for progress events.

    Returns:
        Updated ``StreamLoopState`` with the new ``pending_tool_name``
        and, when ``final_only=True``, reset ``current_section``.
    """
    tool_call_chunks = getattr(token, "tool_call_chunks", None)
    synthetic_fallback = False
    if not tool_call_chunks:
        tool_use_name = _has_tool_use_block(getattr(token, "content", None))
        if tool_use_name:
            tool_call_chunks = [{"name": tool_use_name, "args": ""}]
            synthetic_fallback = True
            logger.debug("[%s] tool_use block detected via content fallback: %s", source, tool_use_name)
    if not tool_call_chunks:
        return state

    pending_tool_name = state.pending_tool_name
    current_section = state.current_section

    for tc in tool_call_chunks:
        if tc.get("name"):
            pending_tool_name = tc["name"]
            if pending_tool_name == "task":
                tracker.reset()
            logger.info("[%s] tool_call: %s", source, pending_tool_name)
            if progress_callback is not None:
                await progress_callback("tool_call", pending_tool_name)
            if final_only:
                current_section = ""
            if synthetic_fallback:
                break
        if tc.get("args"):
            args_fragment = str(tc["args"])
            if pending_tool_name == "task":
                tracker.accumulate(args_fragment)
                if not tracker.name_logged:
                    name = tracker.try_parse_name()
                    if name:
                        logger.info("[agent] subagent_delegate — name=%s", name)
                        tracker.name_logged = True
            logger.info("[%s] tool_call_args [%s]: %s", source, pending_tool_name, args_fragment[:2000])

    return StreamLoopState(
        full_reply=state.full_reply,
        last_tool_result=state.last_tool_result,
        pending_tool_name=pending_tool_name,
        current_section=current_section,
    )


async def handle_tool_result(
    *,
    token: Any,
    source: str,
    state: StreamLoopState,
    guard: Any,
    progress_callback: Callable[[str, str], Awaitable[None]] | None,
) -> StreamLoopState:
    """Process a tool result token (ToolMessage).

    Records the result in the loop state, updates the ToolErrorGuard,
    and fires the progress callback.

    Args:
        token: The LangChain ToolMessage token.
        source: Human-readable source label for logging.
        state: Current loop state.
        guard: ``ToolErrorGuard`` tracking error counts.
        progress_callback: Optional async callable for progress events.

    Returns:
        Updated ``StreamLoopState`` with the new ``last_tool_result``.
    """
    tool_name = getattr(token, "name", "?")
    normalised = _normalise_content(token.content)
    logger.info("[%s] tool_result [%s]: %s", source, tool_name, normalised[:300])
    if progress_callback is not None:
        await progress_callback("tool_result", f"{tool_name}: {normalised[:100]}")
    is_logical_error = getattr(token, "status", None) == "error" or (
        tool_name == "execute" and _EXECUTE_FAILURE_MARKER in normalised
    )
    guard.record(tool_name, is_logical_error)
    return StreamLoopState(
        full_reply=state.full_reply,
        last_tool_result=normalised,
        pending_tool_name=state.pending_tool_name,
        current_section=state.current_section,
    )


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


async def emit_text(
    *,
    text: str,
    buf: StreamBuffer,
    current_section: str,
    final_only: bool,
) -> str:
    """Emit a text token to the stream buffer or accumulate for final_only mode.

    Args:
        text: The text fragment to emit.
        buf: The StreamBuffer to write to when not in final_only mode.
        current_section: The accumulated text since the last tool call.
        final_only: When True, accumulate in current_section; when False,
            forward to buf.

    Returns:
        The updated current_section string.
    """
    if final_only:
        return current_section + text
    await buf.add(text)
    return current_section


async def emit_thinking(
    *,
    raw: object,
    buf: StreamBuffer,
    current_section: str,
    thinking_enabled: bool,
    final_only: bool,
) -> str:
    """Emit a thinking token to the stream buffer if the thinking event is enabled.

    Args:
        raw: The raw content field from a LangChain AIMessageChunk.
        buf: The StreamBuffer to write to when not in final_only mode.
        current_section: The accumulated text since the last tool call.
        thinking_enabled: Whether the thinking display event is active.
        final_only: When True, append to current_section; when False, push to buf.

    Returns:
        The updated current_section string.
    """
    if not thinking_enabled:
        return current_section
    thinking = _extract_thinking(raw)
    if not thinking:
        return current_section
    wrapped = f"\n> *[thinking]* {thinking}\n"
    if final_only:
        return current_section + wrapped
    await buf.add(wrapped)
    return current_section
