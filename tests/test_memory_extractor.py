"""Unit tests for souvenir.memory_extractor.MemoryExtractor."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(user_message: str = "bonjour", content: str = "salut") -> Envelope:
    """Return a minimal Envelope for testing.

    Args:
        user_message: Value stored in metadata["user_message"].
        content: Assistant reply content.

    Returns:
        A configured Envelope instance.
    """
    return Envelope(
        content=content,
        sender_id="user_test",
        channel="discord",
        session_id="sess_test",
        metadata={"user_message": user_message},
    )


def _mock_llm(content_str: str) -> MagicMock:
    """Return a mock LLM whose ainvoke returns an object with .content = content_str.

    Args:
        content_str: The string the mock LLM should return as response content.

    Returns:
        A MagicMock simulating the return value of init_chat_model(), with
        an ``ainvoke`` AsyncMock that returns an object carrying ``.content``.
    """
    response = MagicMock()
    response.content = content_str
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=response)
    return llm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_calls_llm_and_returns_facts() -> None:
    """extract() must call init_chat_model LLM and return extracted facts."""
    from souvenir.memory_extractor import MemoryExtractor

    facts = [{"fact": "likes cats", "category": "preference", "confidence": 0.9}]
    env = _make_envelope(user_message="j'adore les chats", content="c'est bien")

    mock_llm = _mock_llm(json.dumps(facts))

    with patch("souvenir.memory_extractor.init_chat_model", return_value=mock_llm):
        extractor = MemoryExtractor(model="anthropic:claude-haiku-4-5")
        result = await extractor.extract(env)

    assert len(result) == 1
    assert result[0]["fact"] == "likes cats"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_filters_low_confidence() -> None:
    """extract() must exclude facts whose confidence is <= 0.7."""
    from souvenir.memory_extractor import MemoryExtractor

    facts = [
        {"fact": "likes Python", "category": "preference", "confidence": 0.9},
        {"fact": "maybe vegetarian", "category": "diet", "confidence": 0.5},
        {"fact": "lives in Paris", "category": "location", "confidence": 0.8},
    ]
    env = _make_envelope()
    mock_llm = _mock_llm(json.dumps(facts))

    with patch("souvenir.memory_extractor.init_chat_model", return_value=mock_llm):
        extractor = MemoryExtractor(model="anthropic:claude-haiku-4-5")
        result = await extractor.extract(env)

    assert len(result) == 2
    facts_texts = {f["fact"] for f in result}
    assert "maybe vegetarian" not in facts_texts
    assert "likes Python" in facts_texts
    assert "lives in Paris" in facts_texts


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_handles_invalid_json_gracefully() -> None:
    """extract() must return [] if the LLM returns invalid JSON."""
    from souvenir.memory_extractor import MemoryExtractor

    env = _make_envelope()
    mock_llm = _mock_llm("not valid json {{{")

    with patch("souvenir.memory_extractor.init_chat_model", return_value=mock_llm):
        extractor = MemoryExtractor(model="anthropic:claude-haiku-4-5")
        result = await extractor.extract(env)

    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_handles_llm_error_gracefully() -> None:
    """extract() must return [] without raising if the LLM call raises."""
    from souvenir.memory_extractor import MemoryExtractor

    env = _make_envelope()

    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

    with patch("souvenir.memory_extractor.init_chat_model", return_value=llm):
        extractor = MemoryExtractor(model="anthropic:claude-haiku-4-5")
        result = await extractor.extract(env)

    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_uses_configured_model() -> None:
    """MemoryExtractor._model_name must reflect the model passed to __init__."""
    from souvenir.memory_extractor import MemoryExtractor

    with patch("souvenir.memory_extractor.init_chat_model") as mock_init:
        mock_init.return_value = _mock_llm("[]")
        extractor = MemoryExtractor(model="anthropic:claude-haiku-4-5")

    assert extractor._model_name == "anthropic:claude-haiku-4-5"
    mock_init.assert_called_once_with(
        "anthropic:claude-haiku-4-5", temperature=0.1, max_tokens=512
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_skips_empty_user_message() -> None:
    """extract() must return [] immediately if user_message is blank."""
    from souvenir.memory_extractor import MemoryExtractor

    env = _make_envelope(user_message="  ", content="quelque chose")

    with patch("souvenir.memory_extractor.init_chat_model") as mock_init:
        mock_llm_instance = _mock_llm("[]")
        mock_init.return_value = mock_llm_instance

        extractor = MemoryExtractor(model="anthropic:claude-haiku-4-5")
        result = await extractor.extract(env)

    # Should return early without making any LLM call
    mock_llm_instance.ainvoke.assert_not_called()
    assert result == []
