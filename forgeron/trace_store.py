"""SkillTraceStore — SQLite accumulator for per-skill execution traces."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from common.config_loader import resolve_storage_dir
from forgeron.config import ForgeonConfig
from forgeron.models import SkillPatch, SkillTrace

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

    async def get_traces(
        self,
        skill_name: str,
        since_patch_id: str | None = None,
        limit: int = 100,
    ) -> list[SkillTrace]:
        """Retrieve recent traces for a skill.

        Args:
            skill_name: The skill to query.
            since_patch_id: If given, only return traces recorded after the
                patch with this ID was applied (i.e. ``patch_id == since_patch_id``).
                Pass ``None`` to retrieve traces from the beginning.
            limit: Maximum number of traces to return (most recent first).

        Returns:
            List of ``SkillTrace`` objects ordered by ``created_at`` descending.
        """
        async with self._session_factory() as session:
            stmt = select(SkillTrace).where(
                col(SkillTrace.skill_name) == skill_name
            )
            if since_patch_id is not None:
                stmt = stmt.where(col(SkillTrace.patch_id) == since_patch_id)
            stmt = stmt.order_by(col(SkillTrace.created_at).desc()).limit(limit)
            result = await session.exec(stmt)
            return list(result.all())

    async def error_rate(self, skill_name: str, window: int = 10) -> float:
        """Compute the error rate for the last *window* traces of a skill.

        The error rate is defined as the sum of ``tool_error_count`` divided by
        the sum of ``tool_call_count`` over the window.  Returns 0.0 when there
        are no traces or no tool calls.

        Args:
            skill_name: Skill to evaluate.
            window: Number of most recent traces to include.

        Returns:
            Error rate in [0.0, 1.0].
        """
        traces = await self.get_traces(skill_name, limit=window)
        total_calls = sum(t.tool_call_count for t in traces)
        if total_calls == 0:
            return 0.0
        total_errors = sum(t.tool_error_count for t in traces)
        return total_errors / total_calls

    async def should_analyze(
        self,
        skill_name: str,
        config: ForgeonConfig,
        redis_conn: object,
    ) -> bool:
        """Determine whether a full LLM analysis should be triggered.

        Returns True when ALL of the following hold:
        - At least ``config.min_traces_before_analysis`` traces are stored.
        - The rolling error rate is >= ``config.min_error_rate``.
        - The Redis cooldown key ``relais:skill:last_improved:{skill_name}``
          has expired (or was never set).

        Args:
            skill_name: Skill to evaluate.
            config: Loaded Forgeron configuration.
            redis_conn: Active Redis connection (for cooldown TTL check).

        Returns:
            True if analysis should be triggered.
        """
        traces = await self.get_traces(
            skill_name, limit=config.min_traces_before_analysis
        )
        if len(traces) < config.min_traces_before_analysis:
            logger.debug(
                "skill '%s': not enough traces (%d/%d)",
                skill_name,
                len(traces),
                config.min_traces_before_analysis,
            )
            return False

        rate = await self.error_rate(
            skill_name, window=config.min_traces_before_analysis
        )
        if rate < config.min_error_rate:
            logger.debug(
                "skill '%s': error_rate %.0f%% below threshold %.0f%%",
                skill_name,
                rate * 100,
                config.min_error_rate * 100,
            )
            return False

        cooldown_key = f"relais:skill:last_improved:{skill_name}"
        ttl = await redis_conn.ttl(cooldown_key)  # type: ignore[attr-defined]
        if ttl > 0:
            logger.debug(
                "skill '%s': cooldown active (%ds remaining)", skill_name, ttl
            )
            return False

        logger.info(
            "skill '%s': analysis triggered (traces=%d, error_rate=%.0f%%)",
            skill_name,
            len(traces),
            rate * 100,
        )
        return True

    async def close(self) -> None:
        """Dispose the async engine and release aiosqlite threads."""
        await self._engine.dispose()
