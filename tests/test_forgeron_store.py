"""Tests for Forgeron SkillTraceStore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from forgeron.models import SkillTrace
from forgeron.trace_store import SkillTraceStore


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> SkillTraceStore:
    """In-memory SQLite trace store backed by a temp file."""
    s = SkillTraceStore(db_path=tmp_path / "test_forgeron.db")
    await s._create_tables()
    yield s
    await s.close()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_add_trace_persists(store: SkillTraceStore) -> None:
    """add_trace() must persist a trace without raising."""
    trace = SkillTrace(
        skill_name="mail-agent",
        correlation_id="test-corr",
        tool_call_count=3,
        tool_error_count=1,
        messages_raw=json.dumps([]),
    )
    await store.add_trace(trace)
