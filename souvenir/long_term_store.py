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
            await self._clear_checkpointer_thread(user_id)

        return deleted_count

    async def _clear_checkpointer_thread(self, user_id: str) -> None:
        """Delete the LangGraph checkpointer thread for ``user_id``.

        Opens ``checkpoints.db`` via ``AsyncSqliteSaver`` and calls
        ``adelete_thread(user_id)`` to erase all LangGraph history associated
        with this user.

        Args:
            user_id: ``thread_id`` used in the checkpointer (stable
                cross-channel identifier, e.g. ``"usr_admin"``).
        """
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as exc:
            raise ImportError(
                "langgraph-checkpoint-sqlite is required to clear the checkpointer"
            ) from exc

        checkpoints_db = str(self._db_path.parent / "checkpoints.db")
        async with AsyncSqliteSaver.from_conn_string(checkpoints_db) as checkpointer:
            await checkpointer.adelete_thread(user_id)
        logger.info("Cleared LangGraph checkpointer thread for user_id=%s", user_id)

    async def close(self) -> None:
        """Release async engine resources (aiosqlite connections).

        Must be called at service shutdown or at the end of a test to avoid
        aiosqlite thread leaks.

        Returns:
            None
        """
        await self._engine.dispose()
        logger.debug("LongTermStore engine disposed")
