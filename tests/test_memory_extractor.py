"""Unit tests for souvenir.memory_extractor.MemoryExtractor."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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


def _mock_httpx_response(facts: list[dict]) -> MagicMock:
    """Return a mock httpx Response whose JSON contains a choices list.

    Args:
        facts: List of fact dicts that the LLM would return.

    Returns:
        MagicMock simulating httpx.Response.
    """
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [
            {"message": {"content": json.dumps(facts)}}
        ]
    }
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_calls_litellm_proxy() -> None:
    """extract() doit appeler le proxy LiteLLM et retourner les faits extraits."""
    from souvenir.memory_extractor import MemoryExtractor

    facts = [{"fact": "likes cats", "category": "preference", "confidence": 0.9}]
    env = _make_envelope(user_message="j'adore les chats", content="c'est bien")

    mock_resp = _mock_httpx_response(facts)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        extractor = MemoryExtractor(litellm_url="http://localhost:4000", model="gpt-3.5-turbo")
        result = await extractor.extract(env)

    assert len(result) == 1
    assert result[0]["fact"] == "likes cats"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_filters_low_confidence() -> None:
    """extract() doit exclure les faits dont la confidence est <= 0.7."""
    from souvenir.memory_extractor import MemoryExtractor

    facts = [
        {"fact": "likes Python", "category": "preference", "confidence": 0.9},
        {"fact": "maybe vegetarian", "category": "diet", "confidence": 0.5},
        {"fact": "lives in Paris", "category": "location", "confidence": 0.8},
    ]
    env = _make_envelope()
    mock_resp = _mock_httpx_response(facts)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        extractor = MemoryExtractor(litellm_url="http://localhost:4000", model="gpt-3.5-turbo")
        result = await extractor.extract(env)

    assert len(result) == 2
    facts_texts = {f["fact"] for f in result}
    assert "maybe vegetarian" not in facts_texts
    assert "likes Python" in facts_texts
    assert "lives in Paris" in facts_texts


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_handles_invalid_json_gracefully() -> None:
    """extract() doit retourner [] si le LLM retourne un JSON invalide."""
    from souvenir.memory_extractor import MemoryExtractor

    env = _make_envelope()

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "not valid json {{{"}}]
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        extractor = MemoryExtractor(litellm_url="http://localhost:4000", model="gpt-3.5-turbo")
        result = await extractor.extract(env)

    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_handles_http_error_gracefully() -> None:
    """extract() doit retourner [] sans lever d'exception si HTTP échoue."""
    from souvenir.memory_extractor import MemoryExtractor

    env = _make_envelope()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        mock_client_cls.return_value = mock_client

        extractor = MemoryExtractor(litellm_url="http://localhost:4000", model="gpt-3.5-turbo")
        result = await extractor.extract(env)

    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_uses_fast_profile() -> None:
    """extract() doit envoyer le modèle configuré dans la requête HTTP."""
    from souvenir.memory_extractor import MemoryExtractor

    env = _make_envelope()
    mock_resp = _mock_httpx_response([])

    captured_payload: dict = {}

    async def _capture_post(url: str, **kwargs: object) -> MagicMock:
        captured_payload.update(kwargs.get("json", {}))
        return mock_resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _capture_post
        mock_client_cls.return_value = mock_client

        extractor = MemoryExtractor(litellm_url="http://localhost:4000", model="fast-model")
        await extractor.extract(env)

    assert captured_payload.get("model") == "fast-model"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_extract_skips_empty_user_message() -> None:
    """extract() doit retourner [] immédiatement si user_message est vide."""
    from souvenir.memory_extractor import MemoryExtractor

    env = _make_envelope(user_message="  ", content="quelque chose")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client

        extractor = MemoryExtractor(litellm_url="http://localhost:4000")
        result = await extractor.extract(env)

    # Should return early without making any HTTP call
    mock_client.post.assert_not_called()
    assert result == []
