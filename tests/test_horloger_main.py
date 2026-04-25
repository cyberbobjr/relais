"""Tests for Horloger brick main entry point (TDD — RED phase).

Tests are written before the implementation to drive the design.

Covers:
- stream_specs() returns empty list (producer-only brick)
- _tick() triggers due jobs by calling redis.xadd
- _tick() records skipped (disabled/catch-up) jobs without calling xadd
- _tick() records publish_failed status when xadd raises
- _tick() calls registry.reload() on every invocation
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from horloger.job_model import JobSpec
from horloger.scheduler import DueJob


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


def _make_job_spec(
    job_id: str = "test-job",
    owner_id: str = "usr_alice",
    enabled: bool = True,
    channel: str = "discord",
    prompt: str = "Say hello",
) -> JobSpec:
    """Return a minimal valid JobSpec for test purposes.

    Args:
        job_id: Unique job identifier.
        owner_id: Stable user identifier.
        enabled: Whether the job is enabled.
        channel: Target channel name.
        prompt: Prompt text.

    Returns:
        A frozen JobSpec instance.
    """
    return JobSpec(
        id=job_id,
        owner_id=owner_id,
        schedule="0 8 * * *",
        channel=channel,
        prompt=prompt,
        enabled=enabled,
        created_at="2025-01-01T00:00:00Z",
        description="Test job",
        timezone="UTC",
    )


def _make_due_job(job_id: str = "test-job", enabled: bool = True) -> DueJob:
    """Return a DueJob wrapping a minimal JobSpec.

    Args:
        job_id: Unique job identifier.
        enabled: Whether the job is enabled.

    Returns:
        A DueJob with a scheduled_for set to 60 seconds ago.
    """
    return DueJob(
        spec=_make_job_spec(job_id=job_id, enabled=enabled),
        scheduled_for=time.time() - 60.0,
    )


# ---------------------------------------------------------------------------
# Fixture: Horloger instance with all heavy dependencies mocked out
# ---------------------------------------------------------------------------


@pytest.fixture()
def horloger_instance(tmp_path: Path):
    """Return a Horloger instance with all I/O mocked.

    Patches:
    - common.brick_base.RedisClient to avoid real Redis connections.
    - horloger.main.ExecutionStore so no SQLite I/O occurs.
    - JobRegistry and Scheduler are replaced with MagicMock instances
      injected directly on the returned object.

    Args:
        tmp_path: Pytest tmp_path fixture for a throwaway directory.

    Yields:
        Configured Horloger instance ready for unit tests.
    """
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    db_path = tmp_path / "horloger.db"

    fake_config = {
        "tick_interval_seconds": 30,
        "catch_up_window_seconds": 120,
        "jobs_dir": str(jobs_dir),
        "db_path": str(db_path),
    }

    with (
        patch("common.brick_base.RedisClient"),
        patch("horloger.main.ExecutionStore") as MockStore,
        patch("horloger.main.load_horloger_config", return_value=fake_config),
    ):
        MockStore.return_value = AsyncMock()

        from horloger.main import Horloger

        brick = Horloger()

        # Replace registry and scheduler with mocks
        brick._registry = MagicMock()
        brick._scheduler = MagicMock()

        yield brick


# ---------------------------------------------------------------------------
# RED 1 — stream_specs() must return an empty list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_specs_returns_empty(horloger_instance) -> None:
    """Horloger is a producer-only brick — stream_specs() must return [].

    A non-empty list would cause BrickBase to start consumer loops that
    would block indefinitely waiting for messages that never arrive on a
    CRON scheduler.
    """
    result = horloger_instance.stream_specs()
    assert result == [], (
        f"stream_specs() should return [] for a producer-only brick, got {result!r}"
    )


# ---------------------------------------------------------------------------
# RED 2 — _tick() triggers due jobs → xadd called once per due job
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_triggers_due_jobs(horloger_instance) -> None:
    """When scheduler returns one due job, xadd is called once with the correct stream.

    Verifies the happy path: a single enabled, on-time job produces exactly
    one Redis XADD call targeting STREAM_INCOMING_HORLOGER.
    """
    from common.streams import STREAM_INCOMING_HORLOGER

    due_job = _make_due_job()
    horloger_instance._scheduler.get_due_jobs.return_value = ([due_job], [])
    horloger_instance._registry.reload.return_value = {due_job.spec.id: due_job.spec}

    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock(return_value=b"1-0")

    await horloger_instance._tick(mock_redis)

    mock_redis.xadd.assert_called_once()
    call_args = mock_redis.xadd.call_args
    # First positional argument must be the horloger incoming stream
    stream_name = call_args[0][0]
    assert stream_name == STREAM_INCOMING_HORLOGER, (
        f"xadd was called with stream={stream_name!r}, "
        f"expected {STREAM_INCOMING_HORLOGER!r}"
    )


# ---------------------------------------------------------------------------
# RED 3 — _tick() skips disabled jobs → xadd NOT called, store.record called
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_skips_disabled_jobs(horloger_instance) -> None:
    """When scheduler returns a skipped job, xadd is not called.

    The execution is still recorded in the store with status='skipped_disabled'
    so operators can see that the job was deliberately skipped rather than
    silently ignored.
    """
    skipped_due = DueJob(
        spec=_make_job_spec(job_id="test-job", enabled=False),
        scheduled_for=time.time() - 60.0,
        skip_reason="skipped_disabled",
    )
    horloger_instance._scheduler.get_due_jobs.return_value = ([], [skipped_due])
    horloger_instance._registry.reload.return_value = {
        skipped_due.spec.id: skipped_due.spec
    }

    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    await horloger_instance._tick(mock_redis)

    mock_redis.xadd.assert_not_called()

    # The store must have been told to record this skipped execution
    store: AsyncMock = horloger_instance._store
    store.record.assert_called_once()
    recorded = store.record.call_args[0][0]
    assert recorded.status == "skipped_disabled", (
        f"Expected status='skipped_disabled', got {recorded.status!r}"
    )
    assert recorded.job_id == skipped_due.spec.id


# ---------------------------------------------------------------------------
# RED 4 — _tick() handles xadd failure → store.record with status=publish_failed
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_records_publish_failure(horloger_instance) -> None:
    """When redis.xadd raises, the execution is recorded with status='publish_failed'.

    The tick must not propagate the exception — it catches it, records the
    failure, and continues so the scheduler stays running.
    """
    due_job = _make_due_job()
    horloger_instance._scheduler.get_due_jobs.return_value = ([due_job], [])
    horloger_instance._registry.reload.return_value = {due_job.spec.id: due_job.spec}

    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock(side_effect=ConnectionError("Redis is down"))

    # Must not raise
    await horloger_instance._tick(mock_redis)

    store: AsyncMock = horloger_instance._store
    store.record.assert_called_once()
    recorded = store.record.call_args[0][0]
    assert recorded.status == "publish_failed", (
        f"Expected status='publish_failed', got {recorded.status!r}"
    )
    assert recorded.job_id == due_job.spec.id
    assert recorded.error is not None, "error field should contain the exception message"


# ---------------------------------------------------------------------------
# RED 5 — _tick() calls registry.reload() on each invocation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_reloads_registry_each_time(horloger_instance) -> None:
    """registry.reload() is called exactly once per _tick() call.

    This ensures that newly added or modified job files are picked up
    on every tick without requiring a process restart.
    """
    horloger_instance._scheduler.get_due_jobs.return_value = ([], [])
    horloger_instance._registry.reload.return_value = {}

    mock_redis = AsyncMock()

    await horloger_instance._tick(mock_redis)
    await horloger_instance._tick(mock_redis)
    await horloger_instance._tick(mock_redis)

    assert horloger_instance._registry.reload.call_count == 3, (
        f"registry.reload() should be called once per tick, "
        f"was called {horloger_instance._registry.reload.call_count} time(s)"
    )


# ---------------------------------------------------------------------------
# RED 6 — _tick() records triggered execution in store
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_records_triggered_execution(horloger_instance) -> None:
    """When xadd succeeds, the store records a 'triggered' execution.

    This ensures full audit trail: every successful trigger is persisted
    to the SQLite store so the operator can review history.
    """
    due_job = _make_due_job()
    horloger_instance._scheduler.get_due_jobs.return_value = ([due_job], [])
    horloger_instance._registry.reload.return_value = {due_job.spec.id: due_job.spec}

    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock(return_value=b"1-0")

    await horloger_instance._tick(mock_redis)

    store: AsyncMock = horloger_instance._store
    store.record.assert_called_once()
    recorded = store.record.call_args[0][0]
    assert recorded.status == "triggered", (
        f"Expected status='triggered', got {recorded.status!r}"
    )
    assert recorded.job_id == due_job.spec.id


# ---------------------------------------------------------------------------
# RED 7 — _tick() calls scheduler.mark_triggered after successful xadd
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_marks_triggered_after_xadd(horloger_instance) -> None:
    """scheduler.mark_triggered() is called after a successful xadd.

    This prevents the double-fire guard from firing the same job twice
    within one scheduling window — the scheduler's internal state must
    be updated after every successful trigger.
    """
    due_job = _make_due_job()
    horloger_instance._scheduler.get_due_jobs.return_value = ([due_job], [])
    horloger_instance._registry.reload.return_value = {due_job.spec.id: due_job.spec}

    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock(return_value=b"1-0")

    await horloger_instance._tick(mock_redis)

    horloger_instance._scheduler.mark_triggered.assert_called_once()
    call_args = horloger_instance._scheduler.mark_triggered.call_args[0]
    assert call_args[0] == due_job.spec.id, (
        f"mark_triggered was called with job_id={call_args[0]!r}, "
        f"expected {due_job.spec.id!r}"
    )
