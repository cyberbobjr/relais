"""HORLOGER brick — CRON-based job scheduler.

Functional role
---------------
Reads YAML job files from ``$RELAIS_HOME/config/horloger/jobs/``, checks on
every tick which jobs are due, and publishes trigger envelopes to
``relais:messages:incoming:horloger`` so the rest of the RELAIS pipeline
can process them like normal user messages.

Technical overview
------------------
``Horloger`` extends :class:`~common.brick_base.BrickBase` as a
**producer-only** brick: ``stream_specs()`` returns an empty list so no
consumer loops are launched.  The brick's actual work runs inside
``_tick_loop()``, a background task started from ``on_startup()``.

Processing flow (per tick)
--------------------------
1. ``_registry.reload()`` — re-scan the jobs directory for changes.
2. ``_scheduler.get_due_jobs(jobs, now)`` — classify jobs into
   *to_trigger* and *to_skip*.
3. For each skipped job → ``_store.record(… status="skipped_disabled")``
   (or ``"skipped_catchup"`` if the scheduler returned it for that reason;
   in practice the scheduler doesn't distinguish the two skip causes at the
   API level, so we always use ``"skipped_disabled"`` for skips).
4. For each triggerable job:
   a. Build an Envelope via ``build_trigger_envelope()``.
   b. ``redis.xadd(STREAM_INCOMING_HORLOGER, {"payload": envelope.to_json()})``.
   c. ``_scheduler.mark_triggered(job_id, now)`` (double-fire guard).
   d. ``_store.record(… status="triggered")``.
   On ``xadd`` failure: ``_store.record(… status="publish_failed")``.

Configuration (``horloger.yaml``)
----------------------------------
``tick_interval_seconds`` — seconds between scheduler ticks (default 30).
``catch_up_window_seconds`` — jobs older than this are skipped (default 120).
``jobs_dir`` — path relative to ``RELAIS_HOME`` for job YAML files.
``db_path`` — path relative to ``RELAIS_HOME`` for the SQLite trace DB.

Redis channels
--------------
Produced:
  - relais:messages:incoming:horloger  (trigger envelopes)
  - relais:logs                        (operational log entries via BrickBase)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from common.brick_base import BrickBase, StreamSpec
from common.config_loader import get_relais_home, resolve_config_path
from common.shutdown import GracefulShutdown
from common.streams import STREAM_INCOMING_HORLOGER
from horloger.envelope_builder import build_trigger_envelope
from horloger.execution_store import ExecutionStore
from horloger.job_registry import JobRegistry
from horloger.models import HorlogerExecution
from horloger.scheduler import DueJob, Scheduler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loader helper (module-level so tests can patch it)
# ---------------------------------------------------------------------------


def load_horloger_config() -> dict:
    """Load and return the parsed horloger.yaml configuration.

    Resolves the file using the RELAIS config cascade
    (``~/.relais/config/horloger.yaml`` → project ``config/horloger.yaml.default``).

    Returns:
        Parsed YAML content as a plain dict.

    Raises:
        FileNotFoundError: If no horloger.yaml is found in the cascade.
        yaml.YAMLError: If the file is not valid YAML.
    """
    path = resolve_config_path("horloger.yaml")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Horloger brick
# ---------------------------------------------------------------------------


class Horloger(BrickBase):
    """Producer-only CRON scheduler brick.

    Reads job specs from a YAML directory, checks which jobs are due on
    every tick, and publishes trigger envelopes to
    ``relais:messages:incoming:horloger``.

    The brick does **not** consume any Redis stream — ``stream_specs()``
    returns ``[]`` — so BrickBase waits on ``shutdown_event`` rather than
    on stream-loop tasks.
    """

    def __init__(self) -> None:
        """Initialise brick infrastructure and load configuration.

        Calls ``super().__init__("horloger")`` then immediately invokes
        ``_load()`` to parse ``horloger.yaml`` and build the registry /
        scheduler instances.  No I/O takes place here — ``on_startup()``
        handles async initialisation.
        """
        super().__init__("horloger")
        # Attributes populated by _load()
        self._tick_interval: int = 30
        self._catch_up_window: int = 120
        self._jobs_dir: Path = Path()
        self._db_path: Path = Path()
        self._registry: JobRegistry
        self._scheduler: Scheduler
        self._store: ExecutionStore
        self._load()

    # ------------------------------------------------------------------
    # BrickBase abstract interface
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load horloger.yaml and initialise the registry and scheduler.

        Reads ``tick_interval_seconds``, ``catch_up_window_seconds``,
        ``jobs_dir``, and ``db_path`` from the config cascade.  All path
        values are resolved relative to ``RELAIS_HOME`` unless they are
        already absolute.

        Raises:
            FileNotFoundError: If ``horloger.yaml`` is not found.
            yaml.YAMLError: If the YAML is malformed.
        """
        cfg = load_horloger_config()
        home = get_relais_home()

        self._tick_interval = int(cfg.get("tick_interval_seconds", 30))
        self._catch_up_window = int(cfg.get("catch_up_window_seconds", 120))

        def _rp(raw: str) -> Path:
            p = Path(raw)
            return p if p.is_absolute() else home / p

        self._jobs_dir = _rp(cfg.get("jobs_dir", "config/horloger/jobs"))
        self._db_path = _rp(cfg.get("db_path", "storage/horloger.db"))

        # Ensure the jobs directory exists so the registry scanner doesn't fail.
        self._jobs_dir.mkdir(parents=True, exist_ok=True)

        self._registry = JobRegistry(jobs_dir=self._jobs_dir)
        self._scheduler = Scheduler(
            catch_up_window_seconds=self._catch_up_window,
            min_interval_seconds=self._tick_interval,
        )
        self._store = ExecutionStore(db_path=self._db_path)

    def stream_specs(self) -> list[StreamSpec]:
        """Return empty list — Horloger is producer-only.

        BrickBase will wait on ``shutdown_event`` instead of running
        consumer loops when this returns ``[]``.

        Returns:
            An empty list.
        """
        return []

    def _create_shutdown(self) -> GracefulShutdown:
        """Return a fresh GracefulShutdown instance.

        Overridden so tests can patch ``horloger.main.GracefulShutdown``
        and inject a controllable shutdown event without touching signal
        handlers.

        Returns:
            A new :class:`~common.shutdown.GracefulShutdown` instance.
        """
        return GracefulShutdown()

    # ------------------------------------------------------------------
    # BrickBase lifecycle hooks
    # ------------------------------------------------------------------

    async def on_startup(self, redis: Any) -> None:
        """Initialise the execution store and start the tick loop.

        Creates the SQLite schema, loads all jobs from disk, then launches
        ``_tick_loop`` as a fire-and-forget background task so that
        BrickBase can return from ``on_startup`` immediately.

        Args:
            redis: Live async Redis connection passed by BrickBase.
        """
        await self._store.init()
        self._registry.load_all()
        asyncio.create_task(self._tick_loop(redis), name="horloger:tick_loop")

    async def on_shutdown(self) -> None:
        """Release the SQLite engine after the brick stops.

        Called by BrickBase after the shutdown event fires and all
        background tasks have been cancelled.
        """
        await self._store.close()

    # ------------------------------------------------------------------
    # Hot-reload
    # ------------------------------------------------------------------

    def _config_watch_paths(self) -> list[Path]:
        """Return the jobs directory so watchfiles triggers hot-reload on changes.

        Returns:
            A list containing only the jobs directory path.
        """
        return [self._jobs_dir]

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    def _make_execution(
        self,
        due: DueJob,
        correlation_id: str,
        now: float,
        status: str,
        error: str | None = None,
    ) -> HorlogerExecution:
        """Build a HorlogerExecution record from a DueJob.

        Args:
            due: The job being recorded.
            correlation_id: Correlation ID to assign to this execution row.
            now: Epoch timestamp of the current tick (``triggered_at``).
            status: One of ``"triggered"``, ``"publish_failed"``,
                ``"skipped_disabled"``, ``"skipped_catchup"``, or
                ``"skipped_double_fire"``.
            error: Optional error message for ``"publish_failed"`` rows.

        Returns:
            An unsaved :class:`~horloger.models.HorlogerExecution` instance.
        """
        return HorlogerExecution(
            correlation_id=correlation_id,
            job_id=due.spec.id,
            owner_id=due.spec.owner_id,
            channel=due.spec.channel,
            prompt=due.spec.prompt,
            scheduled_for=due.scheduled_for,
            triggered_at=now,
            status=status,
            error=error,
        )

    async def _tick_loop(self, redis: Any) -> None:
        """Run the scheduler loop until shutdown is requested.

        Calls ``_tick(redis)`` once per ``tick_interval_seconds``.  Uses
        ``asyncio.wait_for`` with a timeout so the loop wakes immediately
        when the shutdown event fires rather than sleeping through the
        full interval.

        Args:
            redis: Live async Redis connection.
        """
        shutdown_event: asyncio.Event = getattr(
            self, "_shutdown_event",
            asyncio.Event(),  # fallback for tests that bypass BrickBase.start()
        )

        while not shutdown_event.is_set():
            try:
                await self._tick(redis)
            except Exception:
                self._logger.exception("Horloger tick error")
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=self._tick_interval,
                )
                break  # shutdown requested — exit loop
            except asyncio.TimeoutError:
                pass  # normal — just means it's time for the next tick

    async def _tick(self, redis: Any) -> None:
        """Check for due jobs and publish trigger envelopes.

        On each invocation:
        1. Reload the job registry from disk and sync scheduler state.
        2. Ask the scheduler which jobs are due.
        3. Record skipped jobs in the execution store.
        4. Publish trigger envelopes for due jobs; record each outcome.

        Args:
            redis: Live async Redis connection used for ``XADD``.
        """
        jobs = self._registry.reload()
        self._scheduler.sync_jobs(set(jobs.keys()))
        now = time.time()
        to_trigger, to_skip = self._scheduler.get_due_jobs(jobs, now)

        # --- Record skipped jobs ---
        for due in to_skip:
            await self._store.record(
                self._make_execution(due, str(uuid.uuid4()), now, due.skip_reason or "skipped_disabled")
            )

        # --- Trigger due jobs ---
        for due in to_trigger:
            envelope = build_trigger_envelope(due.spec, due.scheduled_for)
            try:
                await redis.xadd(
                    STREAM_INCOMING_HORLOGER,
                    {"payload": envelope.to_json()},
                )
                self._scheduler.mark_triggered(due.spec.id, now)
                await self._store.record(
                    self._make_execution(due, envelope.correlation_id, now, "triggered")
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.error(
                    "Horloger: failed to publish job %s — %s",
                    due.spec.id,
                    exc,
                )
                await self._store.record(
                    self._make_execution(due, envelope.correlation_id, now, "publish_failed", str(exc))
                )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio

    asyncio.run(Horloger().start())
