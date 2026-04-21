"""Async SQLite-backed store for Horloger execution records."""

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from horloger.models import HorlogerExecution

logger = logging.getLogger(__name__)


class ExecutionStore:
    """Persist and query HorlogerExecution records in an async SQLite database.

    Uses SQLAlchemy async engine + SQLModel AsyncSession for all I/O so the
    asyncio event loop is never blocked.  The database file and its parent
    directories are created on first use inside ``init()``.

    ``expire_on_commit=False`` is set on the session factory so that ORM
    instances remain readable after ``commit()`` without triggering a lazy-load
    on a closed session (avoids ``DetachedInstanceError``).

    Example usage::

        store = ExecutionStore(db_path=Path("~/.relais/storage/horloger.db"))
        await store.init()
        await store.record(execution)
        last = await store.get_last_execution("my-job")
        await store.close()

    Attributes:
        _db_path: Resolved absolute path to the SQLite file.
        _engine: SQLAlchemy async engine (aiosqlite driver).
        _session_factory: Pre-configured async session factory.
    """

    def __init__(self, db_path: Path) -> None:
        """Initialise the store and build the async engine.

        The engine is created here but no I/O takes place until ``init()``
        is awaited.  The parent directory of ``db_path`` is created eagerly
        so that SQLite can open the file on first connection.

        Args:
            db_path: Absolute or relative path to the SQLite database file.
                Typically ``~/.relais/storage/horloger.db``.
        """
        self._db_path: Path = db_path
        url = f"sqlite+aiosqlite:///{self._db_path}"
        self._engine = create_async_engine(url, echo=False)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the database directory and table if they do not exist.

        Creates the parent directory of the database file, then ensures the
        ``horloger_executions`` table exists.  Safe to call multiple times
        (idempotent).  In production you may prefer Alembic migrations; this
        method is provided for lightweight deployments and test fixtures.

        Returns:
            None
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        logger.debug("ExecutionStore tables ensured at %s", self._db_path)

    async def record(self, execution: HorlogerExecution) -> None:
        """Persist a HorlogerExecution row to the database.

        Inserts a new row regardless of whether a row with the same
        ``correlation_id`` already exists.  Each firing of a job produces an
        independent row.  Because the session factory uses
        ``expire_on_commit=False``, attributes on ``execution`` remain readable
        after this call returns.

        Args:
            execution: The execution record to persist.  The ``id`` field is
                ignored on insert and filled by SQLite autoincrement.

        Returns:
            None
        """
        async with self._session_factory() as session:
            session.add(execution)
            await session.commit()
        logger.debug(
            "Recorded execution job_id=%s correlation_id=%s status=%s",
            execution.job_id,
            execution.correlation_id,
            execution.status,
        )

    async def get_last_execution(self, job_id: str) -> HorlogerExecution | None:
        """Return the most recent execution for a job, ordered by triggered_at.

        Queries ``horloger_executions`` for rows matching ``job_id``, orders
        by ``triggered_at DESC``, and returns the first result.

        Args:
            job_id: The job identifier to look up.

        Returns:
            The most recently triggered HorlogerExecution for the given
            ``job_id``, or ``None`` if no execution has been recorded yet.
        """
        query = (
            select(HorlogerExecution)
            .where(HorlogerExecution.job_id == job_id)
            .order_by(HorlogerExecution.triggered_at.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            result = await session.exec(query)
            row = result.first()
        return row

    async def list_executions(
        self, job_id: str, limit: int = 20
    ) -> list[HorlogerExecution]:
        """Return recent executions for a job, newest triggered_at first.

        Queries ``horloger_executions`` for all rows matching ``job_id``,
        ordered by ``triggered_at DESC``, capped at ``limit`` rows.

        Args:
            job_id: The job identifier to query.
            limit: Maximum number of rows to return.  Defaults to ``20``.

        Returns:
            A list of HorlogerExecution objects, newest first.  Returns an
            empty list when no executions exist for the given ``job_id``.
        """
        query = (
            select(HorlogerExecution)
            .where(HorlogerExecution.job_id == job_id)
            .order_by(HorlogerExecution.triggered_at.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            result = await session.exec(query)
            rows = result.all()
        return list(rows)

    async def close(self) -> None:
        """Release the async engine and its underlying aiosqlite connections.

        Must be called at service shutdown or at the end of a test to avoid
        thread leaks from aiosqlite's thread executor.

        Returns:
            None
        """
        await self._engine.dispose()
        logger.debug("ExecutionStore engine disposed")
