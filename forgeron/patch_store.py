"""SkillPatchStore — SQLite store for versioned skill patches."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from common.config_loader import resolve_storage_dir
from forgeron.models import SkillPatch

logger = logging.getLogger(__name__)


class SkillPatchStore:
    """Persist and query skill patches.

    Shares the same ``forgeron.db`` SQLite file as ``SkillTraceStore``.  The
    caller is responsible for passing a consistent *db_path* to both stores.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialise the store.

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
        """Create all SQLModel-declared tables (for tests / initial setup)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def save(self, patch: SkillPatch) -> None:
        """Persist a new or updated patch record.

        Args:
            patch: The ``SkillPatch`` instance to upsert.
        """
        async with self._session_factory() as session:
            session.add(patch)
            await session.commit()
        logger.debug("Saved patch %s for skill '%s'", patch.id, patch.skill_name)

    async def get_applied_patch(self, skill_name: str) -> SkillPatch | None:
        """Return the currently applied (non-rolled-back) patch for a skill.

        Args:
            skill_name: Skill to query.

        Returns:
            The most recent ``SkillPatch`` with status ``"applied"`` or
            ``"validated"``, or ``None`` if no active patch exists.
        """
        async with self._session_factory() as session:
            stmt = (
                select(SkillPatch)
                .where(col(SkillPatch.skill_name) == skill_name)
                .where(col(SkillPatch.status).in_(["applied", "validated"]))
                .order_by(col(SkillPatch.applied_at).desc())
                .limit(1)
            )
            result = await session.exec(stmt)
            return result.first()

    async def mark_applied(self, patch: SkillPatch) -> None:
        """Update a patch status to ``"applied"`` with the current timestamp.

        Args:
            patch: The ``SkillPatch`` to mark as applied.
        """
        patch.status = "applied"
        patch.applied_at = time.time()
        await self.save(patch)

    async def mark_rolled_back(self, patch: SkillPatch) -> None:
        """Update a patch status to ``"rolled_back"`` with the current timestamp.

        Args:
            patch: The ``SkillPatch`` to mark as rolled back.
        """
        patch.status = "rolled_back"
        patch.rolled_back_at = time.time()
        await self.save(patch)

    async def mark_validated(self, patch: SkillPatch) -> None:
        """Update a patch status to ``"validated"``.

        Args:
            patch: The ``SkillPatch`` to mark as validated.
        """
        patch.status = "validated"
        await self.save(patch)

    async def close(self) -> None:
        """Dispose the async engine."""
        await self._engine.dispose()
