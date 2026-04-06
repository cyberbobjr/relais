"""SessionStore — SQLite accumulator for per-session intent patterns.

Shares the forgeron.db SQLite file with SkillTraceStore and SkillPatchStore
(different tables, same engine path).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from common.config_loader import resolve_storage_dir
from forgeron.models import SessionSummary, SkillProposal

logger = logging.getLogger(__name__)


class SessionStore:
    """Persist and query session intent patterns for skill auto-creation.

    Shares the forgeron.db SQLite file with SkillTraceStore and SkillPatchStore
    (different tables, same engine path).

    Args:
        db_path: Optional explicit path to the SQLite file. Defaults to
            ``resolve_storage_dir() / "forgeron.db"``.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (resolve_storage_dir() / "forgeron.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{self._db_path}"
        self._engine = create_async_engine(url, echo=False)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def _create_tables(self) -> None:
        """Create all SQLModel tables in the database (idempotent).

        Called once at startup by Forgeron's ``_load()`` after the engine is
        initialized.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def record_session(
        self,
        session_id: str,
        correlation_id: str,
        channel: str,
        sender_id: str,
        intent_label: str | None,
        user_content_preview: str,
    ) -> None:
        """Persist a session summary and update the SkillProposal aggregate.

        If ``intent_label`` is not None, creates or increments the corresponding
        ``SkillProposal`` row.

        Args:
            session_id: Session identifier from the archive envelope.
            correlation_id: Correlation ID of the archived turn.
            channel: Original channel (e.g. "discord").
            sender_id: Original sender_id.
            intent_label: Normalized intent extracted by IntentLabeler, or None.
            user_content_preview: First 200 chars of the user message.
        """
        summary = SessionSummary(
            session_id=session_id,
            correlation_id=correlation_id,
            channel=channel,
            sender_id=sender_id,
            intent_label=intent_label,
            user_content_preview=user_content_preview,
        )
        async with self._session_factory() as session:
            session.add(summary)
            await session.commit()

        if intent_label is not None:
            await self._upsert_proposal(intent_label, session_id)

    async def _upsert_proposal(self, intent_label: str, session_id: str) -> None:
        """Create or increment the SkillProposal for intent_label.

        Args:
            intent_label: Normalized intent label.
            session_id: Session ID to add to the representative list.
        """
        async with self._session_factory() as session:
            stmt = select(SkillProposal).where(col(SkillProposal.intent_label) == intent_label)
            result = await session.exec(stmt)
            proposal = result.first()

            if proposal is None:
                proposal = SkillProposal(
                    intent_label=intent_label,
                    candidate_name=intent_label.replace("_", "-"),
                    session_count=1,
                    representative_session_ids=json.dumps([session_id]),
                )
                session.add(proposal)
            else:
                proposal.session_count += 1
                existing: list[str] = json.loads(proposal.representative_session_ids)
                if session_id not in existing:
                    existing.append(session_id)
                    proposal.representative_session_ids = json.dumps(existing[-10:])  # keep last 10
            await session.commit()

    async def should_create(
        self,
        intent_label: str,
        min_sessions: int,
        redis_conn: object,
    ) -> bool:
        """Return True if a skill should be created for this intent_label.

        Conditions: ``session_count >= min_sessions`` AND status is "pending"
        AND no cooldown key is set in Redis.

        Args:
            intent_label: The intent label to evaluate.
            min_sessions: Minimum session count threshold.
            redis_conn: Active Redis connection for cooldown check.

        Returns:
            True if skill creation should be triggered.
        """
        async with self._session_factory() as session:
            stmt = select(SkillProposal).where(col(SkillProposal.intent_label) == intent_label)
            result = await session.exec(stmt)
            proposal = result.first()

        if proposal is None:
            return False
        if proposal.status != "pending":
            return False
        if proposal.session_count < min_sessions:
            logger.debug(
                "intent '%s': not enough sessions (%d/%d)",
                intent_label, proposal.session_count, min_sessions,
            )
            return False

        cooldown_key = f"relais:skill:creation_cooldown:{intent_label}"
        ttl = await redis_conn.ttl(cooldown_key)  # type: ignore[attr-defined]
        if ttl > 0:
            logger.debug("intent '%s': cooldown active (%ds remaining)", intent_label, ttl)
            return False

        return True

    async def get_proposal(self, intent_label: str) -> SkillProposal | None:
        """Fetch the SkillProposal for an intent_label.

        Args:
            intent_label: The intent label to look up.

        Returns:
            The matching SkillProposal, or None if not found.
        """
        async with self._session_factory() as session:
            stmt = select(SkillProposal).where(col(SkillProposal.intent_label) == intent_label)
            result = await session.exec(stmt)
            return result.first()

    async def get_representative_sessions(
        self, intent_label: str, limit: int = 5
    ) -> list[SessionSummary]:
        """Fetch the most recent sessions for an intent label.

        Args:
            intent_label: The intent label to query.
            limit: Maximum number of sessions to return.

        Returns:
            List of SessionSummary rows, most recent first.
        """
        async with self._session_factory() as session:
            stmt = (
                select(SessionSummary)
                .where(col(SessionSummary.intent_label) == intent_label)
                .order_by(col(SessionSummary.created_at).desc())
                .limit(limit)
            )
            result = await session.exec(stmt)
            return list(result.all())

    async def mark_created(self, intent_label: str, skill_name: str) -> None:
        """Mark a SkillProposal as created.

        Args:
            intent_label: The intent label to update.
            skill_name: The name of the skill that was created.
        """
        async with self._session_factory() as session:
            stmt = select(SkillProposal).where(col(SkillProposal.intent_label) == intent_label)
            result = await session.exec(stmt)
            proposal = result.first()
            if proposal:
                proposal.status = "created"
                proposal.created_skill_name = skill_name
                await session.commit()

    async def close(self) -> None:
        """Dispose the async engine and release all connections."""
        await self._engine.dispose()
