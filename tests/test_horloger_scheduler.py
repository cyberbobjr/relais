"""Tests for horloger/scheduler.py — Scheduler class.

All tests use injectable ``now`` float parameter to avoid real-clock dependencies.
"""

from __future__ import annotations

import math
import time
from zoneinfo import ZoneInfo

import pytest

from horloger.job_model import JobSpec
from horloger.scheduler import DueJob, Scheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    job_id: str = "test-job",
    schedule: str = "* * * * *",
    enabled: bool = True,
    timezone: str = "UTC",
) -> JobSpec:
    """Create a minimal JobSpec for testing."""
    return JobSpec(
        id=job_id,
        owner_id="usr_test",
        schedule=schedule,
        channel="discord",
        prompt="Hello world",
        enabled=enabled,
        created_at="2026-01-01T00:00:00Z",
        description="Test job",
        timezone=timezone,
    )


def _minute_boundary(now: float) -> float:
    """Return the start of the current minute (floor to 60s)."""
    return math.floor(now / 60) * 60


# ---------------------------------------------------------------------------
# Test: empty job registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_jobs_returns_empty() -> None:
    """get_due_jobs with an empty registry returns two empty lists."""
    scheduler = Scheduler()
    now = time.time()
    to_trigger, to_skip = scheduler.get_due_jobs({}, now)
    assert to_trigger == []
    assert to_skip == []


# ---------------------------------------------------------------------------
# Test: basic due job triggers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_due_job_triggers_when_cron_matches() -> None:
    """A job with '* * * * *' is due within the current minute."""
    scheduler = Scheduler()
    job = _make_job()
    # now is placed 30s after the minute boundary so get_prev returns boundary
    boundary = _minute_boundary(time.time())
    now = boundary + 30.0  # 30s into the current minute

    to_trigger, to_skip = scheduler.get_due_jobs({"test-job": job}, now)

    assert len(to_trigger) == 1
    assert to_trigger[0].spec is job
    # scheduled_for should be the minute boundary (get_prev result)
    assert to_trigger[0].scheduled_for == pytest.approx(boundary, abs=1.0)
    assert to_skip == []


# ---------------------------------------------------------------------------
# Test: disabled job is skipped
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_disabled_job_is_skipped() -> None:
    """A job with enabled=False goes to to_skip, not to_trigger."""
    scheduler = Scheduler()
    job = _make_job(enabled=False)
    boundary = _minute_boundary(time.time())
    now = boundary + 30.0

    to_trigger, to_skip = scheduler.get_due_jobs({"test-job": job}, now)

    assert to_trigger == []
    assert len(to_skip) == 1
    assert to_skip[0].spec is job


# ---------------------------------------------------------------------------
# Test: catch-up window — old jobs are skipped
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_catchup_job_is_skipped() -> None:
    """A job whose scheduled_for is 200s ago (> 120s window) goes to to_skip."""
    scheduler = Scheduler(catch_up_window_seconds=120)
    job = _make_job()
    # Place now 200s after a minute boundary so get_prev gives boundary 200s ago
    boundary = _minute_boundary(time.time()) - 120  # move boundary 120s in past
    now = boundary + 200.0  # 200s after that boundary

    to_trigger, to_skip = scheduler.get_due_jobs({"test-job": job}, now)

    # The most recent get_prev will be within the last minute, which IS within window.
    # We need a scenario where get_prev is older than catch_up_window_seconds.
    # Use a schedule that fires less often so the last occurrence is truly old.
    # "0 0 1 1 *" = once a year (January 1st at midnight).
    # With now = current time, the last firing was Jan 1 00:00 UTC.
    # That is definitely > 120 seconds ago.
    past_job = _make_job(schedule="0 0 1 1 *")  # yearly
    now2 = time.time()
    to_trigger2, to_skip2 = scheduler.get_due_jobs({"yearly-job": past_job}, now2)

    assert to_trigger2 == []
    assert len(to_skip2) == 1
    assert to_skip2[0].spec is past_job


# ---------------------------------------------------------------------------
# Test: recent trigger prevents double-fire
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recent_trigger_prevents_double_fire() -> None:
    """After mark_triggered(job_id, now-30), the same job is not in to_trigger."""
    scheduler = Scheduler(min_interval_seconds=60)
    job = _make_job()
    boundary = _minute_boundary(time.time())
    now = boundary + 30.0

    scheduler.mark_triggered("test-job", now - 30.0)

    to_trigger, to_skip = scheduler.get_due_jobs({"test-job": job}, now)

    assert to_trigger == []
    assert len(to_skip) == 1


# ---------------------------------------------------------------------------
# Test: trigger allowed after min_interval
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trigger_after_min_interval_allowed() -> None:
    """After mark_triggered(job_id, now-90), the job IS in to_trigger (90 > 60)."""
    scheduler = Scheduler(min_interval_seconds=60)
    job = _make_job()
    boundary = _minute_boundary(time.time())
    now = boundary + 30.0

    # Triggered 90 seconds ago — outside the 60s min_interval
    scheduler.mark_triggered("test-job", now - 90.0)

    to_trigger, to_skip = scheduler.get_due_jobs({"test-job": job}, now)

    assert len(to_trigger) == 1
    assert to_skip == []


# ---------------------------------------------------------------------------
# Test: clear_job removes trigger history
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_clear_job_removes_history() -> None:
    """After mark_triggered then clear_job, the job can fire again."""
    scheduler = Scheduler(min_interval_seconds=60)
    job = _make_job()
    boundary = _minute_boundary(time.time())
    now = boundary + 30.0

    # Mark triggered recently — should be blocked
    scheduler.mark_triggered("test-job", now - 10.0)
    to_trigger_blocked, _ = scheduler.get_due_jobs({"test-job": job}, now)
    assert to_trigger_blocked == []

    # Clear the history — should be allowed again
    scheduler.clear_job("test-job")
    to_trigger, to_skip = scheduler.get_due_jobs({"test-job": job}, now)

    assert len(to_trigger) == 1
    assert to_skip == []


# ---------------------------------------------------------------------------
# Test: timezone-aware scheduling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_prev_uses_timezone() -> None:
    """A job with timezone='America/New_York' fires at the correct UTC-based time.

    We construct a now value that is exactly at a minute boundary in UTC,
    then verify the scheduler treats the job as due (or not) correctly
    regardless of the local clock timezone by checking that both
    UTC-timezone job and NY-timezone job are treated consistently —
    the cron expression is evaluated relative to the job's own timezone.
    """
    scheduler = Scheduler(catch_up_window_seconds=120)

    utc_job = _make_job(job_id="utc-job", schedule="* * * * *", timezone="UTC")
    ny_job = _make_job(
        job_id="ny-job", schedule="* * * * *", timezone="America/New_York"
    )

    # Use a now that is 30s past a minute boundary
    boundary = _minute_boundary(time.time())
    now = boundary + 30.0

    jobs = {"utc-job": utc_job, "ny-job": ny_job}
    to_trigger, to_skip = scheduler.get_due_jobs(jobs, now)

    # Both jobs use "* * * * *" so both should fire every minute regardless of tz
    triggered_ids = {due.spec.id for due in to_trigger}
    assert "utc-job" in triggered_ids
    assert "ny-job" in triggered_ids
    assert to_skip == []


# ---------------------------------------------------------------------------
# Test: multiple jobs, mixed results
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mixed_jobs_split_correctly() -> None:
    """Enabled+due jobs trigger; disabled jobs skip; both can coexist."""
    scheduler = Scheduler()
    enabled_job = _make_job(job_id="enabled", enabled=True)
    disabled_job = _make_job(job_id="disabled", enabled=False)

    boundary = _minute_boundary(time.time())
    now = boundary + 30.0

    jobs = {"enabled": enabled_job, "disabled": disabled_job}
    to_trigger, to_skip = scheduler.get_due_jobs(jobs, now)

    triggered_ids = {d.spec.id for d in to_trigger}
    skipped_ids = {d.spec.id for d in to_skip}

    assert triggered_ids == {"enabled"}
    assert skipped_ids == {"disabled"}


# ---------------------------------------------------------------------------
# Test: DueJob dataclass structure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_due_job_has_correct_fields() -> None:
    """DueJob exposes spec and scheduled_for fields."""
    job = _make_job()
    due = DueJob(spec=job, scheduled_for=1234567890.0)

    assert due.spec is job
    assert due.scheduled_for == 1234567890.0


# ---------------------------------------------------------------------------
# Test: mark_triggered is idempotent (overwriting with newer time)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mark_triggered_overwrites_with_latest() -> None:
    """Calling mark_triggered twice keeps the most recent timestamp."""
    scheduler = Scheduler(min_interval_seconds=60)
    job = _make_job()
    boundary = _minute_boundary(time.time())
    now = boundary + 30.0

    # First trigger: 90s ago (outside window — would normally allow firing)
    scheduler.mark_triggered("test-job", now - 90.0)
    # Second trigger: 10s ago (inside window — should block firing)
    scheduler.mark_triggered("test-job", now - 10.0)

    to_trigger, to_skip = scheduler.get_due_jobs({"test-job": job}, now)
    # Should be blocked by the second (more recent) trigger
    assert to_trigger == []
    assert len(to_skip) == 1


# ---------------------------------------------------------------------------
# Test: clear_job on unknown id is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_clear_job_unknown_id_is_noop() -> None:
    """clear_job on an unknown job id does not raise."""
    scheduler = Scheduler()
    scheduler.clear_job("nonexistent-job")  # must not raise


# ---------------------------------------------------------------------------
# Test: invalid timezone returns None from _get_prev → job silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invalid_timezone_job_is_silently_skipped() -> None:
    """A job with an invalid/unknown timezone is silently skipped (not crashed)."""
    scheduler = Scheduler()
    job = _make_job(timezone="Invalid/Timezone_XYZ")
    boundary = _minute_boundary(time.time())
    now = boundary + 30.0

    to_trigger, to_skip = scheduler.get_due_jobs({"bad-tz-job": job}, now)

    # Must not raise; job simply does not appear in either list
    assert to_trigger == []
    assert to_skip == []


# ---------------------------------------------------------------------------
# Test: scheduled_for in future is not due (guard 1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_job_not_yet_due_is_ignored() -> None:
    """A job whose next cron time is in the future is neither triggered nor skipped.

    We construct a 'now' just *before* a minute boundary so the most recent
    get_prev is the boundary 60s ago — which with a tight catch_up_window
    makes the job old and skipped.  We instead rely on a yearly schedule and
    set now to a moment just before the next yearly tick, which is deeply in
    the future; get_prev will be Jan 1 of the current or past year.

    Actually the easier approach: use a far-future-only schedule. Since croniter
    always returns a past time for get_prev, this edge cannot be triggered with
    a valid schedule. Instead we test the boundary by mocking _get_prev directly.
    """
    # We test that if scheduled_for > now, it is skipped from both lists.
    # We achieve this by subclassing Scheduler to inject a future scheduled_for.
    class FutureScheduler(Scheduler):
        def _get_prev(self, spec: JobSpec, now: float) -> float | None:  # type: ignore[override]
            return now + 100.0  # 100s in the future

    scheduler = FutureScheduler()
    job = _make_job()
    now = time.time()

    to_trigger, to_skip = scheduler.get_due_jobs({"test-job": job}, now)

    # scheduled_for > now → silently ignored (not in either list)
    assert to_trigger == []
    assert to_skip == []


# ---------------------------------------------------------------------------
# Test: croniter exception → _get_prev returns None → job silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_croniter_exception_silently_skipped() -> None:
    """If croniter raises during get_prev, the job is silently skipped."""
    class BrokenCronScheduler(Scheduler):
        def _get_prev(self, spec: JobSpec, now: float) -> float | None:  # type: ignore[override]
            return None  # simulates the Exception branch in _get_prev

    scheduler = BrokenCronScheduler()
    job = _make_job()
    now = time.time()

    to_trigger, to_skip = scheduler.get_due_jobs({"test-job": job}, now)

    assert to_trigger == []
    assert to_skip == []


# ---------------------------------------------------------------------------
# Test: _get_prev raises internally → returns None (croniter exception branch)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_prev_croniter_exception_returns_none() -> None:
    """_get_prev returns None when croniter.get_prev raises internally.

    We create a JobSpec with a valid timezone but patch croniter to raise,
    exercising the inner try/except branch in _get_prev.
    """
    from unittest.mock import patch

    scheduler = Scheduler()
    job = _make_job(timezone="UTC")
    now = time.time()

    with patch("horloger.scheduler.Croniter") as mock_croniter_cls:
        mock_instance = mock_croniter_cls.return_value
        mock_instance.get_prev.side_effect = ValueError("simulated croniter error")
        result = scheduler._get_prev(job, now)

    assert result is None
