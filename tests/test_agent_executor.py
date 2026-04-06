"""Unit tests for atelier.agent_executor — DeepAgents-based LLM executor.

Tests validate:
- AgentExecutor and AgentExecutionError are importable
- execute() uses astream with v2 format (stream_mode, subgraphs, version)
- execute() returns the full assembled reply from AI text tokens
- execute() builds messages from context + envelope correctly
- Buffer of 80 chars is respected before flushing to stream_callback
- Remaining buffer is flushed at stream end
- Transient errors (RateLimitError, InternalServerError, APIConnectionError) raise ExhaustedRetriesError after retries
- ValueError with rate-limit message is classified as transient by _is_transient_provider_error
- Retry loop succeeds on second attempt when first raises a transient error
- Permanent/unknown errors are wrapped in AgentExecutionError
- AgentExecutionError stores optional response_body
- skills= parameter is forwarded to create_deep_agent
"""

from __future__ import annotations

from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.profile_loader import ResilienceConfig


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
    # Zero retries so single-attempt tests remain valid; loop iterates exactly once.
    profile.resilience = ResilienceConfig(retry_attempts=0, retry_delays=[])
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


def _v2_chunk(chunk_type: str, ns: tuple, data: object) -> dict:
    """Build a v2 astream chunk dict.

    Args:
        chunk_type: 'messages' or 'updates'.
        ns: Namespace tuple.
        data: For 'messages': (token, metadata_dict). For 'updates': dict.

    Returns:
        Dict with keys 'type', 'ns', 'data'.
    """
    return {"type": chunk_type, "ns": ns, "data": data}


def _ai_token(content: str, tool_call_chunks: list | None = None) -> MagicMock:
    """Build a mock AIMessageChunk for v2 streaming.

    Args:
        content: Text content.
        tool_call_chunks: Optional tool call chunk list.

    Returns:
        MagicMock resembling an AIMessageChunk.
    """
    token = MagicMock()
    token.type = "ai"
    token.content = content
    token.tool_call_chunks = tool_call_chunks or []
    return token


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
# Streaming with v2 format (always uses astream)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_returns_ai_text_from_astream() -> None:
    """execute() assembles the reply from AI text tokens in v2 astream chunks."""
    from atelier.agent_executor import AgentExecutor

    tok = _ai_token("reply from model")

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (tok, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(),
            soul_prompt="You are helpful.",
            tools=[],
        )
        result = await executor.execute(_make_envelope("Hi"))

    assert result.reply_text == "reply from model"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_accumulates_multiple_ai_tokens() -> None:
    """execute() concatenates multiple AI text tokens into a single reply."""
    from atelier.agent_executor import AgentExecutor

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (_ai_token("Hello"), {}))
        yield _v2_chunk("messages", (), (_ai_token(", world!"), {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(_make_envelope("Hi"))

    assert result.reply_text == "Hello, world!"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_streaming_calls_stream_callback() -> None:
    """execute() with a stream_callback must call it with accumulated text."""
    from atelier.agent_executor import AgentExecutor

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        for text in ["Hello", " world"]:
            yield _v2_chunk("messages", (), (_ai_token(text), {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    received: list[str] = []

    async def callback(chunk: str) -> None:
        received.append(chunk)

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(
            _make_envelope("Hi"), stream_callback=callback
        )

    assert "".join(received) == "Hello world"
    assert result.reply_text == "Hello world"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_streaming_buffers_below_80_chars() -> None:
    """Chunks below 80 chars are held in the buffer until stream ends."""
    from atelier.agent_executor import AgentExecutor

    short_text = "A" * 75  # below 80-char threshold

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (_ai_token(short_text), {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    callback_count = 0

    async def callback(chunk: str) -> None:
        nonlocal callback_count
        callback_count += 1

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(
            _make_envelope("Hi"), stream_callback=callback
        )

    # Called exactly once at the end (flush remaining)
    assert callback_count == 1
    assert result.reply_text == short_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_streaming_flushes_at_80_chars() -> None:
    """Buffer flushes when it reaches 80 chars, then remainder is flushed at end."""
    from atelier.agent_executor import AgentExecutor

    chunk_a = "B" * 40
    chunk_b = "C" * 40
    remainder = "D" * 10

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (_ai_token(chunk_a), {}))
        yield _v2_chunk("messages", (), (_ai_token(chunk_b), {}))
        yield _v2_chunk("messages", (), (_ai_token(remainder), {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    received: list[str] = []

    async def callback(chunk: str) -> None:
        received.append(chunk)

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(
            _make_envelope("Hi"), stream_callback=callback
        )

    assert len(received) == 2
    assert received[0] == "B" * 40 + "C" * 40
    assert received[1] == "D" * 10
    assert result.reply_text == chunk_a + chunk_b + remainder


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_streaming_skips_empty_content_chunks() -> None:
    """Chunks with empty or list content (e.g. tool_use blocks) are ignored."""
    from atelier.agent_executor import AgentExecutor

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        yield _v2_chunk("messages", (), (_ai_token(""), {}))       # empty — skip
        yield _v2_chunk("messages", (), (_ai_token("hello"), {}))

        # list content with no text blocks — skip
        list_tok = MagicMock()
        list_tok.type = "ai"
        list_tok.content = [{"type": "tool_use", "id": "abc"}]
        list_tok.tool_call_chunks = []
        yield _v2_chunk("messages", (), (list_tok, {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    received: list[str] = []

    async def callback(chunk: str) -> None:
        received.append(chunk)

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        result = await executor.execute(
            _make_envelope("Hi"), stream_callback=callback
        )

    assert result.reply_text == "hello"
    assert "".join(received) == "hello"


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_raises_exhausted_on_rate_limit() -> None:
    """RateLimitError (transient) with no retries raises ExhaustedRetriesError."""
    from atelier.agent_executor import AgentExecutor, ExhaustedRetriesError

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        raise RateLimitError("rate limited")
        yield  # make it a generator

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        with pytest.raises(ExhaustedRetriesError):
            await executor.execute(_make_envelope("Hi"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_raises_exhausted_on_internal_server_error() -> None:
    """InternalServerError (transient) with no retries raises ExhaustedRetriesError."""
    from atelier.agent_executor import AgentExecutor, ExhaustedRetriesError

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        raise InternalServerError("server error")
        yield  # make it a generator

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        with pytest.raises(ExhaustedRetriesError):
            await executor.execute(_make_envelope("Hi"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_raises_exhausted_on_api_connection_error() -> None:
    """APIConnectionError (transient) with no retries raises ExhaustedRetriesError."""
    from atelier.agent_executor import AgentExecutor, ExhaustedRetriesError

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        raise APIConnectionError("connection failed")
        yield  # make it a generator

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        with pytest.raises(ExhaustedRetriesError):
            await executor.execute(_make_envelope("Hi"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_retries_then_succeeds() -> None:
    """Transient error on first attempt should retry and succeed on second."""
    from atelier.agent_executor import AgentExecutor, AgentResult

    call_count = 0

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RateLimitError("rate limited")
        yield {"type": "messages", "ns": (), "data": (_ai_token("ok"), {})}

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    state = _make_agent_state()
    mock_agent.aget_state = AsyncMock(return_value=state)

    profile = _make_profile()
    from common.profile_loader import ResilienceConfig
    profile.resilience = ResilienceConfig(retry_attempts=1, retry_delays=[0])

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            executor = AgentExecutor(profile=profile, soul_prompt="...", tools=[])
            result = await executor.execute(_make_envelope("Hi"))

    assert call_count == 2
    assert isinstance(result, AgentResult)
    assert result.reply_text == "ok"


@pytest.mark.unit
def test_is_transient_provider_error_matches_rate_limit_valueerror() -> None:
    """ValueError with a rate-limit message should be classified as transient."""
    from atelier.agent_executor import _is_transient_provider_error

    assert _is_transient_provider_error(ValueError("rate limit exceeded"))
    assert _is_transient_provider_error(ValueError("upstream error: 502 Bad Gateway"))
    assert _is_transient_provider_error(ValueError("model overloaded, try again"))
    assert not _is_transient_provider_error(ValueError("unexpected schema mismatch"))
    assert not _is_transient_provider_error(ValueError(""))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_wraps_unknown_error_in_agent_execution_error() -> None:
    """Unknown/permanent errors must be wrapped in AgentExecutionError."""
    from atelier.agent_executor import AgentExecutor, AgentExecutionError

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        raise ValueError("unexpected failure")
        yield  # make it a generator

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(profile=_make_profile(), soul_prompt="...", tools=[])
        with pytest.raises(AgentExecutionError) as exc_info:
            await executor.execute(_make_envelope("Hi"))

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


# ---------------------------------------------------------------------------
# Phase 5 — checkpointer= parameter (Phase 1 migration)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_executor_uses_memory_saver_by_default() -> None:
    """AgentExecutor without checkpointer= must use MemorySaver as fallback."""
    from atelier.agent_executor import AgentExecutor
    from langgraph.checkpoint.memory import MemorySaver

    mock_agent = MagicMock()

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent) as mock_create:
        AgentExecutor(
            profile=_make_profile(),
            soul_prompt="...",
            tools=[],
        )

    call_kwargs = mock_create.call_args.kwargs
    assert isinstance(call_kwargs.get("checkpointer"), MemorySaver)


@pytest.mark.unit
def test_executor_passes_explicit_checkpointer_to_create_deep_agent() -> None:
    """AgentExecutor must forward an explicit checkpointer to create_deep_agent."""
    from atelier.agent_executor import AgentExecutor
    from langgraph.checkpoint.memory import MemorySaver

    mock_agent = MagicMock()
    explicit_checkpointer = MemorySaver()

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent) as mock_create:
        AgentExecutor(
            profile=_make_profile(),
            soul_prompt="...",
            tools=[],
            checkpointer=explicit_checkpointer,
        )

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs.get("checkpointer") is explicit_checkpointer


# ---------------------------------------------------------------------------
# Loop guard — repeated consecutive tool errors raise AgentExecutionError
# ---------------------------------------------------------------------------


def _tool_error_token(tool_name: str, error_msg: str = "Error invoking tool") -> MagicMock:
    """Build a mock ToolMessage chunk with status='error'.

    Args:
        tool_name: The name of the failing tool.
        error_msg: The error content string.

    Returns:
        MagicMock resembling a LangChain ToolMessage with type='tool' and status='error'.
    """
    token = MagicMock()
    token.type = "tool"
    token.name = tool_name
    token.content = error_msg
    token.status = "error"
    token.tool_call_chunks = []
    return token


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_raises_on_repeated_consecutive_tool_errors() -> None:
    """execute() raises AgentExecutionError when the same tool errors 5 times in a row.

    This guards against infinite tool-error loops where the model keeps calling
    the same tool with broken arguments, exhausting max_turns with no progress.
    The loop guard must fire after 5 consecutive errors for the same tool name,
    before the stream would otherwise continue.
    """
    from atelier.agent_executor import AgentExecutor, AgentExecutionError

    failing_tool = "write_todos"

    async def fake_astream(input_data: dict, **kwargs) -> AsyncIterator:
        # Simulate the Mistral parallel-tool-call bug: write_todos errors 5 times (= limit)
        for _ in range(5):
            yield _v2_chunk("messages", (), (_tool_error_token(failing_tool), {}))
        # The 5th error should trigger the guard — this token should never be reached
        yield _v2_chunk("messages", (), (_ai_token("should not reach here"), {}))

    mock_agent = MagicMock()
    mock_agent.astream = fake_astream
    mock_agent.aget_state = AsyncMock(return_value=_make_agent_state())

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(),
            soul_prompt="You are helpful.",
            tools=[],
        )
        with pytest.raises(AgentExecutionError, match="write_todos"):
            await executor.execute(_make_envelope("Do something"))
