"""Tests for Phase 2 diagnostic injection in AgentExecutor.

Covers:
- format_diagnostic_trace() output structure
- inject_diagnostic_message() checkpointer integration
- _enrich_system_prompt() DIAGNOSTIC_AWARENESS_PROMPT inclusion
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(session_id: str = "sess1", sender_id: str = "usr_admin") -> MagicMock:
    env = MagicMock()
    env.session_id = session_id
    env.sender_id = sender_id
    env.correlation_id = "corr-test"
    env.context = {"portail": {"user_id": "usr_admin"}}
    return env


# ---------------------------------------------------------------------------
# format_diagnostic_trace
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_diagnostic_trace_contains_error_and_counts() -> None:
    """format_diagnostic_trace() includes exception text and counters."""
    from atelier.agent_executor import format_diagnostic_trace

    result = format_diagnostic_trace(
        error="AgentExecutionError: max total errors",
        messages_raw=[],
        tool_call_count=8,
        tool_error_count=5,
    )
    assert "[DIAGNOSTIC — internal]" in result
    assert "max total errors" in result
    assert "8 total" in result
    assert "5 errors" in result


@pytest.mark.unit
def test_format_diagnostic_trace_lists_failing_tools() -> None:
    """format_diagnostic_trace() includes names of tools that failed."""
    from atelier.agent_executor import format_diagnostic_trace

    messages_raw = [
        {"role": "tool", "name": "himalaya", "content": "Error: connection refused"},
        {"role": "tool", "name": "read_file", "content": "file.txt contents"},
    ]
    result = format_diagnostic_trace(
        error="AgentExecutionError",
        messages_raw=messages_raw,
        tool_call_count=3,
        tool_error_count=1,
    )
    assert "himalaya" in result
    assert "read_file" not in result  # non-error tool must be absent


@pytest.mark.unit
def test_format_diagnostic_trace_respects_max_chars() -> None:
    """format_diagnostic_trace() truncates output at max_chars."""
    from atelier.agent_executor import format_diagnostic_trace

    result = format_diagnostic_trace(
        error="x" * 2000,
        messages_raw=[],
        tool_call_count=0,
        tool_error_count=0,
        max_chars=100,
    )
    assert len(result) <= 100


@pytest.mark.unit
def test_format_diagnostic_trace_default_max_chars() -> None:
    """format_diagnostic_trace() default cap is 2000 chars."""
    from atelier.agent_executor import format_diagnostic_trace, _DIAGNOSTIC_MAX_CHARS

    assert _DIAGNOSTIC_MAX_CHARS == 2000
    messages_raw = [
        {"role": "tool", "name": f"t{i}", "content": "Error: " + "y" * 400}
        for i in range(5)
    ]
    result = format_diagnostic_trace(
        error="AgentExecutionError",
        messages_raw=messages_raw,
        tool_call_count=5,
        tool_error_count=5,
    )
    assert len(result) <= _DIAGNOSTIC_MAX_CHARS


# ---------------------------------------------------------------------------
# inject_diagnostic_message
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inject_diagnostic_message_calls_aupdate_state() -> None:
    """inject_diagnostic_message() calls aupdate_state with an AIMessage."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()
    mock_state = MagicMock()
    mock_state.values = {"messages": [MagicMock()]}
    mock_agent.aget_state = AsyncMock(return_value=mock_state)
    mock_agent.aupdate_state = AsyncMock(return_value=None)

    executor = object.__new__(AgentExecutor)
    executor._agent = mock_agent

    envelope = _make_envelope()
    result = await executor.inject_diagnostic_message(envelope, "[DIAGNOSTIC — internal]\nError: boom")

    assert result is True
    mock_agent.aupdate_state.assert_called_once()
    call_kwargs = mock_agent.aupdate_state.call_args
    messages_arg = call_kwargs[0][1]["messages"]
    assert len(messages_arg) == 1
    assert "[DIAGNOSTIC — internal]" in messages_arg[0].content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inject_diagnostic_message_returns_false_on_empty_state() -> None:
    """inject_diagnostic_message() returns False when state is empty."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()
    mock_state = MagicMock()
    mock_state.values = {}
    mock_agent.aget_state = AsyncMock(return_value=mock_state)

    executor = object.__new__(AgentExecutor)
    executor._agent = mock_agent

    result = await executor.inject_diagnostic_message(_make_envelope(), "[DIAGNOSTIC — internal]\ntest")
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inject_diagnostic_message_returns_false_on_empty_text() -> None:
    """inject_diagnostic_message() returns False for empty diagnostic text."""
    from atelier.agent_executor import AgentExecutor

    executor = object.__new__(AgentExecutor)
    executor._agent = MagicMock()

    result = await executor.inject_diagnostic_message(_make_envelope(), "   ")
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inject_diagnostic_message_returns_false_on_exception() -> None:
    """inject_diagnostic_message() returns False and logs when aupdate_state raises."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()
    mock_state = MagicMock()
    mock_state.values = {"messages": [MagicMock()]}
    mock_agent.aget_state = AsyncMock(return_value=mock_state)
    mock_agent.aupdate_state = AsyncMock(side_effect=RuntimeError("checkpointer down"))

    executor = object.__new__(AgentExecutor)
    executor._agent = mock_agent

    result = await executor.inject_diagnostic_message(_make_envelope(), "[DIAGNOSTIC — internal]\ntest")
    assert result is False


# ---------------------------------------------------------------------------
# _enrich_system_prompt includes DIAGNOSTIC_AWARENESS_PROMPT
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_system_prompt_includes_diagnostic_awareness() -> None:
    """_enrich_system_prompt() appends DIAGNOSTIC_AWARENESS_PROMPT."""
    from atelier.agent_executor import _enrich_system_prompt, DIAGNOSTIC_AWARENESS_PROMPT

    result = _enrich_system_prompt("base soul prompt")
    assert DIAGNOSTIC_AWARENESS_PROMPT in result


@pytest.mark.unit
def test_enrich_system_prompt_no_duplicate_diagnostic() -> None:
    """_enrich_system_prompt() does not duplicate DIAGNOSTIC_AWARENESS_PROMPT."""
    from atelier.agent_executor import _enrich_system_prompt, DIAGNOSTIC_AWARENESS_PROMPT

    base = "soul\n\n" + DIAGNOSTIC_AWARENESS_PROMPT
    result = _enrich_system_prompt(base)
    assert result.count("[DIAGNOSTIC — internal]") == 1
