"""Tests for horloger.execution_store — written before implementation (TDD RED phase).

All tests are async and use a temporary SQLite database via tmp_path so they are
fully isolated from one another and leave no state behind.
"""

import time
from pathlib import Path

import pytest
import pytest_asyncio

from horloger.models import HorlogerExecution
from horloger.execution_store import ExecutionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_execution(
    job_id: str = "job-001",
    owner_id: str = "usr_admin",
    channel: str = "discord",
    correlation_id: str = "corr-abc",
    scheduled_for: float | None = None,
    triggered_at: float | None = None,
    status: str = "triggered",
    error: str | None = None,
) -> HorlogerExecution:
    """Build a HorlogerExecution instance with sensible defaults.

    Args:
        job_id: Identifier of the scheduled job.
        owner_id: User who owns the job.
        channel: Target channel (e.g. "discord").
        correlation_id: UUID correlating this execution to a pipeline run.
        scheduled_for: Epoch seconds of when the job was planned to fire.
        triggered_at: Epoch seconds of when the job actually fired.
        status: Execution status string.
        error: Optional error message when status indicates failure.

    Returns:
        A HorlogerExecution instance (not yet persisted).
    """
    now = time.time()
    return HorlogerExecution(
        correlation_id=correlation_id,
        job_id=job_id,
        owner_id=owner_id,
        channel=channel,
        prompt="Say hello",
        scheduled_for=scheduled_for or now,
        triggered_at=triggered_at or now,
        status=status,
        error=error,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> ExecutionStore:
    """Provide an initialised ExecutionStore backed by a temporary SQLite file.

    Args:
        tmp_path: pytest-provided temporary directory unique per test.

    Returns:
        An ExecutionStore with its tables already created.
    """
    db_path = tmp_path / "horloger.db"
    s = ExecutionStore(db_path=db_path)
    await s.init()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# init()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_init_creates_table(tmp_path: Path) -> None:
    """init() must create the horloger_executions table without raising.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    db_path = tmp_path / "init_test.db"
    s = ExecutionStore(db_path=db_path)
    # Should not raise
    await s.init()
    # DB file must exist
    assert db_path.exists()
    await s.close()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_init_is_idempotent(tmp_path: Path) -> None:
    """Calling init() twice must not raise (CREATE TABLE IF NOT EXISTS semantics).

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    db_path = tmp_path / "idempotent.db"
    s = ExecutionStore(db_path=db_path)
    await s.init()
    await s.init()  # second call must be safe
    await s.close()


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_persists_execution(store: ExecutionStore) -> None:
    """record() must persist the execution so it can be retrieved afterwards.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    execution = _make_execution(job_id="job-record", correlation_id="corr-001")
    await store.record(execution)

    result = await store.get_last_execution("job-record")
    assert result is not None
    assert result.correlation_id == "corr-001"
    assert result.job_id == "job-record"
    assert result.owner_id == "usr_admin"
    assert result.channel == "discord"
    assert result.prompt == "Say hello"
    assert result.status == "triggered"
    assert result.error is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_persists_error_field(store: ExecutionStore) -> None:
    """record() must persist the optional error field when set.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    execution = _make_execution(
        job_id="job-err",
        correlation_id="corr-err",
        status="publish_failed",
        error="Redis connection refused",
    )
    await store.record(execution)

    result = await store.get_last_execution("job-err")
    assert result is not None
    assert result.status == "publish_failed"
    assert result.error == "Redis connection refused"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_record_multiple_executions_for_same_job(store: ExecutionStore) -> None:
    """record() must support multiple executions for the same job_id.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    t0 = time.time()
    exec1 = _make_execution(job_id="job-multi", correlation_id="c1", triggered_at=t0)
    exec2 = _make_execution(job_id="job-multi", correlation_id="c2", triggered_at=t0 + 60)
    await store.record(exec1)
    await store.record(exec2)

    executions = await store.list_executions("job-multi")
    assert len(executions) == 2


# ---------------------------------------------------------------------------
# get_last_execution()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_last_execution_unknown_job(store: ExecutionStore) -> None:
    """get_last_execution() must return None for an unknown job_id.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    result = await store.get_last_execution("unknown-job")
    assert result is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_last_execution_returns_most_recent(store: ExecutionStore) -> None:
    """get_last_execution() must return the execution with the highest triggered_at.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    t0 = time.time()
    older = _make_execution(
        job_id="job-latest",
        correlation_id="old",
        triggered_at=t0 - 120,
    )
    newer = _make_execution(
        job_id="job-latest",
        correlation_id="new",
        triggered_at=t0,
    )
    # Insert older first, then newer to rule out insertion-order bias
    await store.record(older)
    await store.record(newer)

    result = await store.get_last_execution("job-latest")
    assert result is not None
    assert result.correlation_id == "new"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_last_execution_single_record(store: ExecutionStore) -> None:
    """get_last_execution() must return the only record when exactly one exists.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    execution = _make_execution(job_id="job-single", correlation_id="solo")
    await store.record(execution)

    result = await store.get_last_execution("job-single")
    assert result is not None
    assert result.correlation_id == "solo"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_last_execution_does_not_cross_jobs(store: ExecutionStore) -> None:
    """get_last_execution() must not return executions from a different job.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    t0 = time.time()
    await store.record(_make_execution(job_id="job-A", correlation_id="a1", triggered_at=t0 + 9999))
    await store.record(_make_execution(job_id="job-B", correlation_id="b1", triggered_at=t0))

    result = await store.get_last_execution("job-B")
    assert result is not None
    assert result.correlation_id == "b1"


# ---------------------------------------------------------------------------
# list_executions()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_executions_empty_for_unknown_job(store: ExecutionStore) -> None:
    """list_executions() must return an empty list for an unknown job_id.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    result = await store.list_executions("ghost-job")
    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_executions_newest_first(store: ExecutionStore) -> None:
    """list_executions() must return executions ordered newest triggered_at first.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    t0 = time.time()
    for i in range(3):
        await store.record(
            _make_execution(
                job_id="job-order",
                correlation_id=f"c{i}",
                triggered_at=t0 + i * 60,
            )
        )

    results = await store.list_executions("job-order")
    triggered_ats = [r.triggered_at for r in results]
    assert triggered_ats == sorted(triggered_ats, reverse=True)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_executions_respects_limit(store: ExecutionStore) -> None:
    """list_executions() must honour the limit parameter.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    t0 = time.time()
    for i in range(10):
        await store.record(
            _make_execution(
                job_id="job-limit",
                correlation_id=f"lim{i}",
                triggered_at=t0 + i,
            )
        )

    results = await store.list_executions("job-limit", limit=3)
    assert len(results) == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_executions_default_limit_is_20(store: ExecutionStore) -> None:
    """list_executions() default limit must cap results at 20 rows.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    t0 = time.time()
    for i in range(25):
        await store.record(
            _make_execution(
                job_id="job-default-limit",
                correlation_id=f"dl{i}",
                triggered_at=t0 + i,
            )
        )

    results = await store.list_executions("job-default-limit")
    assert len(results) == 20


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_executions_does_not_cross_jobs(store: ExecutionStore) -> None:
    """list_executions() must only return rows for the requested job_id.

    Args:
        store: Initialised ExecutionStore fixture.
    """
    t0 = time.time()
    await store.record(_make_execution(job_id="job-X", correlation_id="x1", triggered_at=t0))
    await store.record(_make_execution(job_id="job-Y", correlation_id="y1", triggered_at=t0))

    results = await store.list_executions("job-X")
    assert len(results) == 1
    assert results[0].correlation_id == "x1"


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_close_is_safe_to_call(tmp_path: Path) -> None:
    """close() must not raise, even on a freshly initialised store.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    db_path = tmp_path / "close_test.db"
    s = ExecutionStore(db_path=db_path)
    await s.init()
    await s.close()  # Must not raise
