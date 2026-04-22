"""Long-term memory store via SQLModel + async SQLite."""

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from common.config_loader import resolve_storage_dir
from common.contexts import CTX_ATELIER, AtelierCtx
from souvenir.models import ArchivedMessage

if TYPE_CHECKING:
    from common.envelope import Envelope


logger = logging.getLogger(__name__)

class LongTermStore:
    """Long-term memory store in SQLite (~/.relais/memory.db).

    Uses SQLModel + SQLAlchemy async for all I/O operations to avoid blocking
    the asyncio loop. The schema is managed by Alembic in production; in tests,
    ``_create_tables()`` can be called directly.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialise the store and create the async engine.

        Args:
            db_path: Path to the SQLite file. Defaults to
                ``~/.relais/storage/memory.db``.
        """
        self._db_path: Path = db_path or (resolve_storage_dir() / "memory.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{self._db_path}"
        self._engine = create_async_engine(url, echo=False)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def _create_tables(self) -> None:
        """Create all tables declared in SQLModel.metadata.

        Reserved for tests and non-Alembic initialisation. In production,
        use ``alembic upgrade head``.

        Returns:
            None
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def archive(
        self,
        envelope: "Envelope",
        messages_raw: list[dict],
    ) -> None:
        """Archive a complete agent turn (upsert on correlation_id).

        Stores one row in ``archived_messages`` per ``correlation_id``.
        If a row already exists for this ``correlation_id``, it is updated.

        Args:
            envelope: The outgoing message envelope (assistant response).
            messages_raw: JSON-serialisable list of all LangChain messages for
                the turn, as produced by
                ``atelier.message_serializer.serialize_messages()``.
        """
        now = time.time()
        atelier_ctx: AtelierCtx = envelope.context.get(CTX_ATELIER, {})
        user_content = atelier_ctx.get("user_message", "")
        assistant_content = envelope.content
        messages_raw_json = json.dumps(messages_raw)

        stmt = (
            sqlite_insert(ArchivedMessage)
            .values(
                session_id=envelope.session_id,
                sender_id=envelope.sender_id,
                channel=envelope.channel,
                user_content=user_content,
                assistant_content=assistant_content,
                messages_raw=messages_raw_json,
                correlation_id=envelope.correlation_id,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=["correlation_id"],
                set_={
                    "user_content": user_content,
                    "assistant_content": assistant_content,
                    "messages_raw": messages_raw_json,
                    "created_at": now,
                },
            )
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()
        logger.debug("Archived turn for session=%s correlation=%s", envelope.session_id, envelope.correlation_id)

    async def clear_session(self, session_id: str, user_id: str | None = None) -> int:
        """Delete all archived messages for a SQLite session and clear the
        LangGraph checkpointer thread for the user.

        The ``archived_messages`` table is cleaned for ``session_id``.
        If ``user_id`` is provided, the corresponding thread is also deleted
        from the ``AsyncSqliteSaver`` checkpointer (``checkpoints.db``).

        Args:
            session_id: Session identifier to clear (archived_messages).
            user_id: Stable user identifier (checkpointer ``thread_id``).
                If ``None``, the checkpointer is not modified.

        Returns:
            Number of rows deleted from ``archived_messages``.
        """
        from sqlalchemy import delete as sa_delete

        stmt = sa_delete(ArchivedMessage).where(
            ArchivedMessage.session_id == session_id
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()

        deleted_count: int = result.rowcount
        logger.info(
            "Cleared %d archived messages for session=%s",
            deleted_count,
            session_id,
        )

        if user_id:
            thread_id = f"{user_id}:{session_id}"
            await self._clear_checkpointer_thread(thread_id)

        return deleted_count

    async def _clear_checkpointer_thread(self, thread_id: str) -> None:
        """Delete the LangGraph checkpointer thread for ``thread_id``.

        Opens ``checkpoints.db`` via ``AsyncSqliteSaver`` and calls
        ``adelete_thread(thread_id)`` to erase all LangGraph history associated
        with this thread.  Since Phase 4b, threads are keyed as
        ``"{user_id}:{session_id}"`` — the caller is responsible for passing
        the composite key.

        Args:
            thread_id: Composite thread identifier
                (``"{user_id}:{session_id}"`` format, as generated by Atelier).
        """
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as exc:
            raise ImportError(
                "langgraph-checkpoint-sqlite is required to clear the checkpointer"
            ) from exc

        checkpoints_db = str(self._db_path.parent / "checkpoints.db")
        async with AsyncSqliteSaver.from_conn_string(checkpoints_db) as checkpointer:
            await checkpointer.adelete_thread(thread_id)
        logger.info("Cleared LangGraph checkpointer thread for thread_id=%s", thread_id)

    async def list_sessions(self, user_id: str, limit: int = 20) -> list[dict]:
        """Return a summary of archived sessions for a user.

        Queries ``archived_messages`` grouped by ``session_id`` where
        ``sender_id`` ends with ``:{user_id}`` (exact suffix match).
        Sender IDs follow the ``{channel}:{user_id}`` convention stamped by
        Portail, so an exact suffix filter is both correct and injection-safe.
        Results are ordered by the most-recent activity first.

        The preview is derived via a correlated subquery to guarantee it
        comes from the same row as ``MAX(created_at)``.

        Args:
            user_id: Stable user identifier (e.g. ``"usr_admin"``).  Matched
                against the suffix of ``sender_id`` (``LIKE '%:{user_id}'``
                with LIKE special characters in user_id escaped).
            limit: Maximum number of sessions to return.  Defaults to 20.

        Returns:
            A list of dicts, each with keys:
            - ``session_id`` (str)
            - ``last_active`` (float) — MAX(created_at) for the session
            - ``turn_count`` (int) — number of turns in the session
            - ``preview`` (str) — first 80 chars of the latest assistant reply
        """
        from sqlalchemy import func, select
        from souvenir.models import ArchivedMessage

        # Escape LIKE special characters to prevent wildcard injection.
        safe_user_id = (
            user_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        # sender_id follows the "{channel}:{user_id}" convention stamped by Portail.
        # An exact suffix match is both correct and injection-safe.
        sender_pattern = f"%:{safe_user_id}"

        # Correlated scalar subquery: for each outer session_id, find the
        # assistant_content of the most-recent turn.  Using ORDER BY + LIMIT 1
        # guarantees the preview comes from the same row as MAX(created_at),
        # unlike func.max(assistant_content) which is an independent aggregate.
        am_inner = ArchivedMessage.__table__.alias("am_inner")
        preview_sq = (
            select(am_inner.c.assistant_content)
            .where(am_inner.c.session_id == ArchivedMessage.session_id)
            .where(am_inner.c.sender_id.like(sender_pattern, escape="\\"))
            .order_by(am_inner.c.created_at.desc())
            .limit(1)
            .correlate(ArchivedMessage)
            .scalar_subquery()
        )

        query = (
            select(
                ArchivedMessage.session_id,
                func.max(ArchivedMessage.created_at).label("last_active"),
                func.count().label("turn_count"),
                func.substr(preview_sq, 1, 80).label("preview"),
            )
            .where(ArchivedMessage.sender_id.like(sender_pattern, escape="\\"))
            .group_by(ArchivedMessage.session_id)
            .order_by(func.max(ArchivedMessage.created_at).desc())
            .limit(limit)
        )

        async with self._session_factory() as session:
            result = await session.execute(query)
            rows = result.fetchall()

        return [
            {
                "session_id": row.session_id,
                "last_active": row.last_active,
                "turn_count": row.turn_count,
                "preview": row.preview or "",
            }
            for row in rows
        ]

    async def get_session_history(
        self,
        session_id: str,
        limit: int = 50,
        user_id: str | None = None,
    ) -> list[dict]:
        """Return the archived turns for a specific session, oldest-first.

        Queries ``archived_messages`` WHERE ``session_id = :session_id``,
        ordered by ``created_at ASC``, limited to ``limit`` rows.

        When ``user_id`` is supplied the results are further filtered to rows
        whose ``sender_id`` ends with ``:{user_id}``.  Pass ``user_id`` whenever
        the caller needs to enforce that the session belongs to a specific user
        (e.g. REST API ownership checks).

        Args:
            session_id: Session identifier to fetch history for.
            limit: Maximum number of turns to return.  Defaults to 50.
            user_id: When provided, only return rows where
                ``sender_id LIKE '%:{user_id}'`` (exact suffix, wildcards escaped).
                Returns an empty list if the session exists but belongs to a
                different user — callers can treat this as "not found / forbidden".

        Returns:
            A list of dicts (possibly empty) with keys:
            - ``user_content`` (str)
            - ``assistant_content`` (str)
            - ``created_at`` (float)
            - ``correlation_id`` (str)
        """
        from sqlalchemy import select
        from souvenir.models import ArchivedMessage

        inner = (
            select(
                ArchivedMessage.user_content,
                ArchivedMessage.assistant_content,
                ArchivedMessage.created_at,
                ArchivedMessage.correlation_id,
            )
            .where(ArchivedMessage.session_id == session_id)
            .order_by(ArchivedMessage.created_at.desc())
            .limit(limit)
        )

        if user_id is not None:
            safe_uid = (
                user_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            inner = inner.where(
                ArchivedMessage.sender_id.like(f"%:{safe_uid}", escape="\\")
            )

        subq = inner.subquery()
        query = select(subq).order_by(subq.c.created_at.asc())

        async with self._session_factory() as session:
            result = await session.execute(query)
            rows = result.fetchall()

        return [
            {
                "user_content": row.user_content,
                "assistant_content": row.assistant_content,
                "created_at": row.created_at,
                "correlation_id": row.correlation_id,
            }
            for row in rows
        ]

    async def get_full_session_messages_raw(self, session_id: str) -> list[list[dict]]:
        """Return all ``messages_raw`` blobs for a session, oldest-first.

        Each element is a deserialized LangChain message list for one turn.
        Used by ``HistoryReadHandler`` to build the history payload for Forgeron.

        Args:
            session_id: Session identifier to fetch.

        Returns:
            List of turns (oldest-first), each being a list of message dicts.
            Returns an empty list if the session is unknown.
        """
        from sqlalchemy import select

        query = (
            select(ArchivedMessage.messages_raw, ArchivedMessage.created_at)
            .where(ArchivedMessage.session_id == session_id)
            .order_by(ArchivedMessage.created_at.asc())
        )
        async with self._session_factory() as session:
            result = await session.execute(query)
            rows = result.fetchall()

        turns: list[list[dict]] = []
        for row in rows:
            try:
                parsed = json.loads(row.messages_raw or "[]")
                if isinstance(parsed, list):
                    turns.append(parsed)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "LongTermStore: invalid messages_raw JSON for session=%s",
                    session_id,
                )
        return turns

    async def close(self) -> None:
        """Release async engine resources (aiosqlite connections).

        Must be called at service shutdown or at the end of a test to avoid
        aiosqlite thread leaks.

        Returns:
            None
        """
        await self._engine.dispose()
        logger.debug("LongTermStore engine disposed")
