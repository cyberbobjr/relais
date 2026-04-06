"""Tests for Forgeron trace store and should_analyze logic (Étape 1)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from forgeron.config import ForgeonConfig
from forgeron.models import SkillTrace
from forgeron.trace_store import SkillTraceStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> SkillTraceStore:
    """In-memory SQLite trace store backed by a temp file."""
    s = SkillTraceStore(db_path=tmp_path / "test_forgeron.db")
    await s._create_tables()
    yield s
    await s.close()


def _cfg(min_traces: int = 5, min_error_rate: float = 0.3) -> ForgeonConfig:
    """Return a minimal ForgeonConfig for testing."""
    return ForgeonConfig(
        min_traces_before_analysis=min_traces,
        min_error_rate=min_error_rate,
        min_improvement_interval_seconds=3600,
        patch_mode=True,
        annotation_mode=False,
        llm_profile="fast",
        skills_dir=None,
    )


def _trace(skill: str, calls: int, errors: int) -> SkillTrace:
    """Build a SkillTrace with the given counters."""
    return SkillTrace(
        skill_name=skill,
        correlation_id="test-corr",
        tool_call_count=calls,
        tool_error_count=errors,
        messages_raw=json.dumps([]),
    )


def _redis_no_cooldown() -> AsyncMock:
    """Mock Redis connection with no active cooldown (ttl returns -2)."""
    mock = AsyncMock()
    mock.ttl = AsyncMock(return_value=-2)
    return mock


def _redis_active_cooldown(ttl: int = 3600) -> AsyncMock:
    """Mock Redis connection with an active cooldown TTL."""
    mock = AsyncMock()
    mock.ttl = AsyncMock(return_value=ttl)
    return mock


# ---------------------------------------------------------------------------
# error_rate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_error_rate_zero_when_no_traces(store: SkillTraceStore) -> None:
    rate = await store.error_rate("mail-agent", window=10)
    assert rate == 0.0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_error_rate_zero_when_no_calls(store: SkillTraceStore) -> None:
    await store.add_trace(_trace("mail-agent", calls=0, errors=0))
    rate = await store.error_rate("mail-agent", window=10)
    assert rate == 0.0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_error_rate_computed_correctly(store: SkillTraceStore) -> None:
    # 3 calls, 1 error → 1/3 ≈ 0.333
    await store.add_trace(_trace("mail-agent", calls=2, errors=0))
    await store.add_trace(_trace("mail-agent", calls=1, errors=1))
    rate = await store.error_rate("mail-agent", window=10)
    assert abs(rate - 1 / 3) < 1e-9


@pytest.mark.asyncio
@pytest.mark.unit
async def test_error_rate_window_limits_traces(store: SkillTraceStore) -> None:
    # Add 5 traces with errors, then 5 clean ones (most-recent first with window=5)
    for _ in range(5):
        await store.add_trace(_trace("skill-x", calls=2, errors=2))
    for _ in range(5):
        await store.add_trace(_trace("skill-x", calls=2, errors=0))
    # With window=5 we get the 5 most recent (0-error) traces → rate = 0
    rate = await store.error_rate("skill-x", window=5)
    assert rate == 0.0


# ---------------------------------------------------------------------------
# should_analyze
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_should_analyze_false_not_enough_traces(store: SkillTraceStore) -> None:
    cfg = _cfg(min_traces=5)
    for _ in range(3):
        await store.add_trace(_trace("mail-agent", calls=1, errors=1))
    result = await store.should_analyze("mail-agent", cfg, _redis_no_cooldown())
    assert result is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_should_analyze_false_low_error_rate(store: SkillTraceStore) -> None:
    cfg = _cfg(min_traces=3, min_error_rate=0.5)
    for _ in range(3):
        await store.add_trace(_trace("mail-agent", calls=4, errors=1))  # 25% errors
    result = await store.should_analyze("mail-agent", cfg, _redis_no_cooldown())
    assert result is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_should_analyze_false_cooldown_active(store: SkillTraceStore) -> None:
    cfg = _cfg(min_traces=3, min_error_rate=0.3)
    for _ in range(3):
        await store.add_trace(_trace("mail-agent", calls=1, errors=1))
    result = await store.should_analyze(
        "mail-agent", cfg, _redis_active_cooldown(ttl=600)
    )
    assert result is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_should_analyze_true_all_conditions_met(store: SkillTraceStore) -> None:
    cfg = _cfg(min_traces=3, min_error_rate=0.3)
    for _ in range(3):
        await store.add_trace(_trace("mail-agent", calls=2, errors=1))  # 50% errors
    result = await store.should_analyze("mail-agent", cfg, _redis_no_cooldown())
    assert result is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_should_analyze_isolated_per_skill(store: SkillTraceStore) -> None:
    """High error rate on one skill does not trigger analysis on another."""
    cfg = _cfg(min_traces=3, min_error_rate=0.3)
    for _ in range(3):
        await store.add_trace(_trace("bad-skill", calls=1, errors=1))
    result = await store.should_analyze("good-skill", cfg, _redis_no_cooldown())
    assert result is False
