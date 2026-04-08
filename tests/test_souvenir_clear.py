"""Tests TDD — Souvenir action 'clear'.

Verifies that Souvenir correctly handles action="clear" on relais:memory:request
by clearing SQLite archived messages.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from common.envelope import Envelope
from common.contexts import CTX_SOUVENIR_REQUEST
from common.envelope_actions import ACTION_MEMORY_CLEAR
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
    """Helper: returns an xreadgroup result with an action=clear request.

    Builds an Envelope-format payload so tests match the Envelope-based parsing
    in souvenir.main._process_request_stream.
    """
    souvenir_ctx: dict = {"session_id": session_id}
    if user_id is not None:
        souvenir_ctx["user_id"] = user_id
    envelope = Envelope(
        content="",
        sender_id="atelier:test",
        channel="internal",
        session_id=session_id,
        correlation_id=correlation_id,
        action=ACTION_MEMORY_CLEAR,
        context={CTX_SOUVENIR_REQUEST: souvenir_ctx},
    )
    return [(b"relais:memory:request", [(b"1-1", {b"payload": envelope.to_json().encode()})])]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_souvenir_clear_calls_long_term_clear_session(mock_redis):
    """Action 'clear' must call long_term_store.clear_session(session_id)."""
    mock_redis.xreadgroup = AsyncMock(side_effect=[
        _make_clear_request("my_session"),
        asyncio.CancelledError(),
    ])

    souvenir = Souvenir()

    spec = souvenir.stream_specs()[0]
    shutdown_event = asyncio.Event()
    with patch.object(souvenir._long_term, "clear_session", new_callable=AsyncMock) as mock_lt_clear:
        try:
            await souvenir._run_stream_loop(spec, mock_redis, shutdown_event)
        except asyncio.CancelledError:
            pass
        mock_lt_clear.assert_called_once_with("my_session", user_id=None)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_souvenir_clear_passes_user_id_to_clear_session(mock_redis):
    """Action 'clear' must pass user_id to long_term_store.clear_session."""
    mock_redis.xreadgroup = AsyncMock(side_effect=[
        _make_clear_request("my_session", user_id="usr_admin"),
        asyncio.CancelledError(),
    ])

    souvenir = Souvenir()

    spec = souvenir.stream_specs()[0]
    shutdown_event = asyncio.Event()
    with patch.object(souvenir._long_term, "clear_session", new_callable=AsyncMock) as mock_lt_clear:
        try:
            await souvenir._run_stream_loop(spec, mock_redis, shutdown_event)
        except asyncio.CancelledError:
            pass
        mock_lt_clear.assert_called_once_with("my_session", user_id="usr_admin")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_souvenir_clear_acks_message(mock_redis):
    """Action 'clear' must ACK the message after processing."""
    mock_redis.xreadgroup = AsyncMock(side_effect=[
        _make_clear_request(),
        asyncio.CancelledError(),
    ])

    souvenir = Souvenir()

    spec = souvenir.stream_specs()[0]
    shutdown_event = asyncio.Event()
    with patch.object(souvenir._long_term, "clear_session", new_callable=AsyncMock):
        try:
            await souvenir._run_stream_loop(spec, mock_redis, shutdown_event)
        except asyncio.CancelledError:
            pass

    mock_redis.xack.assert_called_once()
