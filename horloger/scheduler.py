"""HORLOGER Scheduler: determines which jobs are due to fire on each tick.

The ``Scheduler`` class is the core of the HORLOGER brick. It uses ``croniter``
to compute the most recent scheduled time for each job and applies three guards:

1. **Disabled guard** — jobs with ``enabled=False`` are skipped.
2. **Catch-up guard** — jobs whose last scheduled time is older than
   ``catch_up_window_seconds`` are skipped to avoid trigger storms after downtime.
3. **Double-fire guard** — jobs triggered within ``min_interval_seconds`` are
   skipped to prevent duplicate firings within the same scheduling window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError
from croniter import croniter as Croniter

from horloger.job_model import JobSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class DueJob:
    """A job that is due to fire or be skipped.

    Attributes:
        spec: The ``JobSpec`` that became due.
        scheduled_for: Epoch timestamp of the scheduled firing time as returned
            by ``croniter.get_prev()``.
        skip_reason: If set, the job should be skipped; value is one of
            ``"skipped_catchup"``, ``"skipped_disabled"``, or
            ``"skipped_double_fire"``.  ``None`` means the job should trigger.
    """

    spec: JobSpec
    scheduled_for: float
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Determines which jobs are due to fire on each tick.

    The scheduler is **stateful**: it remembers the last trigger time for each
    job (via ``mark_triggered``) so it can prevent double-firing within the
    ``min_interval_seconds`` window.

    All timestamps are Unix epoch floats. The ``now`` parameter on
    ``get_due_jobs`` is injectable to make tests fully deterministic.

    Args:
        catch_up_window_seconds: Jobs whose most recent scheduled time is older
            than this many seconds ago are skipped (not triggered). Defaults to
            120 seconds.
        min_interval_seconds: Minimum seconds between two firings of the same
            job. Prevents double-triggering when the scheduler ticks faster than
            once per cron period. Defaults to 60 seconds.
    """

    def __init__(
        self,
        catch_up_window_seconds: int = 120,
        min_interval_seconds: int = 60,
    ) -> None:
        self._catch_up_window = catch_up_window_seconds
        self._min_interval = min_interval_seconds
        self._last_triggered: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_due_jobs(
        self, jobs: dict[str, JobSpec], now: float
    ) -> tuple[list[DueJob], list[DueJob]]:
        """Return ``(to_trigger, to_skip)``.

        Iterates over all jobs in the registry and classifies each one.
        A job is due if its most recent scheduled time falls within the
        catch-up window relative to ``now``. Due jobs are then checked
        against the double-fire guard and the enabled flag.

        Args:
            jobs: Current job registry snapshot ``{job_id: JobSpec}``.
            now: Current epoch time (injectable for testing).

        Returns:
            A tuple ``(to_trigger, to_skip)`` where each element is a list
            of ``DueJob`` instances.
        """
        to_trigger: list[DueJob] = []
        to_skip: list[DueJob] = []

        for job_id, spec in jobs.items():
            scheduled_for = self._get_prev(spec, now)
            if scheduled_for is None:
                # Could not compute a scheduled time — skip silently.
                continue

            # Guard 1: not yet due (scheduled_for is in the future)
            if scheduled_for > now:
                continue

            # Guard 2: catch-up window — too old, skip to avoid storm
            if scheduled_for < now - self._catch_up_window:
                to_skip.append(DueJob(spec=spec, scheduled_for=scheduled_for, skip_reason="skipped_catchup"))
                continue

            # Guard 3: disabled
            if not spec.enabled:
                to_skip.append(DueJob(spec=spec, scheduled_for=scheduled_for, skip_reason="skipped_disabled"))
                continue

            # Guard 4: double-fire — was recently triggered
            last = self._last_triggered.get(job_id)
            if last is not None and (now - last) < self._min_interval:
                to_skip.append(DueJob(spec=spec, scheduled_for=scheduled_for, skip_reason="skipped_double_fire"))
                continue

            to_trigger.append(DueJob(spec=spec, scheduled_for=scheduled_for))

        return to_trigger, to_skip

    def mark_triggered(self, job_id: str, at: float) -> None:
        """Record that a job was triggered at the given timestamp.

        If the job was previously recorded, the timestamp is always
        overwritten with the new value so the guard reflects the most
        recent firing.

        Args:
            job_id: The job identifier.
            at: The epoch timestamp when the job was triggered.
        """
        self._last_triggered[job_id] = at

    def clear_job(self, job_id: str) -> None:
        """Remove a job from the trigger history.

        Called when a job is deleted from the registry so stale history
        does not block future jobs with the same id.  Safe to call with
        an unknown ``job_id`` — it is a no-op in that case.

        Args:
            job_id: The job identifier to remove.
        """
        self._last_triggered.pop(job_id, None)

    def sync_jobs(self, current_ids: set[str]) -> None:
        """Prune trigger history for jobs that no longer exist.

        Removes entries from ``_last_triggered`` whose job IDs are not present
        in ``current_ids``.  Call this after every registry reload to prevent
        the dict from growing unboundedly as jobs are created and deleted.

        Args:
            current_ids: Set of currently active job IDs.
        """
        stale = [jid for jid in self._last_triggered if jid not in current_ids]
        for jid in stale:
            del self._last_triggered[jid]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_prev(self, spec: JobSpec, now: float) -> float | None:
        """Return the most recent scheduled time before ``now``.

        Converts ``now`` to the job's declared timezone before passing it to
        ``croniter`` so that cron expressions like ``"0 9 * * *"`` fire at
        09:00 in the job's local time, not in UTC.

        Args:
            spec: The ``JobSpec`` whose schedule to evaluate.
            now: Current epoch time.

        Returns:
            The epoch timestamp of the most recent past firing, or ``None``
            if the schedule is invalid or the timezone is unknown.
        """
        try:
            tz = ZoneInfo(spec.timezone)
        except ZoneInfoNotFoundError:
            logger.debug("Unknown timezone %r for job %s — skipping", spec.timezone, spec.id)
            return None

        try:
            # Convert 'now' epoch to a timezone-aware datetime for croniter.
            now_dt = datetime.fromtimestamp(now, tz=tz)
            cron = Croniter(spec.schedule, now_dt)
            prev: float = cron.get_prev(float)
            return prev
        except (CroniterBadCronError, ValueError) as exc:
            logger.debug("Invalid schedule %r for job %s: %s", spec.schedule, spec.id, exc)
            return None
