"""SkillTraceStore — SQLite accumulator for per-skill execution traces."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from common.config_loader import resolve_storage_dir
from forgeron.models import SkillTrace

logger = logging.getLogger(__name__)


class SkillTraceStore:
    """Persist and query skill execution traces.

    Wraps an async SQLite session backed by ``~/.relais/storage/forgeron.db``
    (separate from the Souvenir DB to avoid schema coupling).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialise the store and create the async engine.

        Args:
            db_path: Path to the SQLite file.  Defaults to
                ``~/.relais/storage/forgeron.db``.
        """
        self._db_path: Path = db_path or (resolve_storage_dir() / "forgeron.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{self._db_path}"
        self._engine = create_async_engine(url, echo=False)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def _create_tables(self) -> None:
        """Create all SQLModel-declared tables.

        For use in tests and initial setup (production should use Alembic).
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def add_trace(self, trace: SkillTrace) -> None:
        """Persist a new skill execution trace.

        Args:
            trace: The ``SkillTrace`` instance to insert.
        """
        async with self._session_factory() as session:
            session.add(trace)
            await session.commit()
        logger.debug(
            "Saved trace %s for skill '%s' (errors=%d/%d)",
            trace.id,
            trace.skill_name,
            trace.tool_error_count,
            trace.tool_call_count,
        )

    async def close(self) -> None:
        """Dispose the async engine and release aiosqlite threads."""
        await self._engine.dispose()
