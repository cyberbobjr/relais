"""Tests for IntentLabeler correction detection feature.

TDD RED phase: tests for is_correction, corrected_behavior, and
skill_name_hint fields added to IntentLabelLLMResponse and
IntentLabelResult.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from forgeron.intent_labeler import (
    IntentLabeler,
    IntentLabelLLMResponse,
    IntentLabelResult,
)
from common.profile_loader import ProfileConfig, ResilienceConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile() -> ProfileConfig:
    """Build a minimal ProfileConfig for testing."""
    return ProfileConfig(
        model="test:test",
        temperature=0,
        max_tokens=100,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )


def _make_messages(user_texts: list[str]) -> list[dict]:
    """Build a minimal serialized message list with human messages."""
    return [{"type": "human", "content": t} for t in user_texts]


def _mock_structured_llm(response: IntentLabelLLMResponse) -> MagicMock:
    """Create a mock LLM that returns the given structured response."""
    mock_structured = AsyncMock()
    mock_structured.ainvoke = AsyncMock(return_value=response)
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(return_value=mock_structured)
    return mock_llm


# ---------------------------------------------------------------------------
# Tests — correction detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_correction_detected():
    """When LLM returns is_correction=True, result has is_correction=True
    and corrected_behavior is populated."""
    labeler = IntentLabeler(_make_profile())
    llm_response = IntentLabelLLMResponse(
        label="none",
        is_correction=True,
        corrected_behavior="Use formal tone when replying",
        skill_name_hint=None,
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
        result = await labeler.label(_make_messages(["that's wrong, you should be more formal"]))

    assert isinstance(result, IntentLabelResult)
    assert result.is_correction is True
    assert result.corrected_behavior == "Use formal tone when replying"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_correction_not_detected():
    """Normal message returns is_correction=False."""
    labeler = IntentLabeler(_make_profile())
    llm_response = IntentLabelLLMResponse(
        label="send_email",
        is_correction=False,
        corrected_behavior=None,
        skill_name_hint=None,
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
        result = await labeler.label(_make_messages(["please send an email to alice"]))

    assert isinstance(result, IntentLabelResult)
    assert result.is_correction is False
    assert result.corrected_behavior is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_correction_with_valid_label():
    """A correction can also have a valid intent label (both fields populated)."""
    labeler = IntentLabeler(_make_profile())
    llm_response = IntentLabelLLMResponse(
        label="send_email",
        is_correction=True,
        corrected_behavior="Always CC the manager on emails",
        skill_name_hint="email_cc_policy",
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
        result = await labeler.label(_make_messages(["no, you should CC the manager too"]))

    assert isinstance(result, IntentLabelResult)
    assert result.label == "send_email"
    assert result.is_correction is True
    assert result.corrected_behavior == "Always CC the manager on emails"
    assert result.skill_name_hint == "email_cc_policy"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_correction_with_skill_name_hint():
    """The skill_name_hint is forwarded when present."""
    labeler = IntentLabeler(_make_profile())
    llm_response = IntentLabelLLMResponse(
        label="none",
        is_correction=True,
        corrected_behavior="Always greet in French",
        skill_name_hint="french_greeting",
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
        result = await labeler.label(_make_messages(["ce n'est pas correct, dis bonjour en francais"]))

    assert isinstance(result, IntentLabelResult)
    assert result.skill_name_hint == "french_greeting"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_none_but_correction_true():
    """Even when label is 'none' (excluded), is_correction=True is returned."""
    labeler = IntentLabeler(_make_profile())
    llm_response = IntentLabelLLMResponse(
        label="none",
        is_correction=True,
        corrected_behavior="Stop using emojis",
        skill_name_hint=None,
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
        result = await labeler.label(_make_messages(["that's wrong, stop using emojis"]))

    assert isinstance(result, IntentLabelResult)
    assert result.label is None  # "none" is excluded
    assert result.is_correction is True
    assert result.corrected_behavior == "Stop using emojis"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_backward_compat_label_only():
    """When is_correction=False and label is valid, behavior matches old IntentLabeler."""
    labeler = IntentLabeler(_make_profile())
    llm_response = IntentLabelLLMResponse(
        label="summarize_pdf",
        is_correction=False,
        corrected_behavior=None,
        skill_name_hint=None,
    )
    mock_llm = _mock_structured_llm(llm_response)

    with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
        result = await labeler.label(_make_messages(["summarize this PDF for me"]))

    assert isinstance(result, IntentLabelResult)
    assert result.label == "summarize_pdf"
    assert result.is_correction is False
    assert result.corrected_behavior is None
    assert result.skill_name_hint is None
