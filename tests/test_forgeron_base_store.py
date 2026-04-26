"""Tests for BaseAsyncStore — engine de-duplication and context manager protocol."""

from __future__ import annotations

from pathlib import Path

import pytest

from forgeron.base_store import BaseAsyncStore
from forgeron.session_store import SessionStore
from forgeron.trace_store import SkillTraceStore


@pytest.mark.asyncio
@pytest.mark.unit
async def test_base_store_creates_engine(tmp_path: Path) -> None:
    """BaseAsyncStore must expose _engine and _session_factory after __init__."""
    store = BaseAsyncStore(db_path=tmp_path / "test.db")
    assert store._engine is not None
    assert store._session_factory is not None
    await store.close()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_base_store_creates_db_parent(tmp_path: Path) -> None:
    """BaseAsyncStore must create the parent directory when it is missing."""
    nested = tmp_path / "a" / "b" / "test.db"
    store = BaseAsyncStore(db_path=nested)
    assert nested.parent.exists()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_base_store_context_manager(tmp_path: Path) -> None:
    """BaseAsyncStore must support the async context manager protocol."""
    async with BaseAsyncStore(db_path=tmp_path / "test.db") as store:
        assert store._engine is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_session_store_inherits_base(tmp_path: Path) -> None:
    """SessionStore must be a BaseAsyncStore instance."""
    store = SessionStore(db_path=tmp_path / "session.db")
    assert isinstance(store, BaseAsyncStore)
    await store.close()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_trace_store_inherits_base(tmp_path: Path) -> None:
    """SkillTraceStore must be a BaseAsyncStore instance."""
    store = SkillTraceStore(db_path=tmp_path / "trace.db")
    assert isinstance(store, BaseAsyncStore)
    await store.close()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_trace_store_context_manager(tmp_path: Path) -> None:
    """SkillTraceStore must support async context manager usage via BaseAsyncStore."""
    async with SkillTraceStore(db_path=tmp_path / "trace.db") as store:
        assert store._engine is not None
