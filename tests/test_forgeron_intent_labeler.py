"""Tests for IntentLabeler — with_structured_output refactor.

TDD RED phase: these tests import IntentLabelLLMResponse (not yet defined)
and assert that label() uses with_structured_output instead of raw text parsing.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from forgeron.intent_labeler import IntentLabeler, IntentLabelLLMResponse
from common.profile_loader import ProfileConfig, ResilienceConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile() -> ProfileConfig:
    return ProfileConfig(
        model="test:test",
        temperature=0,
        max_tokens=100,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )


def _make_messages(user_texts: list[str]) -> list[dict]:
    return [{"type": "human", "content": t} for t in user_texts]


def _mock_structured_llm(response: IntentLabelLLMResponse) -> MagicMock:
    mock_structured = AsyncMock()
    mock_structured.ainvoke = AsyncMock(return_value=response)
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(return_value=mock_structured)
    return mock_llm


# ---------------------------------------------------------------------------
# Unit tests — label()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_returns_valid_snake_case_label():
    """Happy path: LLM returns a valid snake_case label."""
    labeler = IntentLabeler(_make_profile())
    mock_llm = _mock_structured_llm(IntentLabelLLMResponse(label="send_email"))

    with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
        result = await labeler.label(_make_messages(["please send an email to alice"]))

    assert result.label == "send_email"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_calls_with_structured_output():
    """label() must call llm.with_structured_output(IntentLabelLLMResponse)."""
    labeler = IntentLabeler(_make_profile())
    mock_llm = _mock_structured_llm(IntentLabelLLMResponse(label="send_email"))

    with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
        await labeler.label(_make_messages(["send email"]))

    mock_llm.with_structured_output.assert_called_once_with(IntentLabelLLMResponse)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_returns_none_for_excluded_labels():
    """label() returns None for labels in _EXCLUDED_LABELS."""
    labeler = IntentLabeler(_make_profile())

    for excluded in ["none", "unknown", "general", "chat", "conversation", "question"]:
        mock_llm = _mock_structured_llm(IntentLabelLLMResponse(label=excluded))
        with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
            result = await labeler.label(_make_messages(["hi there"]))
        assert result.label is None, f"Expected None for excluded label '{excluded}'"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_returns_none_for_invalid_format():
    """label() returns None when the label doesn't match _LABEL_RE."""
    labeler = IntentLabeler(_make_profile())

    for bad_label in ["Send-Email", "123start", "a", "has spaces"]:
        mock_llm = _mock_structured_llm(IntentLabelLLMResponse(label=bad_label))
        with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
            result = await labeler.label(_make_messages(["some user message"]))
        assert result.label is None, f"Expected None for invalid label '{bad_label}'"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_returns_none_when_no_user_messages():
    """label() returns None immediately if no user messages found."""
    labeler = IntentLabeler(_make_profile())
    # Only AI messages — no HumanMessage
    messages = [{"type": "ai", "content": "I can help with that"}]

    with patch("forgeron.intent_labeler.build_chat_model") as mock_build:
        result = await labeler.label(messages)

    assert result.label is None
    mock_build.assert_not_called()  # LLM should not be called at all


@pytest.mark.asyncio
@pytest.mark.unit
async def test_label_returns_none_on_llm_failure():
    """label() returns None if the LLM call raises an exception."""
    labeler = IntentLabeler(_make_profile())
    mock_structured = AsyncMock()
    mock_structured.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(return_value=mock_structured)

    with patch("forgeron.intent_labeler.build_chat_model", return_value=mock_llm):
        result = await labeler.label(_make_messages(["do something"]))

    assert result.label is None


# ---------------------------------------------------------------------------
# Unit tests — _extract_user_messages()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_user_messages_type_human():
    """Extracts messages with type='human'."""
    labeler = IntentLabeler(_make_profile())
    messages = [
        {"type": "human", "content": "hello"},
        {"type": "ai", "content": "hi"},
        {"type": "human", "content": "how are you"},
    ]
    result = labeler._extract_user_messages(messages)
    assert result == ["hello", "how are you"]


@pytest.mark.unit
def test_extract_user_messages_langchain_id_style():
    """Extracts messages using LangChain id=[..., 'HumanMessage'] style."""
    labeler = IntentLabeler(_make_profile())
    messages = [
        {"id": ["langchain", "schema", "messages", "HumanMessage"], "content": "query"},
        {"id": ["langchain", "schema", "messages", "AIMessage"], "content": "answer"},
    ]
    result = labeler._extract_user_messages(messages)
    assert result == ["query"]


@pytest.mark.unit
def test_extract_user_messages_truncates_to_300_chars():
    """Messages are truncated to 300 chars."""
    labeler = IntentLabeler(_make_profile())
    long_msg = "x" * 500
    result = labeler._extract_user_messages([{"type": "human", "content": long_msg}])
    assert len(result[0]) == 300


@pytest.mark.unit
def test_extract_user_messages_skips_empty():
    """Empty or whitespace-only messages are skipped."""
    labeler = IntentLabeler(_make_profile())
    messages = [
        {"type": "human", "content": ""},
        {"type": "human", "content": "   "},
        {"type": "human", "content": "real message"},
    ]
    result = labeler._extract_user_messages(messages)
    assert result == ["real message"]
