"""DeepAgents-based LLM executor for the Atelier brick.

Replaces SDKExecutor with a LangChain/DeepAgents agent that supports
native streaming (token-by-token) and multi-provider models.

Helper modules extracted to keep this file under 800 lines (all symbols
re-exported here for backward compatibility):
- ``atelier.profile_model`` — ``_resolve_profile_model``
- ``atelier.diagnostic_trace`` — ``format_diagnostic_trace``, ``_render_diagnostic_trace``
- ``atelier.prompts`` — system-prompt constants and builders
- ``atelier.transient_errors`` — ``_is_transient_provider_error``
- ``atelier.streaming`` — ``StreamBuffer``, ``TaskArgsTracker``, ``ChunkPayload``, ``decode_chunk``
- ``atelier.stream_loop`` — ``StreamLoopState``, ``compute_reply_text``, ``build_subagent_traces``
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable, cast

from deepagents.backends import BackendProtocol, CompositeBackend, LocalShellBackend
from deepagents import SubAgent
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langchain_core.runnables import RunnableConfig
from langchain.chat_models import BaseChatModel, init_chat_model
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from deepagents import create_deep_agent
from common.config_loader import get_relais_home, get_relais_project_dir
from common.profile_loader import ProfileConfig
from atelier.display_config import DisplayConfig
from atelier.message_serializer import serialize_messages
from atelier.errors import AgentExecutionError, DiagnosticTrace, ExhaustedRetriesError, ToolErrorGuard
from atelier.streaming import (
    STREAM_BUFFER_CHARS,
    StreamBuffer,
    TaskArgsTracker,
    decode_chunk,
    _extract_thinking,
    _has_tool_use_block,
    _normalise_content,
    _EXECUTE_FAILURE_MARKER,
    REPLY_PLACEHOLDER,
)
from atelier.prompts import (
    LONG_TERM_MEMORY_PROMPT,
    SELF_DIAGNOSIS_PROMPT,
    DIAGNOSTIC_MARKER,
    DIAGNOSTIC_AWARENESS_PROMPT,
    build_project_context_prompt,
    _build_execution_context,
    _enrich_system_prompt,
)
from atelier.transient_errors import (
    _TRANSIENT_ERROR_NAMES,
    _TRANSIENT_VALUE_ERROR_PATTERNS,
    _is_transient_provider_error,
)
from atelier.profile_model import _resolve_profile_model
from atelier.stream_loop import (
    StreamLoopState,
    compute_reply_text,
    build_subagent_traces,
    handle_updates_chunk,
    handle_tool_call_chunks,
    handle_tool_result,
)
from atelier.diagnostic_trace import (
    _DIAGNOSTIC_MAX_CHARS,
    format_diagnostic_trace,
    _render_diagnostic_trace,
)

# Re-export for backward compatibility — callers do
# ``from atelier.agent_executor import AgentExecutionError`` etc.
__all__ = [
    "AgentExecutionError",
    "DiagnosticTrace",
    "ExhaustedRetriesError",
    "ToolErrorGuard",
    "build_project_context_prompt",
    "format_diagnostic_trace",
    "_render_diagnostic_trace",
    "LONG_TERM_MEMORY_PROMPT",
    "DIAGNOSTIC_MARKER",
    "DIAGNOSTIC_AWARENESS_PROMPT",
    "REPLY_PLACEHOLDER",
    "_is_transient_provider_error",
    "_resolve_profile_model",
    "_DIAGNOSTIC_MAX_CHARS",
]
from common.contexts import CTX_AIGUILLEUR, CTX_PORTAIL, AiguilleurCtx, PortailCtx
from common.envelope import Envelope
from atelier.error_synthesizer import extract_tool_errors
from atelier.subagent_capture import SubagentMessageCapture

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubagentTrace:
    """Captured execution trace for a single subagent invocation.

    Attributes:
        subagent_name: Human-readable subagent name (from ns_to_name mapping).
        skill_names: Skill directory names assigned to this subagent.
        tool_call_count: Total tool invocations made by the subagent.
        tool_error_count: Number of tool invocations that returned an error.
        messages_raw: Serialized subagent conversation captured via callbacks.
    """

    subagent_name: str
    skill_names: list[str]
    tool_call_count: int
    tool_error_count: int
    messages_raw: list[dict]


@dataclass(frozen=True)
class AgentResult:
    """Immutable result of a single agentic turn.

    Attributes:
        reply_text: The final text reply produced by the agent (may be an
            empty string when only tool calls were made).
        messages_raw: Serialized flat list of all LangChain messages captured
            from the agent graph state after the turn completes.  Each element
            is a JSON-serializable dict as produced by ``serialize_messages()``.
        tool_call_count: Total number of tool invocations during the turn.
        tool_error_count: Number of tool invocations that returned
            ``status="error"`` during the turn.
        subagent_traces: Per-subagent execution traces captured via LangChain
            callbacks.  Empty tuple when no subagents were invoked.
    """

    reply_text: str
    messages_raw: list[dict]
    tool_call_count: int
    tool_error_count: int
    subagent_traces: tuple[SubagentTrace, ...]


# REPLY_PLACEHOLDER, _EXECUTE_FAILURE_MARKER, _normalise_content
# are imported from atelier.streaming above.

# _TRANSIENT_ERROR_NAMES, _TRANSIENT_VALUE_ERROR_PATTERNS, _is_transient_provider_error
# are imported from atelier.transient_errors above.

# _resolve_profile_model is imported from atelier.profile_model above.

# _DIAGNOSTIC_MAX_CHARS, format_diagnostic_trace, _render_diagnostic_trace
# are imported from atelier.diagnostic_trace above.


class AgentExecutor:
    """Execute LLM requests via a DeepAgents compiled state graph.

    Wraps `create_deep_agent` and exposes a single `execute()` coroutine
    that streams tokens and progress events via the v2 astream protocol.

    Args:
        profile: Profile config with at least a `.model` attribute
                 (format: ``provider:model-id``).
        soul_prompt: System prompt assembled by SoulAssembler.
        tools: List of LangChain tools (StructuredTool / BaseTool) to
               expose to the agent.
        skills: List of absolute directory paths to skill directories,
                passed directly to ``create_deep_agent(skills=...)``.
                Defaults to an empty list (no skills injected).
        backend: Optional backend instance used for ``/memories/`` paths
                 inside the ``CompositeBackend``.  When ``None`` (default),
                 a ``LocalShellBackend`` rooted at ``RELAIS_HOME`` is used
                 for both the default route and the ``/memories/`` route.
        checkpointer: LangGraph checkpoint saver for persistent conversation
                      history across turns.  When ``None`` (default), falls
                      back to a per-instance ``MemorySaver`` (volatile —
                      history lost on restart).  Pass an ``AsyncSqliteSaver``
                      owned by the Atelier singleton for cross-restart
                      persistence.
        subagents: List of SubAgent specs (dicts with ``name``,
                   ``description``, ``system_prompt``, and optionally
                   ``model``, ``tools``, ``skills``).  Each spec is
                   registered as a child agent invocable via the ``task``
                   tool.  Defaults to an empty list (no subagents).
        delegation_prompt: Pre-assembled delegation prompt from the
                   subagent registry.  Appended to the system prompt so
                   the main agent knows when to delegate via ``task()``.
                   Empty string means no delegation instructions.
        display_config: Display configuration controlling which events and
                   tokens are published to the channel. When None (default),
                   a default DisplayConfig is used (all events enabled,
                   final_only=True).
    """

    def __init__(
        self,
        profile: ProfileConfig,
        soul_prompt: str,
        tools: list[BaseTool],
        skills: list[str] | None = None,
        backend: BackendProtocol | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        subagents: list[dict[str, Any]] | None = None,
        delegation_prompt: str = "",
        display_config: DisplayConfig | None = None,
    ) -> None:
        self._profile = profile
        self._display = display_config or DisplayConfig()
        _relais_home = str(get_relais_home())
        _project_dir = str(get_relais_project_dir())
        _shell_env = {"RELAIS_HOME": _relais_home}
        memories_backend: BackendProtocol = backend or LocalShellBackend(
            root_dir=_relais_home, virtual_mode=False, inherit_env=True, env=_shell_env
        )
        composite_backend = CompositeBackend(
            default=LocalShellBackend(root_dir=_relais_home, virtual_mode=False, inherit_env=True, env=_shell_env),
            routes={
                "/memories/": memories_backend,
            },
        )
        # Build skill map: subagent name → list of skill basenames (for trace enrichment)
        self._subagent_skill_map: dict[str, list[str]] = {
            spec["name"]: [Path(s).name for s in spec.get("skills", [])]
            for spec in (subagents or [])
            if spec.get("name")
        }
        # Convert dict subagent specs to SubAgent objects
        compiled_subagents: list[SubAgent] = [
            SubAgent(**spec) for spec in (subagents or [])
        ]
        resolved_skills = skills or []
        _project_context = build_project_context_prompt(_relais_home, _project_dir)
        self._agent = create_deep_agent(
            model=_resolve_profile_model(profile),
            tools=tools,
            system_prompt=_enrich_system_prompt(
                soul_prompt,
                delegation_prompt=delegation_prompt,
                project_context=_project_context,
            ),
            skills=resolved_skills,
            backend=composite_backend,
            checkpointer=checkpointer or MemorySaver(),
            subagents=cast(list[SubAgent | Any], compiled_subagents),
        )
        logger.info(
            "agent.init — model=%s skills=%d %s tools=%d",
            profile.model,
            len(resolved_skills),
            [Path(s).name for s in resolved_skills],
            len(tools),
        )
        if tools:
            tool_names = [t.name for t in tools]
            logger.info(
                "agent.init — mcp_tools=%d names=%s%s",
                len(tool_names),
                tool_names[:10],
                " (truncated)" if len(tool_names) > 10 else "",
            )

    async def execute(
        self,
        envelope: Envelope,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> AgentResult:
        """Run the agent on *envelope* with retry-on-transient-error logic.

        Wraps ``_run_once()`` in a retry loop driven by the profile's
        ``resilience`` configuration.  On each transient error the coroutine
        sleeps for the configured backoff delay before retrying.  When all
        attempts are exhausted, raises ``ExhaustedRetriesError`` (a subclass
        of ``AgentExecutionError``) so the caller routes the message to the DLQ
        and ACKs it — preventing the PEL from being poisoned indefinitely.

        Non-transient errors are wrapped in ``AgentExecutionError`` immediately
        on the first attempt.

        Args:
            envelope: Incoming message envelope; `.content` is the user turn.
            stream_callback: Async callable receiving buffered text chunks.
                If ``None``, no token-by-token streaming is performed.
            progress_callback: Async callable receiving ``(event, detail)`` pairs
                that describe pipeline progress (tool calls, tool results, subagent
                starts).  If ``None``, progress events are only logged locally.

        Returns:
            An ``AgentResult`` containing the full reply text and the serialized
            message list for the completed turn.

        Raises:
            AgentExecutionError: Non-transient failure on the first attempt.
            ExhaustedRetriesError: All retry attempts exhausted on a transient error.
        """
        resilience = self._profile.resilience
        last_exc: BaseException | None = None

        for attempt in range(resilience.retry_attempts + 1):
            try:
                return await self._run_once(envelope, stream_callback, progress_callback)
            except AgentExecutionError:
                raise
            except Exception as exc:
                if not _is_transient_provider_error(exc):
                    raise AgentExecutionError(f"Agent execution failed: {exc}") from exc
                last_exc = exc
                if attempt < resilience.retry_attempts:
                    delay = (
                        resilience.retry_delays[
                            min(attempt, len(resilience.retry_delays) - 1)
                        ]
                        if resilience.retry_delays
                        else 0
                    )
                    logger.warning(
                        "[%s] Transient error (attempt %d/%d), retrying in %ds: %s",
                        envelope.correlation_id,
                        attempt + 1,
                        resilience.retry_attempts,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        raise ExhaustedRetriesError(
            f"All {resilience.retry_attempts} retries exhausted: {last_exc}"
        ) from last_exc

    async def _run_once(
        self,
        envelope: Envelope,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> AgentResult:
        """Execute a single agent turn without retry logic.

        Builds the RunnableConfig, runs the astream loop, captures the final
        graph state, and returns the serialized result.  All exceptions
        propagate to the caller (``execute()``).

        Args:
            envelope: Incoming message envelope; `.content` is the user turn.
            stream_callback: Forwarded to ``_stream()``.
            progress_callback: Forwarded to ``_stream()``.

        Returns:
            An ``AgentResult`` for this single attempt.
        """
        exec_context = _build_execution_context(envelope)
        user_content = f"{exec_context}\n\n{envelope.content}" if exec_context else envelope.content
        inline_images = [r for r in envelope.media_refs if r.data_base64]
        if inline_images:
            content_parts: list[dict[str, Any]] = [{"type": "text", "text": user_content}]
            for ref in inline_images:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{ref.mime_type};base64,{ref.data_base64}"},
                })
            messages: list[dict[str, Any]] = [{"role": "user", "content": content_parts}]
        else:
            messages = [{"role": "user", "content": user_content}]
        logger.info(
            "agent.execute start — correlation_id=%s sender=%s",
            envelope.correlation_id,
            envelope.sender_id,
        )
        portail_ctx: PortailCtx = envelope.context.get(CTX_PORTAIL, {})  # type: ignore
        user_id = portail_ctx.get("user_id", envelope.sender_id)
        capture = SubagentMessageCapture()
        config = RunnableConfig(
            configurable={"thread_id": f"{user_id}:{envelope.session_id}"},
            callbacks=[capture],
        )
        try:
            reply, tool_call_count, tool_error_count, subagent_traces = await self._stream(
                {"messages": messages}, stream_callback, progress_callback, config=config, capture=capture
            )
        except AgentExecutionError as exc:
            # Attempt to capture the partial conversation state so that an
            # error-synthesis LLM call can produce a user-visible explanation.
            try:
                state = await self._agent.aget_state(config)
                partial_messages = serialize_messages(state.values.get("messages", []))
                exc.messages_raw = partial_messages
            except Exception:
                pass  # best-effort; messages_raw stays []
            raise
        # Capture full message list from the agent graph state.
        # aget_state() must not fail — if it does, it means the agent is
        # misconfigured (e.g. no checkpointer).  Propagate the exception
        # to execute(): transient errors are retried, others go to DLQ.
        state = await self._agent.aget_state(config)
        state_messages = state.values.get("messages", [])
        messages_raw = serialize_messages(state_messages)
        logger.info(
            "agent.execute done — correlation_id=%s reply_len=%d messages=%d "
            "tool_calls=%d tool_errors=%d",
            envelope.correlation_id,
            len(reply),
            len(messages_raw),
            tool_call_count,
            tool_error_count,
        )
        return AgentResult(
            reply_text=reply,
            messages_raw=messages_raw,
            tool_call_count=tool_call_count,
            tool_error_count=tool_error_count,
            subagent_traces=subagent_traces,
        )

    async def inject_diagnostic_message(
        self,
        envelope: Envelope,
        diagnostic_text: str,
    ) -> bool:
        """Append a hidden diagnostic message to the thread's LangGraph checkpoint.

        Called after an ``AgentExecutionError`` so that follow-up questions
        (e.g. "what went wrong?") can be answered precisely.  The message is
        an ``AIMessage`` whose content starts with ``[DIAGNOSTIC — internal]``;
        the system prompt instructs the agent to surface it in plain language
        when the user asks about prior failures.

        Args:
            envelope: The envelope from the failed turn; its ``portail`` context
                and ``session_id`` determine the LangGraph thread_id.
            diagnostic_text: Pre-formatted text from ``_render_diagnostic_trace()``.

        Returns:
            ``True`` if the injection succeeded, ``False`` otherwise (empty
            state, empty text, or any exception — all handled non-fatally).
        """
        if not diagnostic_text.strip():
            return False
        try:
            portail_ctx: PortailCtx = envelope.context.get(CTX_PORTAIL, {})  # type: ignore
            user_id = portail_ctx.get("user_id", envelope.sender_id)
            config = RunnableConfig(
                configurable={"thread_id": f"{user_id}:{envelope.session_id}"}
            )
            state = await self._agent.aget_state(config)
            if not state or not state.values.get("messages"):
                logger.debug(
                    "inject_diagnostic: empty state, skipping — corr=%s",
                    envelope.correlation_id,
                )
                return False
            await self._agent.aupdate_state(config, {"messages": [AIMessage(content=diagnostic_text)]})
            logger.info(
                "inject_diagnostic: injected %d chars — corr=%s",
                len(diagnostic_text),
                envelope.correlation_id,
            )
            return True
        except Exception as exc:
            logger.warning(
                "inject_diagnostic: failed — corr=%s error=%s",
                envelope.correlation_id,
                exc,
            )
            return False

    async def _stream(
        self,
        input_data: dict[str, list[dict[str, Any]]],
        stream_callback: Callable[[str], Awaitable[None]] | None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
        config: RunnableConfig | None = None,
        capture: SubagentMessageCapture | None = None,
    ) -> tuple[str, int, int, tuple[SubagentTrace, ...]]:
        """Stream tokens and events from the agent, logging all operations.

        Uses the v2 streaming format with ``stream_mode=["updates", "messages"]``
        and ``subgraphs=True`` to capture step transitions, tool calls, tool
        results, and text tokens from both the main agent and any subagents.

        When *stream_callback* is not None, text tokens are forwarded via
        ``StreamBuffer`` (STREAM_BUFFER_CHARS threshold).  Tool error limits are
        enforced by ``ToolErrorGuard``.

        When *progress_callback* is not None, it is called with ``(event, detail)``
        pairs for:
        - ``('tool_call', tool_name)`` — when a tool call is initiated.
        - ``('tool_result', 'name: preview')`` — when a tool result arrives
          (preview capped at 100 characters).
        - ``('subagent_start', source)`` — when a subagent starts an LLM call
          (only emitted for non-root namespaces).

        Falls back to the last ToolMessage content when the model emits no AI
        text tokens (nemotron-mini pattern).  Returns a placeholder constant
        when neither AI text nor tool results are available.

        Args:
            input_data: Input dict passed to ``agent.astream``.
            stream_callback: Async callable receiving flushed text chunks,
                or ``None`` for logging-only mode.
            progress_callback: Async callable receiving ``(event, detail)``
                pipeline progress pairs, or ``None`` to skip progress events.

        Returns:
            A 4-tuple of ``(reply, tool_call_count, tool_error_count, subagent_traces)``
            where *reply* is the complete text assembled from all streamed tokens (or
            a fallback string when no AI text was emitted), *tool_call_count*
            is the total number of tool invocations, *tool_error_count* is
            the number of invocations that returned ``status="error"``, and
            *subagent_traces* is a tuple of per-subagent execution traces captured
            via LangChain callbacks (empty tuple when no subagents were invoked).
        """
        state = StreamLoopState()
        tracker = TaskArgsTracker()
        guard = ToolErrorGuard(max_consecutive=5, max_total=8)

        async def _noop_callback(chunk: str) -> None:  # pragma: no cover
            pass

        buf = StreamBuffer(
            flush_threshold=STREAM_BUFFER_CHARS,
            callback=stream_callback if stream_callback is not None else _noop_callback,
        )
        stream_kwargs: dict = {"stream_mode": ["updates", "messages"], "subgraphs": True, "version": "v2"}
        if config is not None:
            stream_kwargs["config"] = config

        async with contextlib.aclosing(self._agent.astream(input_data, **stream_kwargs)) as stream:
            async for raw_chunk in stream:
                chunk = decode_chunk(raw_chunk)
                if chunk is None:
                    logger.warning("Unexpected astream chunk shape: %s", type(raw_chunk))
                    continue

                if chunk.chunk_type == "updates":
                    await handle_updates_chunk(
                        ns=chunk.ns, data=chunk.data, source=chunk.source,
                        tracker=tracker, progress_callback=progress_callback,
                    )
                elif chunk.chunk_type == "messages":
                    token, _metadata = chunk.data
                    state = await handle_tool_call_chunks(
                        token=token, source=chunk.source, state=state, tracker=tracker,
                        final_only=self._display.final_only, progress_callback=progress_callback,
                    )
                    if token.type == "tool":
                        state = await handle_tool_result(
                            token=token, source=chunk.source, state=state,
                            guard=guard, progress_callback=progress_callback,
                        )
                    if token.type != "tool" and token.content:
                        text = _normalise_content(token.content)
                        if text:
                            state = StreamLoopState(
                                full_reply=state.full_reply + text,
                                last_tool_result=state.last_tool_result,
                                pending_tool_name=state.pending_tool_name,
                                current_section=await self._emit_text(text, buf, state.current_section),
                            )
                        new_section = await self._emit_thinking(token.content, buf, state.current_section)
                        if new_section is not state.current_section:
                            state = StreamLoopState(
                                full_reply=state.full_reply,
                                last_tool_result=state.last_tool_result,
                                pending_tool_name=state.pending_tool_name,
                                current_section=new_section,
                            )

        if stream_callback is not None:
            if self._display.final_only:
                await buf.add(state.current_section)
            await buf.flush()

        reply_text = compute_reply_text(
            full_reply=state.full_reply,
            current_section=state.current_section,
            last_tool_result=state.last_tool_result,
            final_only=self._display.final_only,
        )
        subagent_traces = build_subagent_traces(
            capture=capture,
            ns_to_name=tracker.ns_to_name,
            subagent_skill_map=self._subagent_skill_map,
            serialize_messages_fn=serialize_messages,
        )
        return reply_text, guard.total_calls, guard.total_errors, subagent_traces


    async def _emit_text(self, text: str, buf: StreamBuffer, current_section: str) -> str:
        """Emit a text token to the stream buffer or accumulate for final_only mode.

        Args:
            text: The text fragment to emit.
            buf: The StreamBuffer to write to when not in final_only mode.
            current_section: The accumulated text since the last tool call.

        Returns:
            The updated current_section string.
        """
        if self._display.final_only:
            return current_section + text
        await buf.add(text)
        return current_section

    async def _emit_thinking(self, raw: object, buf: StreamBuffer, current_section: str) -> str:
        """Emit a thinking token to the stream buffer if the thinking event is enabled.

        Thinking tokens are only emitted when ``events["thinking"]`` is True in the
        DisplayConfig. The token is wrapped in a blockquote for visual distinction.

        Behaviour depends on ``final_only``:

        - ``final_only=False`` (stream mode): the thinking block is pushed to ``buf``
          so it appears in the live token stream. It is NOT added to ``full_reply`` or
          ``current_section`` — the stored reply text will not contain thinking content.
        - ``final_only=True`` (block mode): the thinking block is appended to
          ``current_section`` and therefore included in the final reply sent to the
          channel. This is intentional and useful for debugging or transparency: the
          user receives the model's reasoning inline with the answer.

        Args:
            raw: The raw content field from a LangChain AIMessageChunk.
            buf: The StreamBuffer to write to when not in final_only mode.
            current_section: The accumulated text since the last tool call.

        Returns:
            The updated current_section string.
        """
        if not self._display.events.get("thinking", False):
            return current_section
        thinking = _extract_thinking(raw)
        if not thinking:
            return current_section
        wrapped = f"\n> *[thinking]* {thinking}\n"
        if self._display.final_only:
            return current_section + wrapped
        await buf.add(wrapped)
        return current_section


# _build_execution_context and _enrich_system_prompt are imported from atelier.prompts above.
