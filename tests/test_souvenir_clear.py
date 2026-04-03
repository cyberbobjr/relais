"""Tests TDD — Souvenir action 'clear'.

Verifies that Souvenir correctly handles action="clear" on relais:memory:request
by clearing SQLite archived messages.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from souvenir.main import Souvenir


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xadd = AsyncMock()
    redis.xack = AsyncMock()
    redis.delete = AsyncMock()
    return redis


def _make_clear_request(
    session_id: str = "s1",
    correlation_id: str = "c1",
    user_id: str | None = None,
) -> list:
    """Helper: retourne un résultat xreadgroup avec une requête action=clear."""
    payload: dict = {
        "action": "clear",
        "session_id": session_id,
        "correlation_id": correlation_id,
    }
    if user_id is not None:
        payload["user_id"] = user_id
    return [(b"relais:memory:request", [(b"1-1", {b"payload": json.dumps(payload).encode()})])]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_souvenir_clear_calls_long_term_clear_session(mock_redis):
    """Action 'clear' appelle long_term_store.clear_session(session_id)."""
    mock_redis.xreadgroup = AsyncMock(return_value=_make_clear_request("my_session"))

    souvenir = Souvenir()

    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    with patch.object(souvenir._long_term, "clear_session", new_callable=AsyncMock) as mock_lt_clear:
        await souvenir._process_request_stream(mock_redis, shutdown=shutdown)
        mock_lt_clear.assert_called_once_with("my_session", user_id=None)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_souvenir_clear_passes_user_id_to_clear_session(mock_redis):
    """Action 'clear' transmet user_id à long_term_store.clear_session."""
    mock_redis.xreadgroup = AsyncMock(
        return_value=_make_clear_request("my_session", user_id="usr_admin")
    )

    souvenir = Souvenir()

    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    with patch.object(souvenir._long_term, "clear_session", new_callable=AsyncMock) as mock_lt_clear:
        await souvenir._process_request_stream(mock_redis, shutdown=shutdown)
        mock_lt_clear.assert_called_once_with("my_session", user_id="usr_admin")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_souvenir_clear_acks_message(mock_redis):
    """Action 'clear' ACK le message après traitement."""
    mock_redis.xreadgroup = AsyncMock(return_value=_make_clear_request())

    souvenir = Souvenir()

    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]

    with patch.object(souvenir._long_term, "clear_session", new_callable=AsyncMock):
        await souvenir._process_request_stream(mock_redis, shutdown=shutdown)

    mock_redis.xack.assert_called_once()
