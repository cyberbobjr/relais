"""Streaming utilities for the Atelier agent executor.

Provides the token-buffering class and content-extraction helpers used by
``AgentExecutor._stream()`` to handle LangChain / DeepAgents streaming output.
"""

from __future__ import annotations

from typing import Awaitable, Callable

# Number of buffered characters that triggers an automatic flush to the
# stream callback.  Keep at 10 — changing this value affects all
# streaming consumers (REST SSE, Discord, WhatsApp, …).
STREAM_BUFFER_CHARS: int = 10


class StreamBuffer:
    """Accumulates text tokens and flushes to a callback when a threshold is met.

    Args:
        flush_threshold: Number of buffered characters that triggers an automatic
            flush.
        callback: Async callable that receives a non-empty string chunk.
    """

    def __init__(self, flush_threshold: int, callback: Callable[[str], Awaitable[None]]) -> None:
        self._threshold = flush_threshold
        self._callback = callback
        self._buf: str = ""

    async def add(self, token: str) -> None:
        """Append *token* to the buffer and flush if the threshold is reached.

        Args:
            token: Text fragment to buffer.
        """
        self._buf += token
        if len(self._buf) >= self._threshold:
            await self.flush()

    async def flush(self) -> None:
        """Flush the buffer to the callback if it is non-empty."""
        if self._buf:
            await self._callback(self._buf)
            self._buf = ""


def _extract_thinking(raw: object) -> str:
    """Extract thinking/reflection text from a structured content block list.

    When ``langchain_anthropic`` is configured with extended thinking enabled
    (``thinking={"type": "enabled", ...}``), it emits structured content blocks
    instead of a plain string.  Thinking deltas carry
    ``{"type": "thinking", "thinking": "..."}`` entries alongside text blocks.

    This function is the counterpart of ``_normalise_content``: where that
    function extracts ``type == "text"`` blocks, this one extracts
    ``type == "thinking"`` blocks.  Non-list content (str, other) returns "".

    Args:
        raw: The raw ``.content`` value from a LangChain ``AIMessageChunk``.

    Returns:
        Concatenated thinking text, or empty string if none found.
    """
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for item in raw:
        if isinstance(item, dict) and item.get("type") == "thinking":
            thinking_text = item.get("thinking", "")
            if thinking_text:
                parts.append(thinking_text)
    return "".join(parts)


def _has_tool_use_block(raw: object) -> str | None:
    """Return the tool name if the content list contains a ``tool_use`` block.

    ``langchain_anthropic`` emits both ``token.tool_call_chunks`` and a
    ``{"type": "tool_use", "name": "...", ...}`` block in the content list
    simultaneously.  This function provides a fallback detection path for
    providers that populate the structured content block but leave
    ``tool_call_chunks`` empty.

    Args:
        raw: The raw ``.content`` value from a LangChain ``AIMessageChunk``.

    Returns:
        The tool name string if a ``tool_use`` block with a non-empty ``name``
        is found, otherwise ``None``.
    """
    if not isinstance(raw, list):
        return None
    for item in raw:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            name = item.get("name", "")
            if name:
                return str(name)
    return None
