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
