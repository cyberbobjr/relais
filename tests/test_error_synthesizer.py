"""Tests for atelier.error_synthesizer.ErrorSynthesizer.

These tests are written BEFORE the implementation (TDD RED phase).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from common.profile_loader import ProfileConfig, ResilienceConfig


def _make_profile() -> ProfileConfig:
    return ProfileConfig(
        model="test:test",
        temperature=0,
        max_tokens=100,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_synthesize_returns_string() -> None:
    """synthesize() returns a non-empty string."""
    from atelier.error_synthesizer import ErrorSynthesizer

    messages_raw = [
        {"role": "user", "content": "send an email"},
        {"role": "assistant", "content": "Calling himalaya..."},
    ]

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="Sorry, I could not send the email."))

    with patch("atelier.error_synthesizer.init_chat_model", return_value=mock_llm):
        synth = ErrorSynthesizer()
        result = await synth.synthesize(
            messages_raw=messages_raw,
            error="AgentExecutionError: tool loop",
            profile=_make_profile(),
        )

    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_synthesize_calls_llm_with_messages_raw() -> None:
    """synthesize() passes message history to the LLM."""
    from atelier.error_synthesizer import ErrorSynthesizer

    messages_raw = [
        {"role": "user", "content": "forward the report to bob@example.com"},
        {"role": "assistant", "content": "I'll use himalaya to send it."},
    ]

    captured_calls: list = []
    mock_response = MagicMock(content="I was unable to forward the email.")

    async def fake_ainvoke(messages, **kwargs):
        captured_calls.append(messages)
        return mock_response

    mock_llm = AsyncMock()
    mock_llm.ainvoke = fake_ainvoke

    with patch("atelier.error_synthesizer.init_chat_model", return_value=mock_llm):
        synth = ErrorSynthesizer()
        await synth.synthesize(
            messages_raw=messages_raw,
            error="Tool loop exceeded",
            profile=_make_profile(),
        )

    assert len(captured_calls) == 1
    # The LLM must have received a prompt that includes some reference to the
    # original conversation (user content visible in the call args)
    prompt_str = str(captured_calls[0])
    assert "forward the report" in prompt_str or "bob@example.com" in prompt_str


@pytest.mark.unit
@pytest.mark.asyncio
async def test_synthesize_uses_profile_model() -> None:
    """synthesize() initialises the LLM from the provided profile's model field."""
    from atelier.error_synthesizer import ErrorSynthesizer

    profile = _make_profile()
    init_calls: list = []

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=MagicMock(content="Could not complete the request.")
    )

    def fake_init_chat_model(model: str, **kwargs):
        init_calls.append(model)
        return mock_llm

    with patch("atelier.error_synthesizer.init_chat_model", side_effect=fake_init_chat_model):
        synth = ErrorSynthesizer()
        await synth.synthesize(
            messages_raw=[],
            error="some error",
            profile=profile,
        )

    assert len(init_calls) == 1
    assert init_calls[0] == profile.model


@pytest.mark.unit
@pytest.mark.asyncio
async def test_synthesize_fallback_on_llm_error() -> None:
    """synthesize() returns a safe fallback string if the LLM call fails."""
    from atelier.error_synthesizer import ErrorSynthesizer

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM unreachable"))

    with patch("atelier.error_synthesizer.init_chat_model", return_value=mock_llm):
        synth = ErrorSynthesizer()
        result = await synth.synthesize(
            messages_raw=[{"role": "user", "content": "do something"}],
            error="AgentExecutionError",
            profile=_make_profile(),
        )

    assert isinstance(result, str)
    assert len(result) > 0  # fallback must not be empty


@pytest.mark.unit
@pytest.mark.asyncio
async def test_synthesize_empty_messages_raw() -> None:
    """synthesize() works even when messages_raw is empty."""
    from atelier.error_synthesizer import ErrorSynthesizer

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=MagicMock(content="I encountered an unexpected error.")
    )

    with patch("atelier.error_synthesizer.init_chat_model", return_value=mock_llm):
        synth = ErrorSynthesizer()
        result = await synth.synthesize(
            messages_raw=[],
            error="AgentExecutionError: unknown",
            profile=_make_profile(),
        )

    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# Tests for extract_tool_errors (Option A)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_tool_errors_detects_error_marker() -> None:
    """extract_tool_errors() returns tool messages containing error markers."""
    from atelier.error_synthesizer import extract_tool_errors

    messages_raw = [
        {"role": "tool", "name": "execute", "content": "[Command failed with exit code 1]\nls: no such file"},
        {"role": "tool", "name": "read_file", "content": "contents of the file"},
        {"role": "tool", "name": "write_file", "content": "Error: permission denied"},
    ]
    result = extract_tool_errors(messages_raw)
    assert len(result) == 2
    assert result[0]["tool_name"] == "execute"
    assert result[1]["tool_name"] == "write_file"


@pytest.mark.unit
def test_extract_tool_errors_empty_for_success() -> None:
    """extract_tool_errors() returns empty list when no errors are present."""
    from atelier.error_synthesizer import extract_tool_errors

    messages_raw = [
        {"role": "tool", "name": "execute", "content": "file1.txt\nfile2.txt"},
        {"role": "user", "content": "list files"},
    ]
    result = extract_tool_errors(messages_raw)
    assert result == []


@pytest.mark.unit
def test_extract_tool_errors_caps_at_five() -> None:
    """extract_tool_errors() returns at most 5 entries."""
    from atelier.error_synthesizer import extract_tool_errors

    messages_raw = [
        {"role": "tool", "name": f"tool_{i}", "content": "Error: something"}
        for i in range(10)
    ]
    result = extract_tool_errors(messages_raw)
    assert len(result) == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_synthesize_includes_error_string_in_prompt() -> None:
    """synthesize() injects the error string into the LLM system prompt."""
    from atelier.error_synthesizer import ErrorSynthesizer

    captured: list = []
    mock_llm = AsyncMock()

    async def fake_ainvoke(messages, **kwargs):
        captured.append(messages)
        return MagicMock(content="Sorry about that.")

    mock_llm.ainvoke = fake_ainvoke

    with patch("atelier.error_synthesizer.init_chat_model", return_value=mock_llm):
        synth = ErrorSynthesizer()
        await synth.synthesize(
            messages_raw=[],
            error="AgentExecutionError: max tool errors exceeded",
            profile=_make_profile(),
        )

    system_content = str(captured[0][0].content)
    assert "max tool errors exceeded" in system_content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_synthesize_includes_tool_errors_in_prompt() -> None:
    """synthesize() includes failing tool names in the LLM system prompt."""
    from atelier.error_synthesizer import ErrorSynthesizer

    captured: list = []
    mock_llm = AsyncMock()

    async def fake_ainvoke(messages, **kwargs):
        captured.append(messages)
        return MagicMock(content="Sorry.")

    mock_llm.ainvoke = fake_ainvoke

    messages_raw = [
        {"role": "tool", "name": "himalaya", "content": "Error: SMTP connection refused"},
    ]

    with patch("atelier.error_synthesizer.init_chat_model", return_value=mock_llm):
        synth = ErrorSynthesizer()
        await synth.synthesize(
            messages_raw=messages_raw,
            error="AgentExecutionError",
            profile=_make_profile(),
        )

    system_content = str(captured[0][0].content)
    assert "himalaya" in system_content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_synthesize_truncates_tool_error_content() -> None:
    """synthesize() truncates tool error content to _MAX_TOOL_ERROR_PREVIEW chars."""
    from atelier.error_synthesizer import ErrorSynthesizer, _MAX_TOOL_ERROR_PREVIEW

    captured: list = []
    mock_llm = AsyncMock()

    async def fake_ainvoke(messages, **kwargs):
        captured.append(messages)
        return MagicMock(content="Sorry.")

    mock_llm.ainvoke = fake_ainvoke

    long_content = "Error: " + "x" * 1000
    messages_raw = [
        {"role": "tool", "name": "big_tool", "content": long_content},
    ]

    with patch("atelier.error_synthesizer.init_chat_model", return_value=mock_llm):
        synth = ErrorSynthesizer()
        await synth.synthesize(
            messages_raw=messages_raw,
            error="AgentExecutionError",
            profile=_make_profile(),
        )

    system_content = str(captured[0][0].content)
    assert "x" * (_MAX_TOOL_ERROR_PREVIEW + 1) not in system_content
