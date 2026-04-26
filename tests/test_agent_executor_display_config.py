"""Unit tests for AgentExecutor with DisplayConfig — final_only and thinking paths.

Tests:
1. final_only=True: pre-tool narration is discarded; only post-tool text is returned
2. final_only=True + stream_callback: only the final section is streamed
3. final_only=True + thinking=True: thinking tokens are included in the final reply
4. final_only=False + thinking=True: thinking goes to stream only, NOT in reply_text
5. final_only=False + thinking=False (default): thinking tokens are not emitted at all
"""

from __future__ import annotations

from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers (mirrors test_agent_executor_progress.py conventions)
# ---------------------------------------------------------------------------


def _make_profile(model: str = "anthropic:claude-haiku-4-5") -> MagicMock:
    profile = MagicMock()
    profile.model = model
    profile.base_url = None
    profile.api_key_env = None
    return profile


def _make_envelope(content: str = "Hello") -> MagicMock:
    envelope = MagicMock()
    envelope.content = content
    envelope.correlation_id = "test-corr-id"
    envelope.sender_id = "test:user"
    return envelope


def _make_agent_state() -> MagicMock:
    state = MagicMock()
    state.values = {"messages": []}
    return state


def _v2_chunk(chunk_type: str, ns: tuple, data: object) -> dict:
    return {"type": chunk_type, "ns": ns, "data": data}


def _ai_token(content: object, tool_call_chunks: list | None = None) -> MagicMock:
    """Build a mock AIMessageChunk.

    Args:
        content: Text content (str) or structured content list (list of dicts).
        tool_call_chunks: Optional list of tool call chunk dicts.

    Returns:
        MagicMock resembling an AIMessageChunk.
    """
    token = MagicMock()
    token.type = "AIMessageChunk"
    token.content = content
    token.tool_call_chunks = tool_call_chunks or []
    return token


def _tool_token(name: str, content: str) -> MagicMock:
    token = MagicMock()
    token.type = "tool"
    token.name = name
    token.content = content
    token.tool_call_chunks = None
    return token


def _thinking_token(thinking_text: str, reply_text: str = "") -> MagicMock:
    """Build a mock AIMessageChunk with a structured thinking block.

    Simulates the langchain_anthropic extended-thinking format where content
    is a list of dicts, thinking blocks have type='thinking'.

    Args:
        thinking_text: The internal reasoning text.
        reply_text: Optional plain text content alongside the thinking block.

    Returns:
        MagicMock resembling an AIMessageChunk with structured content.
    """
    content: list[dict] = [{"type": "thinking", "thinking": thinking_text}]
    if reply_text:
        content.append({"type": "text", "text": reply_text})
    token = MagicMock()
    token.type = "AIMessageChunk"
    token.content = content
    token.tool_call_chunks = []
    return token


# ---------------------------------------------------------------------------
# 1. final_only=True — pre-tool narration is discarded
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_final_only_discards_pre_tool_narration() -> None:
    """With final_only=True, text emitted before a tool call is excluded from reply_text.

    Sequence:
      AI text "Narration before tool."
      AI tool_call_chunk (triggers current_section reset)
      ToolMessage result
      AI text "Final answer."

    Expected reply_text: "Final answer." only.
    """
    from atelier.agent_executor import AgentExecutor
    from atelier.display_config import DisplayConfig

    narration_tok = _ai_token("Narration before tool.")
    tool_call_tok = _ai_token("", tool_call_chunks=[{"name": "search", "args": ""}])
    tool_result_tok = _tool_token("search", "some result")
    final_tok = _ai_token("Final answer.")

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (narration_tok, {}))
        yield _v2_chunk("messages", (), (tool_call_tok, {}))
        yield _v2_chunk("messages", (), (tool_result_tok, {}))
        yield _v2_chunk("messages", (), (final_tok, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(),
            memory_paths=[],
            tools=[],
            display_config=DisplayConfig(final_only=True),
        )
        result = await executor.execute(_make_envelope("search something"))

    assert result.reply_text == "Final answer."
    assert "Narration before tool." not in result.reply_text


# ---------------------------------------------------------------------------
# 2. final_only=True + stream_callback — only final section is streamed
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_final_only_streams_only_final_section() -> None:
    """With final_only=True, the stream_callback receives only the post-tool text.

    Pre-tool narration must NOT appear in the stream. The streamed content
    must equal the returned reply_text.
    """
    from atelier.agent_executor import AgentExecutor
    from atelier.display_config import DisplayConfig

    narration_tok = _ai_token("Narration.")
    tool_call_tok = _ai_token("", tool_call_chunks=[{"name": "search", "args": ""}])
    tool_result_tok = _tool_token("search", "ok")
    final_tok = _ai_token("Final.")

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (narration_tok, {}))
        yield _v2_chunk("messages", (), (tool_call_tok, {}))
        yield _v2_chunk("messages", (), (tool_result_tok, {}))
        yield _v2_chunk("messages", (), (final_tok, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    received: list[str] = []

    async def stream_callback(chunk: str) -> None:
        received.append(chunk)

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(),
            memory_paths=[],
            tools=[],
            display_config=DisplayConfig(final_only=True),
        )
        result = await executor.execute(
            _make_envelope("go"), stream_callback=stream_callback
        )

    streamed = "".join(received)
    assert streamed == "Final."
    assert "Narration." not in streamed
    assert result.reply_text == "Final."


# ---------------------------------------------------------------------------
# 3. final_only=True + thinking=True — thinking included in final reply
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_final_only_with_thinking_includes_thinking_in_reply() -> None:
    """With final_only=True and thinking=True, thinking tokens appear in reply_text.

    This is intentional: final_only block mode is useful for debugging, and
    the thinking block is appended to current_section so it reaches the user.
    """
    from atelier.agent_executor import AgentExecutor
    from atelier.display_config import DisplayConfig

    think_tok = _thinking_token("Let me reason about this.")
    reply_tok = _ai_token("The answer is 42.")

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (think_tok, {}))
        yield _v2_chunk("messages", (), (reply_tok, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    events = {"tool_call": True, "tool_result": True, "subagent_start": True, "thinking": True}
    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(),
            memory_paths=[],
            tools=[],
            display_config=DisplayConfig(final_only=True, events=events),
        )
        result = await executor.execute(_make_envelope("what is 6x7?"))

    assert "Let me reason about this." in result.reply_text
    assert "The answer is 42." in result.reply_text
    # Thinking is wrapped in the blockquote format
    assert "[thinking]" in result.reply_text


# ---------------------------------------------------------------------------
# 4. final_only=False + thinking=True — thinking goes to stream, NOT reply_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_mode_thinking_goes_to_stream_not_reply() -> None:
    """With final_only=False + thinking=True, thinking appears in the stream only.

    The stream_callback receives the thinking block, but reply_text contains
    only the plain text tokens (not the thinking content).
    """
    from atelier.agent_executor import AgentExecutor
    from atelier.display_config import DisplayConfig

    think_tok = _thinking_token("Internal reasoning here.")
    reply_tok = _ai_token("Plain reply.")

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (think_tok, {}))
        yield _v2_chunk("messages", (), (reply_tok, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    received: list[str] = []

    async def stream_callback(chunk: str) -> None:
        received.append(chunk)

    events = {"tool_call": True, "tool_result": True, "subagent_start": True, "thinking": True}
    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(),
            memory_paths=[],
            tools=[],
            display_config=DisplayConfig(final_only=False, events=events),
        )
        result = await executor.execute(
            _make_envelope("think"), stream_callback=stream_callback
        )

    streamed = "".join(received)
    # Thinking appears in the live stream
    assert "Internal reasoning here." in streamed
    assert "[thinking]" in streamed
    # But reply_text contains only the plain text reply
    assert result.reply_text == "Plain reply."
    assert "Internal reasoning here." not in result.reply_text


# ---------------------------------------------------------------------------
# 5. final_only=False + thinking=False (default) — thinking not emitted
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_mode_thinking_disabled_not_in_stream() -> None:
    """With thinking=False (default), thinking tokens are silently discarded.

    Neither the stream nor reply_text should contain any thinking content.
    """
    from atelier.agent_executor import AgentExecutor
    from atelier.display_config import DisplayConfig

    think_tok = _thinking_token("Secret reasoning.")
    reply_tok = _ai_token("Clean reply.")

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (think_tok, {}))
        yield _v2_chunk("messages", (), (reply_tok, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    received: list[str] = []

    async def stream_callback(chunk: str) -> None:
        received.append(chunk)

    # Default DisplayConfig has thinking=False
    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(),
            memory_paths=[],
            tools=[],
            display_config=DisplayConfig(final_only=False),
        )
        result = await executor.execute(
            _make_envelope("think"), stream_callback=stream_callback
        )

    streamed = "".join(received)
    assert "Secret reasoning." not in streamed
    assert "[thinking]" not in streamed
    assert result.reply_text == "Clean reply."
