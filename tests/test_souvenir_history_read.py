"""Tests for HistoryReadHandler — memory.history_read action.

Validates that Souvenir can serve full conversation history (messages_raw)
for a session, with token-based truncation and TTL on the response key.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from common.envelope_actions import ACTION_MEMORY_HISTORY_READ
from souvenir.handlers.base import HandlerContext
from souvenir.handlers.history_read_handler import HistoryReadHandler
from souvenir.long_term_store import LongTermStore


@pytest_asyncio.fixture
async def long_term_store(tmp_path: Path) -> LongTermStore:
    """Create an in-memory LongTermStore with tables initialised."""
    store = LongTermStore(db_path=tmp_path / "test_memory.db")
    await store._create_tables()
    yield store
    await store.close()


@pytest.fixture
def redis_mock() -> AsyncMock:
    """Return an AsyncMock simulating an async Redis connection."""
    mock = AsyncMock()
    mock.lpush = AsyncMock()
    mock.expire = AsyncMock()
    return mock


async def _insert_turn(
    store: LongTermStore,
    session_id: str,
    correlation_id: str,
    messages_raw: list[dict],
    created_at: float = 0.0,
) -> None:
    """Insert a turn directly into the archived_messages table.

    Args:
        store: The LongTermStore instance.
        session_id: Session identifier.
        correlation_id: Unique correlation ID for the turn.
        messages_raw: The raw message list to archive.
        created_at: Timestamp for ordering.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from souvenir.models import ArchivedMessage

    stmt = sqlite_insert(ArchivedMessage).values(
        session_id=session_id,
        sender_id="test:user1",
        channel="test",
        user_content="hello",
        assistant_content="world",
        messages_raw=json.dumps(messages_raw),
        correlation_id=correlation_id,
        created_at=created_at,
    )
    async with store._session_factory() as session:
        await session.execute(stmt)
        await session.commit()


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_read_returns_messages_raw(
    long_term_store: LongTermStore, redis_mock: AsyncMock
) -> None:
    """HistoryReadHandler queries LongTermStore and writes messages_raw to Redis."""
    turn1 = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}]
    turn2 = [{"role": "user", "content": "Bye"}, {"role": "assistant", "content": "See ya"}]

    await _insert_turn(long_term_store, "sess-1", "corr-1", turn1, created_at=1.0)
    await _insert_turn(long_term_store, "sess-1", "corr-2", turn2, created_at=2.0)

    ctx = HandlerContext(
        redis_conn=redis_mock,
        long_term_store=long_term_store,
        file_store=AsyncMock(),
        req={
            "session_id": "sess-1",
            "max_tokens": 64000,
            "correlation_id": "req-corr-1",
        },
        stream_res="relais:memory:response",
    )

    handler = HistoryReadHandler()
    await handler.handle(ctx)

    # Should LPUSH the result as JSON to the response key
    response_key = "relais:memory:response:req-corr-1"
    redis_mock.lpush.assert_called_once()
    call_args = redis_mock.lpush.call_args
    assert call_args[0][0] == response_key

    result = json.loads(call_args[0][1])
    assert len(result) == 2
    assert result[0] == turn1
    assert result[1] == turn2


@pytest.mark.asyncio
async def test_history_read_truncates_to_max_tokens(
    long_term_store: LongTermStore, redis_mock: AsyncMock
) -> None:
    """When messages exceed max_tokens (~4 chars/token), oldest turns are dropped."""
    # Create a short turn and a long turn
    short_turn = [{"role": "user", "content": "Hi"}]
    long_turn = [{"role": "assistant", "content": "A" * 400}]  # ~100 tokens

    await _insert_turn(long_term_store, "sess-2", "corr-a", short_turn, created_at=1.0)
    await _insert_turn(long_term_store, "sess-2", "corr-b", long_turn, created_at=2.0)

    # Set max_tokens so only the most recent turn fits.
    # short_turn serialized ≈ 8 tokens; long_turn serialized ≈ 109 tokens; total ≈ 117.
    # With max_tokens=110, total (117) exceeds budget, so the oldest (short_turn, 8 tokens)
    # is dropped, leaving long_turn (109 tokens) within budget.
    ctx = HandlerContext(
        redis_conn=redis_mock,
        long_term_store=long_term_store,
        file_store=AsyncMock(),
        req={
            "session_id": "sess-2",
            "max_tokens": 110,
            "correlation_id": "req-corr-2",
        },
        stream_res="relais:memory:response",
    )

    handler = HistoryReadHandler()
    await handler.handle(ctx)

    redis_mock.lpush.assert_called_once()
    result = json.loads(redis_mock.lpush.call_args[0][1])

    # Only the most recent turn should remain (oldest dropped first)
    assert len(result) == 1
    assert result[0] == long_turn


@pytest.mark.asyncio
async def test_history_read_empty_session(
    long_term_store: LongTermStore, redis_mock: AsyncMock
) -> None:
    """Returns empty list for an unknown session."""
    ctx = HandlerContext(
        redis_conn=redis_mock,
        long_term_store=long_term_store,
        file_store=AsyncMock(),
        req={
            "session_id": "nonexistent-session",
            "max_tokens": 64000,
            "correlation_id": "req-corr-3",
        },
        stream_res="relais:memory:response",
    )

    handler = HistoryReadHandler()
    await handler.handle(ctx)

    redis_mock.lpush.assert_called_once()
    result = json.loads(redis_mock.lpush.call_args[0][1])
    assert result == []


@pytest.mark.asyncio
async def test_history_read_sets_ttl(
    long_term_store: LongTermStore, redis_mock: AsyncMock
) -> None:
    """The Redis response key has a 60s TTL."""
    await _insert_turn(
        long_term_store, "sess-3", "corr-x",
        [{"role": "user", "content": "test"}],
        created_at=1.0,
    )

    ctx = HandlerContext(
        redis_conn=redis_mock,
        long_term_store=long_term_store,
        file_store=AsyncMock(),
        req={
            "session_id": "sess-3",
            "max_tokens": 64000,
            "correlation_id": "req-corr-4",
        },
        stream_res="relais:memory:response",
    )

    handler = HistoryReadHandler()
    await handler.handle(ctx)

    response_key = "relais:memory:response:req-corr-4"
    redis_mock.expire.assert_called_once_with(response_key, 60)
