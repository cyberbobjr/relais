"""Unit tests for AgentExecutor progress_callback and fallback reply.

Tests validate:
- progress_callback is called with ('tool_call', tool_name) on tool_call_chunks
- progress_callback is called with ('tool_result', 'name: preview') on tool messages
- progress_callback is called with ('subagent_start', source) on subagent updates
- fallback: when astream yields only tool messages (no AI text), full_reply
  equals the last ToolMessage content (nemotron-mini pattern)
- fallback: when astream yields nothing useful, full_reply is the placeholder string
- existing streaming and error-propagation behaviour is unchanged
"""

from __future__ import annotations

from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
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
    """Return a mock graph state with an empty messages list."""
    state = MagicMock()
    state.values = {"messages": []}
    return state


def _v2_chunk(
    chunk_type: str,
    ns: tuple,
    data: object,
) -> dict:
    """Build a v2 astream chunk dict.

    Args:
        chunk_type: 'messages' or 'updates'.
        ns: Namespace tuple, e.g. () for main agent or ('tools:abc123',) for subagent.
        data: For 'messages': (token, metadata_dict). For 'updates': {node_name: data}.

    Returns:
        Dict with keys 'type', 'ns', 'data'.
    """
    return {"type": chunk_type, "ns": ns, "data": data}


def _ai_token(content: str, tool_call_chunks: list | None = None) -> MagicMock:
    """Build a mock AIMessageChunk.

    Args:
        content: Text content of the token.
        tool_call_chunks: Optional list of tool call chunk dicts.

    Returns:
        MagicMock resembling an AIMessageChunk.
    """
    token = MagicMock()
    token.type = "AIMessageChunk"  # LangChain streaming type (not "ai")
    token.content = content
    token.tool_call_chunks = tool_call_chunks or []
    return token


def _tool_token(name: str, content: str) -> MagicMock:
    """Build a mock ToolMessage token.

    Args:
        name: Name of the tool that produced this result.
        content: The tool result content.

    Returns:
        MagicMock resembling a ToolMessage.
    """
    token = MagicMock()
    token.type = "tool"
    token.name = name
    token.content = content
    token.tool_call_chunks = None
    return token


# ---------------------------------------------------------------------------
# Tests — progress_callback on tool_call
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_emits_progress_on_tool_call() -> None:
    """progress_callback is called with ('tool_call', tool_name) on tool_call_chunks.

    Simulates a single AIMessageChunk with tool_call_chunks=[{"name": "web_search"}]
    and verifies the progress_callback is called with the correct arguments.
    """
    from atelier.agent_executor import AgentExecutor

    tool_call_chunk = {"name": "web_search", "args": ""}
    ai_token = _ai_token(content="", tool_call_chunks=[tool_call_chunk])
    chunk = _v2_chunk("messages", (), (ai_token, {}))

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield chunk

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    progress_calls: list[tuple[str, str]] = []

    async def progress_callback(event: str, detail: str) -> None:
        progress_calls.append((event, detail))

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        await executor.execute(
            _make_envelope("Hi"),
            
            progress_callback=progress_callback,
        )

    assert ("tool_call", "web_search") in progress_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_emits_progress_on_tool_result() -> None:
    """progress_callback is called with ('tool_result', 'name: preview') on tool tokens.

    Simulates a ToolMessage token and verifies the callback receives the
    tool name and a preview of the content (up to 100 chars).
    """
    from atelier.agent_executor import AgentExecutor

    tool_tok = _tool_token("web_search", "search result")
    chunk = _v2_chunk("messages", (), (tool_tok, {}))

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield chunk

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    progress_calls: list[tuple[str, str]] = []

    async def progress_callback(event: str, detail: str) -> None:
        progress_calls.append((event, detail))

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        await executor.execute(
            _make_envelope("Hi"),
            
            progress_callback=progress_callback,
        )

    assert ("tool_result", "web_search: search result") in progress_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_emits_progress_tool_result_truncated_at_100() -> None:
    """Tool result detail is truncated at 100 characters.

    Verifies that very long tool results are trimmed to 100 chars in the
    progress_callback detail argument.
    """
    from atelier.agent_executor import AgentExecutor

    long_content = "x" * 200
    tool_tok = _tool_token("my_tool", long_content)
    chunk = _v2_chunk("messages", (), (tool_tok, {}))

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield chunk

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    progress_calls: list[tuple[str, str]] = []

    async def progress_callback(event: str, detail: str) -> None:
        progress_calls.append((event, detail))

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        await executor.execute(
            _make_envelope("Hi"),
            
            progress_callback=progress_callback,
        )

    # Find the tool_result call
    tool_result_calls = [(e, d) for e, d in progress_calls if e == "tool_result"]
    assert len(tool_result_calls) == 1
    _event, detail = tool_result_calls[0]
    # prefix "my_tool: " is 9 chars, so content should be capped at 100 chars total
    assert len(detail) <= len("my_tool: ") + 100


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_emits_progress_on_subagent_start() -> None:
    """progress_callback is called with ('subagent_start', source) on subagent model node.

    Simulates a v2 'updates' chunk with non-empty ns and node_name='model',
    which signals a subagent starting an LLM call.
    """
    from atelier.agent_executor import AgentExecutor

    chunk = _v2_chunk(
        "updates",
        ("tools:abc123",),
        {"model": {}},
    )

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield chunk

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    progress_calls: list[tuple[str, str]] = []

    async def progress_callback(event: str, detail: str) -> None:
        progress_calls.append((event, detail))

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        await executor.execute(
            _make_envelope("Hi"),
            
            progress_callback=progress_callback,
        )

    subagent_calls = [(e, d) for e, d in progress_calls if e == "subagent_start"]
    assert len(subagent_calls) == 1
    assert subagent_calls[0][0] == "subagent_start"
    # detail contains the subagent source identifier
    assert "tools:abc123" in subagent_calls[0][1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_no_progress_on_main_agent_model_request() -> None:
    """subagent_start is NOT emitted for the main agent (empty ns).

    Only subagents (non-empty ns) trigger the subagent_start progress event.
    """
    from atelier.agent_executor import AgentExecutor

    # Main agent: ns=() (empty tuple)
    chunk = _v2_chunk("updates", (), {"model": {}})

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield chunk

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    progress_calls: list[tuple[str, str]] = []

    async def progress_callback(event: str, detail: str) -> None:
        progress_calls.append((event, detail))

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        await executor.execute(
            _make_envelope("Hi"),
            
            progress_callback=progress_callback,
        )

    subagent_calls = [(e, d) for e, d in progress_calls if e == "subagent_start"]
    assert len(subagent_calls) == 0


# ---------------------------------------------------------------------------
# Tests — progress_callback is None (no errors when not provided)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_progress_callback_none_no_error() -> None:
    """When progress_callback is None, tool events must not raise any error."""
    from atelier.agent_executor import AgentExecutor

    tool_call_chunk = {"name": "search", "args": "q=test"}
    ai_tok = _ai_token(content="", tool_call_chunks=[tool_call_chunk])
    tool_tok = _tool_token("search", "result")
    ai_reply = _ai_token(content="Final answer.")

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (ai_tok, {}))
        yield _v2_chunk("messages", (), (tool_tok, {}))
        yield _v2_chunk("messages", (), (ai_reply, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(
            _make_envelope("Hi"),
            
            # No progress_callback
        )

    assert result.reply_text == "Final answer."


# ---------------------------------------------------------------------------
# Tests — fallback reply (nemotron-mini pattern)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_fallback_last_tool_result_when_reply_empty() -> None:
    """When no AI text token is emitted, fallback to the last ToolMessage content.

    This tests the nemotron-mini pattern where the model does not emit an AI
    text token after ToolMessages — the last tool result is used as the reply.
    """
    from atelier.agent_executor import AgentExecutor

    tool_call_chunk = {"name": "calculator", "args": '{"expr": "2+2"}'}
    ai_tok = _ai_token(content="", tool_call_chunks=[tool_call_chunk])
    tool_tok = _tool_token("calculator", "4")

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        # AI token with tool_call (no text content)
        yield _v2_chunk("messages", (), (ai_tok, {}))
        # Tool result — no AI text follows
        yield _v2_chunk("messages", (), (tool_tok, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(_make_envelope("What is 2+2?"))

    assert result.reply_text == "4"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_fallback_tool_result_list_content() -> None:
    """Fallback works when ToolMessage content is a list of text blocks.

    Some providers return content as a list of dicts with 'type'='text'.
    The fallback must concatenate the text fields.
    """
    from atelier.agent_executor import AgentExecutor

    tool_call_chunk = {"name": "my_tool", "args": "{}"}
    ai_tok = _ai_token(content="", tool_call_chunks=[tool_call_chunk])

    tool_tok = MagicMock()
    tool_tok.type = "tool"
    tool_tok.name = "my_tool"
    tool_tok.content = [
        {"type": "text", "text": "Part one. "},
        {"type": "text", "text": "Part two."},
    ]
    tool_tok.tool_call_chunks = None

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (ai_tok, {}))
        yield _v2_chunk("messages", (), (tool_tok, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(_make_envelope("Do it"))

    assert result.reply_text == "Part one. Part two."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_placeholder_when_all_empty() -> None:
    """When astream yields no useful content, full_reply is the placeholder string.

    Verifies that the constant placeholder '[No response generated by the model.]'
    is returned when no AI text and no tool result content is emitted.
    """
    from atelier.agent_executor import AgentExecutor

    PLACEHOLDER = "[No response generated by the model.]"

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        # Yield only an updates chunk — no messages
        yield _v2_chunk("updates", (), {"tools": {}})

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(_make_envelope("silence"))

    assert result.reply_text == PLACEHOLDER


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_fallback_overridden_by_ai_text() -> None:
    """When AI text IS emitted after a tool result, the AI text takes priority.

    Verifies that the fallback mechanism does not interfere with the normal
    case where the model emits both a tool call and a subsequent AI text reply.
    """
    from atelier.agent_executor import AgentExecutor

    tool_call_chunk = {"name": "search", "args": "q=relais"}
    ai_tok = _ai_token(content="", tool_call_chunks=[tool_call_chunk])
    tool_tok = _tool_token("search", "some results")
    ai_reply = _ai_token(content="Here is the answer.")

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (ai_tok, {}))
        yield _v2_chunk("messages", (), (tool_tok, {}))
        yield _v2_chunk("messages", (), (ai_reply, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(_make_envelope("search relais"))

    assert result.reply_text == "Here is the answer."


# ---------------------------------------------------------------------------
# Tests — existing behaviour preserved (v2 streaming format)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_text_tokens_accumulated() -> None:
    """Text tokens from AIMessageChunk are assembled into full_reply.

    Uses the v2 chunk format with type='messages'.
    """
    from atelier.agent_executor import AgentExecutor

    tok1 = _ai_token("Hello")
    tok2 = _ai_token(", world!")

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (tok1, {}))
        yield _v2_chunk("messages", (), (tok2, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    received: list[str] = []

    async def stream_callback(chunk: str) -> None:
        received.append(chunk)

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(
            _make_envelope("Hi"), stream_callback=stream_callback
        )

    assert result.reply_text == "Hello, world!"
    assert "".join(received) == "Hello, world!"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_error_wrapping_preserved() -> None:
    """Permanent errors from astream are wrapped in AgentExecutionError."""
    from atelier.agent_executor import AgentExecutor, AgentExecutionError

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        raise ValueError("unexpected boom")
        yield  # make it a generator

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        with pytest.raises(AgentExecutionError):
            await executor.execute(_make_envelope("Hi"))



# ---------------------------------------------------------------------------
# Tests — content normalisation edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_text_preserved_when_tool_call_chunks_coexist() -> None:
    """Text content is NOT lost when tool_call_chunks coexist in the same AIMessageChunk.

    Some models emit a chunk that simultaneously carries both a tool_call_chunks
    fragment (initiating a call) and a text narration.  Previously the code gated
    text accumulation on 'not tool_call_chunks', silently dropping that text.
    """
    from atelier.agent_executor import AgentExecutor

    # A chunk that carries both a tool_call fragment and text content
    mixed_tok = _ai_token(
        content="Searching for you…",
        tool_call_chunks=[{"name": "web_search", "args": ""}],
    )
    ai_reply = _ai_token("Done.")

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (mixed_tok, {}))
        yield _v2_chunk("messages", (), (ai_reply, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(_make_envelope("find relais"))

    assert "Searching for you…" in result.reply_text
    assert "Done." in result.reply_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_result_list_with_str_items_not_dropped() -> None:
    """ToolMessage.content list elements that are plain str are included in the fallback.

    LangChain allows ToolMessage.content to be a mixed list of str and dict blocks.
    Previously only dict blocks with type='text' were extracted; plain str items
    were silently dropped, producing an empty fallback.
    """
    from atelier.agent_executor import AgentExecutor

    PLACEHOLDER = "[No response generated by the model.]"

    # ToolMessage with a mixed list: one plain str + one text block dict
    tool_tok = MagicMock()
    tool_tok.type = "tool"
    tool_tok.name = "calculator"
    tool_tok.content = ["Result: ", {"type": "text", "text": "42"}]
    tool_tok.tool_call_chunks = None

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (tool_tok, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(_make_envelope("calc"))

    # Both the str element and the dict text block must appear in the fallback
    assert result.reply_text == "Result: 42"
    assert result.reply_text != PLACEHOLDER


# ---------------------------------------------------------------------------
# Tests — tool_use content fallback detection (extended-thinking mode)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_use_content_fallback_emits_exactly_one_progress_event() -> None:
    """When tool_call_chunks is empty but content has a tool_use block, exactly one
    progress event is emitted for that tool, regardless of how many tool_use blocks
    the content list contains.

    This guards against the synthetic fallback path in _stream() iterating past
    the first named entry and emitting duplicate events.  The break after the
    'if tc.get("name")' branch ensures the single synthetic chunk is processed
    exactly once.
    """
    from atelier.agent_executor import AgentExecutor

    # Token with NO tool_call_chunks but with a structured tool_use block in content.
    # The _has_tool_use_block() function should detect this and create a synthetic
    # one-item list [{"name": "my_tool", "args": ""}].
    token = MagicMock()
    token.type = "AIMessageChunk"
    token.content = [
        {"type": "tool_use", "name": "my_tool", "id": "toolu_abc"},
        {"type": "tool_use", "name": "other_tool", "id": "toolu_xyz"},  # second block
    ]
    token.tool_call_chunks = []  # explicitly empty — triggers content fallback

    chunk = _v2_chunk("messages", (), (token, {}))

    async def fake_astream(input_data, **kwargs) -> AsyncIterator:
        yield chunk

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    progress_calls: list[tuple[str, str]] = []

    async def progress_callback(event: str, detail: str) -> None:
        progress_calls.append((event, detail))

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        await executor.execute(
            _make_envelope("Hi"),
            progress_callback=progress_callback,
        )

    tool_call_events = [(e, d) for e, d in progress_calls if e == "tool_call"]
    # The content fallback returns only the FIRST tool_use block name,
    # so exactly one progress event must be emitted.
    assert len(tool_call_events) == 1, (
        f"Expected exactly 1 tool_call event, got {len(tool_call_events)}: {tool_call_events}"
    )
    assert tool_call_events[0] == ("tool_call", "my_tool")
