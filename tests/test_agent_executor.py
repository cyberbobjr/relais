"""Unit tests for atelier.agent_executor — DeepAgents-based LLM executor.

Tests validate:
- AgentExecutor and AgentExecutionError are importable
- execute() calls ainvoke when no stream_callback is provided
- execute() returns the last message's content from the result
- execute() builds messages from context + envelope correctly
- execute() calls astream with stream_mode="messages" when stream_callback given
- Buffer of 80 chars is respected before flushing to stream_callback
- Remaining buffer is flushed at stream end
- Transient errors (RateLimitError, InternalServerError, APIConnectionError) propagate unwrapped
- Permanent/unknown errors are wrapped in AgentExecutionError
- AgentExecutionError stores optional response_body
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fake transient provider errors (provider-agnostic, matched by class name)
# ---------------------------------------------------------------------------

class RateLimitError(Exception):
    """Fake provider RateLimitError — matched by _is_transient_provider_error."""


class InternalServerError(Exception):
    """Fake provider InternalServerError — matched by _is_transient_provider_error."""


class APIConnectionError(Exception):
    """Fake provider APIConnectionError — matched by _is_transient_provider_error."""


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
    return envelope


def _make_ai_chunk(content: str) -> MagicMock:
    chunk = MagicMock()
    chunk.content = content
    return chunk


def _rate_limit_error() -> RateLimitError:
    return RateLimitError("rate limited")


def _internal_server_error() -> InternalServerError:
    return InternalServerError("server error")


def _connection_error() -> APIConnectionError:
    return APIConnectionError("connection failed")


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_executor_imports() -> None:
    """atelier.agent_executor must be importable with AgentExecutor and AgentExecutionError."""
    from atelier.agent_executor import AgentExecutor, AgentExecutionError  # noqa: F401


# ---------------------------------------------------------------------------
# Non-streaming: ainvoke
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_calls_ainvoke_without_stream_callback() -> None:
    """execute() with no stream_callback must call ainvoke on the agent."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={
        "messages": [MagicMock(content="reply")]
    })

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(),
            soul_prompt="You are helpful.",
            tools=[],
        )
        result = await executor.execute(_make_envelope("Hi"), context=[])

    mock_agent.ainvoke.assert_awaited_once()
    assert result == "reply"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_returns_last_message_content() -> None:
    """execute() must return the content of the last message in the result."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={
        "messages": [
            MagicMock(content="first"),
            MagicMock(content="second"),
            MagicMock(content="final reply"),
        ]
    })

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(_make_envelope("Hi"), context=[])

    assert result == "final reply"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_includes_context_in_messages() -> None:
    """execute() must prepend context turns before the envelope content."""
    from atelier.agent_executor import AgentExecutor

    captured: list[dict] = []

    async def capture_ainvoke(input_data: dict) -> dict:
        captured.append(input_data)
        return {"messages": [MagicMock(content="ok")]}

    mock_agent = MagicMock()
    mock_agent.ainvoke = capture_ainvoke

    context = [
        {"role": "user", "content": "previous message"},
        {"role": "assistant", "content": "previous reply"},
    ]

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        await executor.execute(_make_envelope("new message"), context=context)

    messages = captured[0]["messages"]
    roles = [m["role"] for m in messages]
    contents = [m["content"] for m in messages]

    assert roles == ["user", "assistant", "user"]
    assert contents[-1] == "new message"
    assert "previous message" in contents
    assert "previous reply" in contents


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_inserts_empty_user_turn_when_context_starts_with_assistant() -> None:
    """When context starts with an assistant turn, a synthetic empty user turn is prepended."""
    from atelier.agent_executor import AgentExecutor

    captured: list[dict] = []

    async def capture_ainvoke(input_data: dict) -> dict:
        captured.append(input_data)
        return {"messages": [MagicMock(content="ok")]}

    mock_agent = MagicMock()
    mock_agent.ainvoke = capture_ainvoke

    context = [{"role": "assistant", "content": "Hello!"}]

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        await executor.execute(_make_envelope("Hi"), context=context)

    messages = captured[0]["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == ""
    assert messages[1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# Streaming: astream
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_streaming_calls_stream_callback() -> None:
    """execute() with a stream_callback must call it with accumulated text."""
    from atelier.agent_executor import AgentExecutor

    async def fake_astream(input_data: dict, stream_mode: str = "values"):
        for text in ["Hello", " world"]:
            yield _make_ai_chunk(text), {}

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream

    received: list[str] = []

    async def callback(chunk: str) -> None:
        received.append(chunk)

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(
            _make_envelope("Hi"), context=[], stream_callback=callback
        )

    assert "".join(received) == "Hello world"
    assert result == "Hello world"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_streaming_buffers_below_80_chars() -> None:
    """Chunks below 80 chars are held in the buffer until stream ends."""
    from atelier.agent_executor import AgentExecutor

    short_text = "A" * 75  # below 80-char threshold

    async def fake_astream(input_data: dict, stream_mode: str = "values"):
        yield _make_ai_chunk(short_text), {}

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream

    callback_count = 0

    async def callback(chunk: str) -> None:
        nonlocal callback_count
        callback_count += 1

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(
            _make_envelope("Hi"), context=[], stream_callback=callback
        )

    # Called exactly once at the end (flush remaining)
    assert callback_count == 1
    assert result == short_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_streaming_flushes_at_80_chars() -> None:
    """Buffer flushes when it reaches 80 chars, then remainder is flushed at end."""
    from atelier.agent_executor import AgentExecutor

    chunk_a = "B" * 40
    chunk_b = "C" * 40
    remainder = "D" * 10

    async def fake_astream(input_data: dict, stream_mode: str = "values"):
        yield _make_ai_chunk(chunk_a), {}
        yield _make_ai_chunk(chunk_b), {}
        yield _make_ai_chunk(remainder), {}

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream

    received: list[str] = []

    async def callback(chunk: str) -> None:
        received.append(chunk)

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(
            _make_envelope("Hi"), context=[], stream_callback=callback
        )

    assert len(received) == 2
    assert received[0] == "B" * 40 + "C" * 40
    assert received[1] == "D" * 10
    assert result == chunk_a + chunk_b + remainder


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_streaming_skips_empty_content_chunks() -> None:
    """Chunks with empty or non-string content (e.g. tool_use blocks) are ignored."""
    from atelier.agent_executor import AgentExecutor

    async def fake_astream(input_data: dict, stream_mode: str = "values"):
        yield _make_ai_chunk(""), {}       # empty string — skip
        yield _make_ai_chunk("hello"), {}
        empty_chunk = MagicMock()
        empty_chunk.content = []           # non-string — skip
        yield empty_chunk, {}

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream

    received: list[str] = []

    async def callback(chunk: str) -> None:
        received.append(chunk)

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(
            _make_envelope("Hi"), context=[], stream_callback=callback
        )

    assert result == "hello"
    assert "".join(received) == "hello"


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_propagates_rate_limit_error_unwrapped() -> None:
    """RateLimitError (transient) must propagate without wrapping."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(side_effect=_rate_limit_error())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        with pytest.raises(RateLimitError):
            await executor.execute(_make_envelope("Hi"), context=[])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_propagates_internal_server_error_unwrapped() -> None:
    """InternalServerError (transient) must propagate without wrapping."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(side_effect=_internal_server_error())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        with pytest.raises(InternalServerError):
            await executor.execute(_make_envelope("Hi"), context=[])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_propagates_api_connection_error_unwrapped() -> None:
    """APIConnectionError (transient) must propagate without wrapping."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(side_effect=_connection_error())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        with pytest.raises(APIConnectionError):
            await executor.execute(_make_envelope("Hi"), context=[])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_wraps_unknown_error_in_agent_execution_error() -> None:
    """Unknown/permanent errors must be wrapped in AgentExecutionError."""
    from atelier.agent_executor import AgentExecutor, AgentExecutionError

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(side_effect=ValueError("unexpected failure"))

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        with pytest.raises(AgentExecutionError) as exc_info:
            await executor.execute(_make_envelope("Hi"), context=[])

    assert "unexpected failure" in str(exc_info.value)


@pytest.mark.unit
def test_agent_execution_error_stores_response_body() -> None:
    """AgentExecutionError must store an optional response_body attribute."""
    from atelier.agent_executor import AgentExecutionError

    err = AgentExecutionError("failed", response_body="raw body")
    assert err.response_body == "raw body"
    assert "failed" in str(err)


@pytest.mark.unit
def test_agent_execution_error_defaults_response_body_to_none() -> None:
    """AgentExecutionError.response_body defaults to None when not provided."""
    from atelier.agent_executor import AgentExecutionError

    err = AgentExecutionError("failed")
    assert err.response_body is None


# ---------------------------------------------------------------------------
# Phase 4 — AgentExecutor skills= parameter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_executor_accepts_skills_parameter() -> None:
    """AgentExecutor must accept a skills= list without raising."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(),
            soul_prompt="You are helpful.",
            tools=[],
            skills=["/some/path/to/skills"],
        )
    assert executor is not None


@pytest.mark.unit
def test_executor_passes_skills_to_create_deep_agent() -> None:
    """AgentExecutor must pass the skills list to create_deep_agent."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent) as mock_create:
        AgentExecutor(
            profile=_make_profile(),
            soul_prompt="...",
            tools=[],
            skills=["/path/coding", "/path/research"],
        )

    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs.get("skills") == ["/path/coding", "/path/research"]


@pytest.mark.unit
def test_executor_defaults_skills_to_empty_list() -> None:
    """AgentExecutor without skills= must pass skills=[] to create_deep_agent."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent) as mock_create:
        AgentExecutor(
            profile=_make_profile(),
            soul_prompt="...",
            tools=[],
        )

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs.get("skills", "NOT_SET") == []
