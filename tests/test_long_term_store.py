"""TDD tests for LongTermStore.list_sessions and LongTermStore.get_session_history."""

import time

import pytest
import pytest_asyncio
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from souvenir.long_term_store import LongTermStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path):
    """LongTermStore backed by an in-memory SQLite database."""
    import sqlite3  # noqa: F401 — ensure stdlib is available

    # Build an in-memory store by patching the engine after construction.
    # We re-use the store's _create_tables() helper used in other tests.
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Create a store instance without touching the filesystem.
    s = LongTermStore.__new__(LongTermStore)
    s._db_path = tmp_path / "memory.db"
    s._engine = engine
    s._session_factory = session_factory

    # Create tables.
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    yield s

    await engine.dispose()


async def _insert_row(
    store: LongTermStore,
    *,
    session_id: str,
    sender_id: str,
    correlation_id: str,
    user_content: str = "hello",
    assistant_content: str = "world",
    created_at: float | None = None,
) -> None:
    """Insert a single ArchivedMessage row directly via the session factory."""
    from souvenir.models import ArchivedMessage
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    now = created_at if created_at is not None else time.time()
    stmt = (
        sqlite_insert(ArchivedMessage)
        .values(
            session_id=session_id,
            sender_id=sender_id,
            channel="test",
            user_content=user_content,
            assistant_content=assistant_content,
            messages_raw="[]",
            correlation_id=correlation_id,
            created_at=now,
        )
        .on_conflict_do_nothing()
    )
    async with store._session_factory() as session:
        await session.execute(stmt)
        await session.commit()


# ---------------------------------------------------------------------------
# list_sessions — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_sessions_empty_db(store: LongTermStore) -> None:
    """list_sessions returns an empty list when no rows exist."""
    result = await store.list_sessions("usr_alice")
    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_sessions_single_session(store: LongTermStore) -> None:
    """list_sessions returns one dict with correct fields for a single session."""
    await _insert_row(
        store,
        session_id="sess-1",
        sender_id="discord:usr_alice",
        correlation_id="corr-1",
        assistant_content="Hello there",
    )

    result = await store.list_sessions("usr_alice")

    assert len(result) == 1
    row = result[0]
    assert row["session_id"] == "sess-1"
    assert row["turn_count"] == 1
    assert isinstance(row["last_active"], float)
    assert isinstance(row["preview"], str)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_sessions_multiple_sessions_ordering(store: LongTermStore) -> None:
    """list_sessions returns sessions ordered by last_active DESC."""
    t_old = time.time() - 1000
    t_new = time.time()

    await _insert_row(
        store,
        session_id="sess-old",
        sender_id="discord:usr_alice",
        correlation_id="corr-old",
        created_at=t_old,
    )
    await _insert_row(
        store,
        session_id="sess-new",
        sender_id="discord:usr_alice",
        correlation_id="corr-new",
        created_at=t_new,
    )

    result = await store.list_sessions("usr_alice")

    assert len(result) == 2
    assert result[0]["session_id"] == "sess-new"
    assert result[1]["session_id"] == "sess-old"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_sessions_turn_count(store: LongTermStore) -> None:
    """turn_count reflects the number of rows for that session."""
    for i in range(3):
        await _insert_row(
            store,
            session_id="sess-multi",
            sender_id="discord:usr_alice",
            correlation_id=f"corr-{i}",
        )

    result = await store.list_sessions("usr_alice")

    assert len(result) == 1
    assert result[0]["turn_count"] == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_sessions_preview_truncated_to_80_chars(store: LongTermStore) -> None:
    """preview is truncated to the first 80 characters of assistant_content."""
    long_content = "A" * 200

    await _insert_row(
        store,
        session_id="sess-long",
        sender_id="discord:usr_alice",
        correlation_id="corr-long",
        assistant_content=long_content,
    )

    result = await store.list_sessions("usr_alice")

    assert len(result[0]["preview"]) <= 80


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_sessions_preview_is_last_assistant_content(store: LongTermStore) -> None:
    """preview is taken from the latest (highest created_at) assistant_content."""
    t_base = time.time()

    await _insert_row(
        store,
        session_id="sess-order",
        sender_id="discord:usr_alice",
        correlation_id="corr-first",
        assistant_content="First reply",
        created_at=t_base,
    )
    await _insert_row(
        store,
        session_id="sess-order",
        sender_id="discord:usr_alice",
        correlation_id="corr-last",
        assistant_content="Last reply",
        created_at=t_base + 10,
    )

    result = await store.list_sessions("usr_alice")

    assert result[0]["preview"].startswith("Last reply")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_sessions_limit(store: LongTermStore) -> None:
    """list_sessions honours the limit parameter."""
    t_base = time.time()
    for i in range(5):
        await _insert_row(
            store,
            session_id=f"sess-{i}",
            sender_id="discord:usr_alice",
            correlation_id=f"corr-{i}",
            created_at=t_base + i,
        )

    result = await store.list_sessions("usr_alice", limit=3)

    assert len(result) == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_sessions_user_filter(store: LongTermStore) -> None:
    """list_sessions only returns sessions matching sender_id LIKE '%user_id%'."""
    await _insert_row(
        store,
        session_id="sess-alice",
        sender_id="discord:usr_alice",
        correlation_id="corr-alice",
    )
    await _insert_row(
        store,
        session_id="sess-bob",
        sender_id="discord:usr_bob",
        correlation_id="corr-bob",
    )

    result = await store.list_sessions("usr_alice")

    assert len(result) == 1
    assert result[0]["session_id"] == "sess-alice"


# ---------------------------------------------------------------------------
# get_session_history — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_session_history_nonexistent(store: LongTermStore) -> None:
    """get_session_history returns an empty list for an unknown session_id."""
    result = await store.get_session_history("no-such-session")
    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_session_history_returns_correct_fields(store: LongTermStore) -> None:
    """get_session_history rows contain the expected keys."""
    await _insert_row(
        store,
        session_id="sess-a",
        sender_id="discord:usr_alice",
        correlation_id="corr-x",
        user_content="question",
        assistant_content="answer",
    )

    result = await store.get_session_history("sess-a")

    assert len(result) == 1
    row = result[0]
    assert set(row.keys()) == {"user_content", "assistant_content", "created_at", "correlation_id"}
    assert row["user_content"] == "question"
    assert row["assistant_content"] == "answer"
    assert row["correlation_id"] == "corr-x"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_session_history_ordering_asc(store: LongTermStore) -> None:
    """get_session_history returns rows ordered by created_at ASC."""
    t_base = time.time()

    await _insert_row(
        store,
        session_id="sess-b",
        sender_id="discord:usr_alice",
        correlation_id="corr-b2",
        assistant_content="second",
        created_at=t_base + 10,
    )
    await _insert_row(
        store,
        session_id="sess-b",
        sender_id="discord:usr_alice",
        correlation_id="corr-b1",
        assistant_content="first",
        created_at=t_base,
    )

    result = await store.get_session_history("sess-b")

    assert result[0]["assistant_content"] == "first"
    assert result[1]["assistant_content"] == "second"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_session_history_limit(store: LongTermStore) -> None:
    """get_session_history honours the limit parameter."""
    t_base = time.time()
    for i in range(10):
        await _insert_row(
            store,
            session_id="sess-limit",
            sender_id="discord:usr_alice",
            correlation_id=f"corr-lim-{i}",
            created_at=t_base + i,
        )

    result = await store.get_session_history("sess-limit", limit=4)

    assert len(result) == 4


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_session_history_isolation(store: LongTermStore) -> None:
    """get_session_history only returns rows for the requested session_id."""
    await _insert_row(
        store,
        session_id="sess-target",
        sender_id="discord:usr_alice",
        correlation_id="corr-target",
    )
    await _insert_row(
        store,
        session_id="sess-other",
        sender_id="discord:usr_alice",
        correlation_id="corr-other",
    )

    result = await store.get_session_history("sess-target")

    assert len(result) == 1
    assert result[0]["correlation_id"] == "corr-target"
