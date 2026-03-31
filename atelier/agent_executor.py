"""DeepAgents-based LLM executor for the Atelier brick.

Replaces SDKExecutor with a LangChain/DeepAgents agent that supports
native streaming (token-by-token) and multi-provider models.
"""

from __future__ import annotations

from typing import Callable, Awaitable

from langchain_core.tools import BaseTool

from deepagents import create_deep_agent
from atelier.profile_loader import ProfileConfig
from common.envelope import Envelope

STREAM_BUFFER_CHARS = 80

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
    that handles both non-streaming (ainvoke) and streaming (astream) paths.

    Args:
        profile: Profile config with at least a `.model` attribute
                 (format: ``provider:model-id``).
        soul_prompt: System prompt assembled by SoulAssembler.
        tools: List of LangChain tools (StructuredTool / BaseTool) to
               expose to the agent.
    """

    def __init__(
        self,
        profile: ProfileConfig,
        soul_prompt: str,
        tools: list[BaseTool],
    ) -> None:
        self._profile = profile
        self._agent = create_deep_agent(
            model=profile.model,
            tools=tools,
            system_prompt=soul_prompt,
        )

    async def execute(
        self,
        envelope: Envelope,
        context: list[dict[str, str]],
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Run the agent on *envelope* with optional streaming.

        Args:
            envelope: Incoming message envelope; `.content` is the user turn.
            context: Prior conversation turns as ``[{"role": ..., "content": ...}]``.
            stream_callback: Async callable receiving buffered text chunks.
                If ``None``, the non-streaming (ainvoke) path is used.

        Returns:
            The full LLM reply as a single string.

        Raises:
            Exception: Transient provider errors (RateLimitError, InternalServerError,
                APIConnectionError, APITimeoutError, ServiceUnavailableError) are
                propagated unwrapped so the caller can leave the message in the PEL.
            AgentExecutionError: Wraps any other non-transient exception.
        """
        messages = _build_messages(envelope, context)
        try:
            if stream_callback is None:
                result = await self._agent.ainvoke({"messages": messages})
                last_content = result["messages"][-1].content
                if isinstance(last_content, list):
                    last_content = "".join(
                        block["text"]
                        for block in last_content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                return last_content
            return await self._stream({"messages": messages}, stream_callback)
        except AgentExecutionError:
            raise
        except Exception as exc:
            if _is_transient_provider_error(exc):
                raise
            raise AgentExecutionError(f"Agent execution failed: {exc}") from exc

    async def _stream(
        self,
        input_data: dict[str, list[dict[str, str]]],
        stream_callback: Callable[[str], Awaitable[None]],
    ) -> str:
        """Stream tokens from the agent and flush via *stream_callback*.

        Accumulates tokens in a buffer and flushes when the buffer reaches
        STREAM_BUFFER_CHARS characters, then flushes any remainder at the end.

        Args:
            input_data: Input dict passed to ``agent.astream``.
            stream_callback: Async callable receiving flushed chunks.

        Returns:
            The complete reply assembled from all streamed tokens.
        """
        buf = ""
        full_reply = ""
        async for chunk, _ in self._agent.astream(input_data, stream_mode="messages"):
            if not isinstance(chunk.content, str) or not chunk.content:
                continue
            token = chunk.content
            full_reply += token
            buf += token
            if len(buf) >= STREAM_BUFFER_CHARS:
                await stream_callback(buf)
                buf = ""
        if buf:
            await stream_callback(buf)
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
