"""Tests for LongTermStore.query() pagination — TDD Wave 1D."""

from pathlib import Path

import pytest
import pytest_asyncio

from souvenir.long_term_store import LongTermStore, PaginatedResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    """LongTermStore backed by a temporary in-memory-equivalent SQLite file."""
    s = LongTermStore(db_path=tmp_path / "query_test.db")
    await s._create_tables()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# Helper — insert ArchivedMessage rows directly via archive()
# ---------------------------------------------------------------------------

from common.envelope import Envelope


def _envelope(
    user_id: str,
    session_id: str,
    content: str,
    user_message: str = "",
) -> Envelope:
    return Envelope(
        content=content,
        sender_id=user_id,
        channel="discord",
        session_id=session_id,
        metadata={"user_message": user_message},
    )


# ---------------------------------------------------------------------------
# T1: query returns messages for user, ordered newest first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_returns_messages_newest_first(store: LongTermStore) -> None:
    """query() must return ArchivedMessage rows for the given user_id, ordered
    by timestamp descending (most recent first)."""
    import time

    base_ts = 1_700_000_000.0

    # Archive 3 messages with distinct timestamps by patching created_at after insert
    # We use archive() which inserts ArchivedMessage rows.
    # To control timestamps we use a helper that manipulates the DB directly.
    from sqlmodel import select
    from souvenir.models import ArchivedMessage
    from sqlmodel.ext.asyncio.session import AsyncSession

    async with store._session_factory() as session:
        for i in range(3):
            session.add(
                ArchivedMessage(
                    session_id="sess-t1",
                    sender_id="user_t1",
                    channel="discord",
                    role="user",
                    content=f"message {i}",
                    correlation_id=f"corr-{i}",
                    created_at=base_ts + i,
                )
            )
        await session.commit()

    result = await store.query("user_t1")

    assert isinstance(result, PaginatedResult)
    assert result.total == 3
    assert len(result.items) == 3
    # Newest first: created_at descending
    assert result.items[0].content == "message 2"
    assert result.items[1].content == "message 1"
    assert result.items[2].content == "message 0"


# ---------------------------------------------------------------------------
# T2: limit and offset work correctly (pagination)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_limit_and_offset(store: LongTermStore) -> None:
    """query() with limit=2, offset=1 must return the correct slice."""
    from souvenir.models import ArchivedMessage

    base_ts = 1_700_000_000.0

    async with store._session_factory() as session:
        for i in range(5):
            session.add(
                ArchivedMessage(
                    session_id="sess-t2",
                    sender_id="user_t2",
                    channel="discord",
                    role="assistant",
                    content=f"item {i}",
                    correlation_id=f"corr-{i}",
                    created_at=base_ts + i,
                )
            )
        await session.commit()

    # Full order DESC: item4, item3, item2, item1, item0
    # offset=1, limit=2 → item3, item2
    result = await store.query("user_t2", limit=2, offset=1)

    assert len(result.items) == 2
    assert result.total == 5
    assert result.items[0].content == "item 3"
    assert result.items[1].content == "item 2"
    assert result.limit == 2
    assert result.offset == 1


# ---------------------------------------------------------------------------
# T3: since/until filters by timestamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_since_until_filter(store: LongTermStore) -> None:
    """query() with since/until must include only messages within the range."""
    from souvenir.models import ArchivedMessage

    base_ts = 1_700_000_000.0

    async with store._session_factory() as session:
        for i in range(6):
            session.add(
                ArchivedMessage(
                    session_id="sess-t3",
                    sender_id="user_t3",
                    channel="discord",
                    role="user",
                    content=f"ts {i}",
                    correlation_id=f"corr-{i}",
                    created_at=base_ts + i,
                )
            )
        await session.commit()

    # since=base+1, until=base+3 → ts 1, ts 2, ts 3 (3 items)
    result = await store.query(
        "user_t3", since=base_ts + 1, until=base_ts + 3
    )

    contents = {item.content for item in result.items}
    assert contents == {"ts 1", "ts 2", "ts 3"}
    assert result.total == 3


# ---------------------------------------------------------------------------
# T4: search filters by content substring (case-insensitive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_search_case_insensitive(store: LongTermStore) -> None:
    """query(search=...) must do case-insensitive substring match on content."""
    from souvenir.models import ArchivedMessage

    base_ts = 1_700_000_000.0

    async with store._session_factory() as session:
        for content in ["Hello world", "HELLO again", "goodbye friend", "say hello"]:
            session.add(
                ArchivedMessage(
                    session_id="sess-t4",
                    sender_id="user_t4",
                    channel="discord",
                    role="user",
                    content=content,
                    correlation_id="corr-t4",
                    created_at=base_ts,
                )
            )
            base_ts += 1
        await session.commit()

    result = await store.query("user_t4", search="hello")

    contents = {item.content for item in result.items}
    assert contents == {"Hello world", "HELLO again", "say hello"}
    assert result.total == 3


# ---------------------------------------------------------------------------
# T5: has_more=True when more results exist beyond limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_has_more_true(store: LongTermStore) -> None:
    """has_more must be True when total > offset + len(items)."""
    from souvenir.models import ArchivedMessage

    base_ts = 1_700_000_000.0

    async with store._session_factory() as session:
        for i in range(5):
            session.add(
                ArchivedMessage(
                    session_id="sess-t5",
                    sender_id="user_t5",
                    channel="discord",
                    role="user",
                    content=f"msg {i}",
                    correlation_id=f"corr-{i}",
                    created_at=base_ts + i,
                )
            )
        await session.commit()

    result = await store.query("user_t5", limit=2, offset=0)

    assert result.has_more is True
    assert result.total == 5
    assert len(result.items) == 2


# ---------------------------------------------------------------------------
# T6: has_more=False on last page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_has_more_false_on_last_page(store: LongTermStore) -> None:
    """has_more must be False when all remaining items fit in the current page."""
    from souvenir.models import ArchivedMessage

    base_ts = 1_700_000_000.0

    async with store._session_factory() as session:
        for i in range(4):
            session.add(
                ArchivedMessage(
                    session_id="sess-t6",
                    sender_id="user_t6",
                    channel="discord",
                    role="user",
                    content=f"item {i}",
                    correlation_id=f"corr-{i}",
                    created_at=base_ts + i,
                )
            )
        await session.commit()

    # offset=2, limit=5 → 2 items remain, total=4, 4 <= 2+2
    result = await store.query("user_t6", limit=5, offset=2)

    assert result.has_more is False
    assert result.total == 4
    assert len(result.items) == 2


# ---------------------------------------------------------------------------
# T7: empty result for unknown user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_empty_for_unknown_user(store: LongTermStore) -> None:
    """query() for a user_id with no messages must return an empty PaginatedResult."""
    result = await store.query("no_such_user")

    assert isinstance(result, PaginatedResult)
    assert result.total == 0
    assert result.items == ()
    assert result.has_more is False


# ---------------------------------------------------------------------------
# T8: total reflects count without limit (not just current page size)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_total_is_unsliced_count(store: LongTermStore) -> None:
    """total in PaginatedResult must reflect the full matching count, not the
    page size returned by limit/offset."""
    from souvenir.models import ArchivedMessage

    base_ts = 1_700_000_000.0

    async with store._session_factory() as session:
        for i in range(10):
            session.add(
                ArchivedMessage(
                    session_id="sess-t8",
                    sender_id="user_t8",
                    channel="discord",
                    role="assistant",
                    content=f"resp {i}",
                    correlation_id=f"corr-{i}",
                    created_at=base_ts + i,
                )
            )
        await session.commit()

    result = await store.query("user_t8", limit=3, offset=0)

    assert result.total == 10       # full count, not page size
    assert len(result.items) == 3   # only 3 returned on this page
    assert result.has_more is True


# ---------------------------------------------------------------------------
# T9: PaginatedResult is a frozen dataclass (immutability requirement)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_paginated_result_is_frozen() -> None:
    """PaginatedResult must be a frozen dataclass — mutation must raise."""
    pr = PaginatedResult(items=(), total=0, limit=20, offset=0, has_more=False)

    with pytest.raises((AttributeError, TypeError)):
        pr.total = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# T10: items field is a tuple, not a list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_items_is_tuple(store: LongTermStore) -> None:
    """items in PaginatedResult must be a tuple, not a list."""
    from souvenir.models import ArchivedMessage

    async with store._session_factory() as session:
        session.add(
            ArchivedMessage(
                session_id="sess-t10",
                sender_id="user_t10",
                channel="discord",
                role="user",
                content="something",
                correlation_id="corr-t10",
                created_at=1_700_000_000.0,
            )
        )
        await session.commit()

    result = await store.query("user_t10")
    assert isinstance(result.items, tuple)
