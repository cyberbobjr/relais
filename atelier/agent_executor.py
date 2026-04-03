"""DeepAgents-based LLM executor for the Atelier brick.

Replaces SDKExecutor with a LangChain/DeepAgents agent that supports
native streaming (token-by-token) and multi-provider models.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Awaitable

from deepagents.backends import BackendProtocol, CompositeBackend, LocalShellBackend, StateBackend
from langchain_core.tools import BaseTool
from langchain.chat_models import BaseChatModel, init_chat_model

from deepagents import create_deep_agent
from common.config_loader import get_relais_home
from common.profile_loader import ProfileConfig
from common.envelope import Envelope

logger = logging.getLogger(__name__)

STREAM_BUFFER_CHARS = 80
REPLY_PLACEHOLDER = "[Aucune réponse générée par le modèle.]"
LONG_TERM_MEMORY_PROMPT = """

Mémoire long-terme:
- Toute information qui doit être retenue au-delà de la conversation courante doit être stockée dans le répertoire `memories`.
- Utilise toujours des chemins du type `/memories/...` pour créer, relire, mettre à jour ou organiser ces souvenirs persistants.
- N'écris pas d'informations de long terme en dehors de `/memories/`.
""".strip()

# Error class names raised by LLM providers (anthropic, openai, google, …) that
# indicate a transient condition — caller must NOT ACK the message so it stays in
# the PEL for automatic re-delivery.  We detect by name to stay provider-agnostic
# and avoid importing provider SDKs directly.
_TRANSIENT_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "RateLimitError",
        "InternalServerError",
        "APIConnectionError",
        "APITimeoutError",
        "ServiceUnavailableError",
    }
)


def _resolve_profile_model(
    profile: ProfileConfig,
) -> BaseChatModel | str:
    """Build the model argument for create_deep_agent from a ProfileConfig.

    When both base_url and api_key_env are None, returns the model string
    directly so create_deep_agent can resolve the provider internally.
    When either is set, constructs a BaseChatModel via init_chat_model(),
    passing only the kwargs that are present.

    Args:
        profile: The resolved ProfileConfig for the current envelope.

    Returns:
        Either the model identifier string, or a pre-built BaseChatModel
        instance with the configured endpoint and credentials.

    Raises:
        KeyError: api_key_env is set but the environment variable is absent.
    """
    if profile.base_url is None and profile.api_key_env is None:
        return profile.model
    kwargs: dict[str, Any] = {}
    if profile.base_url is not None:
        kwargs["base_url"] = profile.base_url
    if profile.api_key_env is not None:
        api_key = os.environ.get(profile.api_key_env)
        if api_key is None:
            raise KeyError(
                f"Environment variable '{profile.api_key_env}' (required by profile "
                f"'{profile.model}') is not set."
            )
        kwargs["api_key"] = api_key
    return init_chat_model(profile.model, **kwargs)


def _is_transient_provider_error(exc: BaseException) -> bool:
    """Return True if *exc* is a known transient provider error.

    Checks the exception class name against a set of well-known transient error
    names shared across major LLM providers (Anthropic, OpenAI, Google, …).
    This avoids importing provider SDKs directly.

    Args:
        exc: The exception to classify.

    Returns:
        True if the error is transient and the caller should not ACK the message.
    """
    return type(exc).__name__ in _TRANSIENT_ERROR_NAMES


def _normalise_content(raw: object) -> str:
    """Normalise a message content field to a plain string.

    Handles the three shapes LangChain message content can take:
    - ``str`` — returned as-is.
    - ``list`` — elements may be ``str`` (joined directly) or ``dict`` blocks
      (only ``{"type": "text", "text": "..."}`` blocks are extracted; other
      block types such as ``image_url`` are silently skipped).
    - anything else — converted via ``str()``.

    Args:
        raw: The raw ``.content`` value from a LangChain message.

    Returns:
        A plain string representation of the content.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item["text"])
        return "".join(parts)
    return str(raw)


class AgentExecutionError(Exception):
    """Raised for permanent/unknown agent execution failures.

    Transient errors (RateLimitError, InternalServerError, APIConnectionError)
    are propagated unwrapped so the caller can leave the message in the PEL
    for re-delivery.

    Args:
        message: Human-readable error description.
        response_body: Optional raw response body from the LLM provider.
    """

    def __init__(self, message: str, response_body: str | None = None) -> None:
        super().__init__(message)
        self.response_body = response_body


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
        backend: Optional backend instance routed to ``/memories/`` paths.
                 When provided, replaces the default ``LocalShellBackend``
                 with the given backend inside a ``CompositeBackend``.
                 When ``None``, falls back to ``LocalShellBackend`` rooted
                 at ``RELAIS_HOME`` (legacy behaviour).
    """

    def __init__(
        self,
        profile: ProfileConfig,
        soul_prompt: str,
        tools: list[BaseTool],
        skills: list[str] | None = None,
        backend: BackendProtocol | None = None,
    ) -> None:
        self._profile = profile
        memories_backend: BackendProtocol = backend or LocalShellBackend(
            root_dir=str(get_relais_home()), virtual_mode=False, inherit_env=True
        )
        # LocalShellBackend(root_dir=str(get_relais_home()), virtual_mode=False, inherit_env=True)
        composite_backend = lambda rt: CompositeBackend(
            default=StateBackend(rt),
            routes={
                "/memories/": memories_backend,
            },
        )
        self._agent = create_deep_agent(
            model=_resolve_profile_model(profile),
            tools=tools,
            system_prompt=_with_long_term_memory_prompt(soul_prompt),
            skills=skills or [],
            backend=composite_backend
        )

    async def execute(
        self,
        envelope: Envelope,
        context: list[dict[str, str]],
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> str:
        """Run the agent on *envelope* with optional streaming and progress events.

        Args:
            envelope: Incoming message envelope; `.content` is the user turn.
            context: Prior conversation turns as ``[{"role": ..., "content": ...}]``.
            stream_callback: Async callable receiving buffered text chunks.
                If ``None``, no token-by-token streaming is performed.
            progress_callback: Async callable receiving ``(event, detail)`` pairs
                that describe pipeline progress (tool calls, tool results, subagent
                starts).  If ``None``, progress events are only logged locally.

        Returns:
            The full LLM reply as a single string.  Falls back to the last
            ToolMessage content when the model emits no AI text (nemotron-mini
            pattern), and to a placeholder constant when nothing is emitted.

        Raises:
            Exception: Transient provider errors (RateLimitError, InternalServerError,
                APIConnectionError, APITimeoutError, ServiceUnavailableError) are
                propagated unwrapped so the caller can leave the message in the PEL.
            AgentExecutionError: Wraps any other non-transient exception.
        """
        messages = _build_messages(envelope, context)
        logger.info(
            "agent.execute start — correlation_id=%s sender=%s turns=%d",
            envelope.correlation_id,
            envelope.sender_id,
            len(messages),
        )
        try:
            reply = await self._stream({"messages": messages}, stream_callback, progress_callback)
            logger.info(
                "agent.execute done — correlation_id=%s reply_len=%d",
                envelope.correlation_id,
                len(reply),
            )
            return reply
        except AgentExecutionError:
            raise
        except Exception as exc:
            if _is_transient_provider_error(exc):
                raise
            raise AgentExecutionError(f"Agent execution failed: {exc}") from exc

    async def _stream(
        self,
        input_data: dict[str, list[dict[str, str]]],
        stream_callback: Callable[[str], Awaitable[None]] | None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> str:
        """Stream tokens and events from the agent, logging all operations.

        Uses the v2 streaming format with ``stream_mode=["updates", "messages"]``
        and ``subgraphs=True`` to capture step transitions, tool calls, tool
        results, and text tokens from both the main agent and any subagents.

        When *stream_callback* is not None, text tokens are forwarded in
        buffered chunks (STREAM_BUFFER_CHARS threshold).

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
            The complete reply assembled from all streamed text tokens, or
            a fallback string when no AI text was emitted.
        """
        buf = ""
        full_reply = ""
        last_tool_result: str = ""
        pending_tool_name: str = ""

        async for chunk in self._agent.astream(
            input_data,
            stream_mode=["updates", "messages"],
            subgraphs=True,
            version="v2",
        ):
            if not (isinstance(chunk, dict) and "type" in chunk and "ns" in chunk and "data" in chunk):
                logger.warning("Unexpected astream chunk shape: %s", type(chunk))
                continue
            chunk_type = chunk["type"]
            ns = chunk["ns"]
            data = chunk["data"]
            source = f"subagent:{ns[0]}" if ns else "agent"

            # ── Step transitions ─────────────────────────────────────────
            if chunk_type == "updates":
                for node_name in data:
                    if node_name in ("model", "tools"):
                        logger.debug("[%s] step: %s", source, node_name)
                    if node_name == "model" and ns:
                        # Subagent (non-root namespace) is starting an LLM call
                        if progress_callback is not None:
                            await progress_callback("subagent_start", source)

            # ── Message events ───────────────────────────────────────────
            elif chunk_type == "messages":
                token, _metadata = data

                # Tool call chunks — only AIMessageChunk has this attribute
                tool_call_chunks = getattr(token, "tool_call_chunks", None)
                if tool_call_chunks:
                    for tc in tool_call_chunks:
                        if tc.get("name"):
                            pending_tool_name = tc["name"]
                            logger.info("[%s] tool_call: %s", source, pending_tool_name)
                            if progress_callback is not None:
                                await progress_callback("tool_call", pending_tool_name)
                        if tc.get("args"):
                            logger.debug(
                                "[%s] tool_args [%s]: %s",
                                source,
                                pending_tool_name,
                                tc["args"],
                            )

                # Tool result
                if token.type == "tool":
                    tool_name = getattr(token, "name", "?")
                    normalised = _normalise_content(token.content)
                    last_tool_result = normalised
                    result_preview = normalised[:300]
                    logger.info(
                        "[%s] tool_result [%s]: %s",
                        source,
                        tool_name,
                        result_preview,
                    )
                    if progress_callback is not None:
                        await progress_callback(
                            "tool_result",
                            f"{tool_name}: {normalised[:100]}",
                        )

                # Text tokens from the LLM.
                # AIMessageChunk.type is "AIMessageChunk" in streaming (not "ai"),
                # so we match any non-tool message that carries content.
                # Note: tool_call_chunks and text content can coexist in the same
                # AIMessageChunk (parallel tool call + narration), so we do NOT
                # gate on the absence of tool_call_chunks.
                if token.type != "tool" and token.content:
                    text = _normalise_content(token.content)
                    if text:
                        full_reply += text
                        if stream_callback is not None:
                            buf += text
                            if len(buf) >= STREAM_BUFFER_CHARS:
                                await stream_callback(buf)
                                buf = ""

        if buf and stream_callback is not None:
            await stream_callback(buf)

        # ── Fallback for models that do not emit a final AI text token ─────
        # (e.g. nemotron-mini / Ollama models that treat tool results as the reply)
        if not full_reply:
            if last_tool_result:
                logger.warning(
                    "No AI text token emitted — using last tool result as reply "
                    "(nemotron fallback). preview=%s",
                    last_tool_result[:80],
                )
                full_reply = last_tool_result
            else:
                logger.warning(
                    "No AI text token and no tool result — returning placeholder reply."
                )
                full_reply = REPLY_PLACEHOLDER

        return full_reply


def _build_messages(envelope: Envelope, context: list[dict[str, str]]) -> list[dict[str, str]]:
    """Assemble the messages list for the agent from context + envelope.

    Filters context to only ``user`` / ``assistant`` turns, prepends a
    synthetic empty user turn when the context starts with an assistant
    turn (LangChain requirement), then appends the envelope content as
    the final user turn.

    Args:
        envelope: Incoming envelope; `.content` is used as the last user turn.
        context: Prior turns ``[{"role": "user"|"assistant", "content": ...}]``.

    Returns:
        A list of ``{"role": ..., "content": ...}`` dicts ready for the agent.
    """
    messages: list[dict[str, str]] = []
    for turn in context:
        role = turn.get("role", "")
        if role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": turn.get("content", "")})

    if messages and messages[0]["role"] == "assistant":
        messages.insert(0, {"role": "user", "content": ""})

    messages.append({"role": "user", "content": envelope.content})
    return messages


def _with_long_term_memory_prompt(soul_prompt: str) -> str:
    """Append long-term memory operating rules to the assembled system prompt."""
    if LONG_TERM_MEMORY_PROMPT in soul_prompt:
        return soul_prompt
    return f"{soul_prompt.rstrip()}\n\n{LONG_TERM_MEMORY_PROMPT}"
