"""Unit tests for souvenir.handlers — each handler tested in isolation via HandlerContext."""

import json
import pytest
from unittest.mock import AsyncMock

from souvenir.handlers import HandlerContext, build_registry
from souvenir.handlers.clear_handler import ClearHandler
from souvenir.handlers.file_list_handler import FileListHandler
from souvenir.handlers.file_read_handler import FileReadHandler
from souvenir.handlers.file_write_handler import FileWriteHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(req: dict, mock_redis: AsyncMock, long_term_store=None, file_store=None) -> HandlerContext:
    """Build a HandlerContext with sensible defaults for testing."""
    if long_term_store is None:
        long_term_store = AsyncMock()
    if file_store is None:
        file_store = AsyncMock()
    return HandlerContext(
        redis_conn=mock_redis,
        long_term_store=long_term_store,
        file_store=file_store,
        req=req,
        stream_res="relais:memory:response",
    )


# ---------------------------------------------------------------------------
# ClearHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_handler_clears_long_term_store() -> None:
    """ClearHandler clears the long_term_store (SQLite)."""
    mock_redis = AsyncMock()
    long_term_store = AsyncMock()
    long_term_store.clear_session = AsyncMock()

    ctx = _make_ctx(
        req={"session_id": "sess-a", "correlation_id": "c1"},
        mock_redis=mock_redis,
        long_term_store=long_term_store,
    )

    await ClearHandler().handle(ctx)

    long_term_store.clear_session.assert_awaited_once_with("sess-a", user_id=None)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_handler_sends_confirmation_envelope() -> None:
    """ClearHandler publishes a confirmation envelope when envelope_json is present."""
    from common.envelope import Envelope
    from common.envelope_actions import ACTION_MEMORY_CLEAR

    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()
    long_term_store = AsyncMock()
    long_term_store.clear_session = AsyncMock()

    orig = Envelope(
        content="/clear",
        sender_id="discord:123",
        channel="discord",
        session_id="sess-b",
        correlation_id="corr-b",
        action=ACTION_MEMORY_CLEAR,
    )

    ctx = _make_ctx(
        req={"session_id": "sess-b", "envelope_json": orig.to_json()},
        mock_redis=mock_redis,
        long_term_store=long_term_store,
    )

    await ClearHandler().handle(ctx)

    mock_redis.xadd.assert_awaited_once()
    stream = mock_redis.xadd.call_args[0][0]
    assert stream == "relais:messages:outgoing:discord"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_handler_handles_missing_envelope_json() -> None:
    """ClearHandler must not raise when envelope_json is absent."""
    mock_redis = AsyncMock()
    long_term_store = AsyncMock()
    long_term_store.clear_session = AsyncMock()

    ctx = _make_ctx(
        req={"session_id": "sess-c"},
        mock_redis=mock_redis,
        long_term_store=long_term_store,
    )

    await ClearHandler().handle(ctx)

    mock_redis.xadd.assert_not_awaited()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_build_registry_contains_all_actions() -> None:
    """build_registry() must return the 5 expected actions."""
    from common.envelope_actions import (
        ACTION_MEMORY_ARCHIVE,
        ACTION_MEMORY_CLEAR,
        ACTION_MEMORY_FILE_LIST,
        ACTION_MEMORY_FILE_READ,
        ACTION_MEMORY_FILE_WRITE,
    )

    registry = build_registry()
    assert set(registry.keys()) == {
        ACTION_MEMORY_ARCHIVE,
        ACTION_MEMORY_CLEAR,
        ACTION_MEMORY_FILE_WRITE,
        ACTION_MEMORY_FILE_READ,
        ACTION_MEMORY_FILE_LIST,
    }
    assert isinstance(registry[ACTION_MEMORY_CLEAR], ClearHandler)
    assert isinstance(registry[ACTION_MEMORY_FILE_WRITE], FileWriteHandler)
    assert isinstance(registry[ACTION_MEMORY_FILE_READ], FileReadHandler)
    assert isinstance(registry[ACTION_MEMORY_FILE_LIST], FileListHandler)
