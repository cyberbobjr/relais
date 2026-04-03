"""Unit tests for souvenir.handlers — each handler tested in isolation via HandlerContext."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from souvenir.context_store import ContextStore
from souvenir.handlers import HandlerContext, build_registry
from souvenir.handlers.clear_handler import ClearHandler
from souvenir.handlers.file_list_handler import FileListHandler
from souvenir.handlers.file_read_handler import FileReadHandler
from souvenir.handlers.file_write_handler import FileWriteHandler
from souvenir.handlers.get_handler import GetHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(req: dict, mock_redis: AsyncMock, context_store=None, long_term_store=None, file_store=None) -> HandlerContext:
    """Build a HandlerContext with sensible defaults for testing."""
    if context_store is None:
        context_store = AsyncMock(spec=ContextStore)
    if long_term_store is None:
        long_term_store = AsyncMock()
    if file_store is None:
        file_store = AsyncMock()
    return HandlerContext(
        redis_conn=mock_redis,
        context_store=context_store,
        long_term_store=long_term_store,
        file_store=file_store,
        req=req,
        stream_res="relais:memory:response",
    )


# ---------------------------------------------------------------------------
# GetHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_handler_returns_redis_cache() -> None:
    """GetHandler publie le cache Redis sur stream_res via XADD."""
    cached_turns = [
        json.dumps({"role": "user", "content": "hello"}).encode(),
        json.dumps({"role": "assistant", "content": "hi"}).encode(),
    ]
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()
    mock_redis.xtrim = AsyncMock()

    context_store = ContextStore(redis=mock_redis)
    mock_redis.lrange = AsyncMock(return_value=cached_turns)

    long_term_store = AsyncMock()
    long_term_store.get_recent_messages = AsyncMock(return_value=[])

    ctx = _make_ctx(
        req={"session_id": "sess-x", "correlation_id": "corr-1"},
        mock_redis=mock_redis,
        context_store=context_store,
        long_term_store=long_term_store,
    )

    await GetHandler().handle(ctx)

    mock_redis.xadd.assert_awaited_once()
    xadd_call = mock_redis.xadd.call_args
    stream = xadd_call[0][0]
    payload = json.loads(xadd_call[0][1]["payload"])
    assert stream == "relais:memory:response"
    assert payload["correlation_id"] == "corr-1"
    assert len(payload["messages"]) == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_handler_falls_back_to_sqlite_when_cache_empty() -> None:
    """GetHandler utilise SQLite quand le cache Redis est vide."""
    mock_redis = AsyncMock()
    mock_redis.lrange = AsyncMock(return_value=[])
    mock_redis.xadd = AsyncMock()
    mock_redis.xtrim = AsyncMock()

    sqlite_messages = [
        {"role": "user", "content": "from sqlite"},
        {"role": "assistant", "content": "sqlite reply"},
    ]
    context_store = ContextStore(redis=mock_redis)
    long_term_store = AsyncMock()
    long_term_store.get_recent_messages = AsyncMock(return_value=sqlite_messages)

    ctx = _make_ctx(
        req={"session_id": "sess-y", "correlation_id": "corr-2"},
        mock_redis=mock_redis,
        context_store=context_store,
        long_term_store=long_term_store,
    )

    await GetHandler().handle(ctx)

    long_term_store.get_recent_messages.assert_awaited_once_with("sess-y", limit=20)
    xadd_payload = json.loads(mock_redis.xadd.call_args[0][1]["payload"])
    assert len(xadd_payload["messages"]) == 2
    assert xadd_payload["messages"][0]["content"] == "from sqlite"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_handler_trims_response_stream() -> None:
    """GetHandler appelle xtrim sur stream_res après xadd."""
    mock_redis = AsyncMock()
    mock_redis.lrange = AsyncMock(return_value=[])
    mock_redis.xadd = AsyncMock()
    mock_redis.xtrim = AsyncMock()

    context_store = ContextStore(redis=mock_redis)
    long_term_store = AsyncMock()
    long_term_store.get_recent_messages = AsyncMock(return_value=[])

    ctx = _make_ctx(
        req={"session_id": "sess-z", "correlation_id": "corr-3"},
        mock_redis=mock_redis,
        context_store=context_store,
        long_term_store=long_term_store,
    )

    await GetHandler().handle(ctx)

    mock_redis.xtrim.assert_awaited_once_with("relais:memory:response", maxlen=500, approximate=True)


# ---------------------------------------------------------------------------
# ClearHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_handler_clears_both_stores() -> None:
    """ClearHandler vide le context_store ET le long_term_store."""
    mock_redis = AsyncMock()
    context_store = AsyncMock(spec=ContextStore)
    context_store.clear = AsyncMock()
    long_term_store = AsyncMock()
    long_term_store.clear_session = AsyncMock()

    ctx = _make_ctx(
        req={"session_id": "sess-a", "correlation_id": "c1"},
        mock_redis=mock_redis,
        context_store=context_store,
        long_term_store=long_term_store,
    )

    await ClearHandler().handle(ctx)

    context_store.clear.assert_awaited_once_with("sess-a")
    long_term_store.clear_session.assert_awaited_once_with("sess-a")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_handler_sends_confirmation_envelope() -> None:
    """ClearHandler publie une enveloppe de confirmation quand envelope_json est présent."""
    from common.envelope import Envelope

    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()
    context_store = AsyncMock(spec=ContextStore)
    context_store.clear = AsyncMock()
    long_term_store = AsyncMock()
    long_term_store.clear_session = AsyncMock()

    orig = Envelope(
        content="/clear",
        sender_id="discord:123",
        channel="discord",
        session_id="sess-b",
        correlation_id="corr-b",
    )

    ctx = _make_ctx(
        req={"session_id": "sess-b", "envelope_json": orig.to_json()},
        mock_redis=mock_redis,
        context_store=context_store,
        long_term_store=long_term_store,
    )

    await ClearHandler().handle(ctx)

    mock_redis.xadd.assert_awaited_once()
    stream = mock_redis.xadd.call_args[0][0]
    assert stream == "relais:messages:outgoing:discord"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_handler_handles_missing_envelope_json() -> None:
    """ClearHandler ne lève pas d'exception quand envelope_json est absent."""
    mock_redis = AsyncMock()
    context_store = AsyncMock(spec=ContextStore)
    context_store.clear = AsyncMock()
    long_term_store = AsyncMock()
    long_term_store.clear_session = AsyncMock()

    ctx = _make_ctx(
        req={"session_id": "sess-c"},
        mock_redis=mock_redis,
        context_store=context_store,
        long_term_store=long_term_store,
    )

    await ClearHandler().handle(ctx)

    mock_redis.xadd.assert_not_awaited()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_build_registry_contains_all_actions() -> None:
    """build_registry() retourne les 5 actions attendues."""
    registry = build_registry()
    assert set(registry.keys()) == {"get", "clear", "file_write", "file_read", "file_list"}
    assert isinstance(registry["get"], GetHandler)
    assert isinstance(registry["clear"], ClearHandler)
    assert isinstance(registry["file_write"], FileWriteHandler)
    assert isinstance(registry["file_read"], FileReadHandler)
    assert isinstance(registry["file_list"], FileListHandler)
