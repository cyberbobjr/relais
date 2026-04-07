"""Async memory file storage in SQLite for the SouvenirBackend.

This module exposes :class:`FileStore`, analogous to :class:`LongTermStore` but
dedicated to the ``memory_files`` table. It uses SQLModel + aiosqlite and shares
the same ``memory.db`` file as ``LongTermStore``.
"""

import logging
import time
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from common.config_loader import resolve_storage_dir
from souvenir.models import MemoryFile

logger = logging.getLogger(__name__)


class FileStore:
    """Memory file storage in SQLite (table ``memory_files``).

    Shares the same ``memory.db`` file as ``LongTermStore``. All I/O operations
    are non-blocking thanks to ``aiosqlite``.
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

        Reserved for tests and non-Alembic initialisation.

        Returns:
            None
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def write_file(
        self,
        user_id: str,
        path: str,
        content: str,
        overwrite: bool = False,
    ) -> str | None:
        """Write or create a memory file.

        Args:
            user_id: Unique identifier of the owning user.
            path: Virtual file path (e.g. ``/memories/notes.md``).
            content: Text content of the file.
            overwrite: If ``True``, replaces an existing file.
                If ``False`` (default), returns an error if the file already exists.

        Returns:
            ``None`` on success, or an error message string on failure.
        """
        now = time.time()
        if not overwrite:
            # Check existence first
            stmt = select(MemoryFile).where(
                MemoryFile.user_id == user_id, MemoryFile.path == path
            )
            async with self._session_factory() as session:
                result = await session.exec(stmt)
                existing = result.first()
            if existing is not None:
                return f"File already exists: {path}"

        upsert_stmt = (
            sqlite_insert(MemoryFile)
            .values(
                user_id=user_id,
                path=path,
                content=content,
                created_at=now,
                modified_at=now,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "path"],
                set_={"content": content, "modified_at": now},
            )
        )
        async with self._session_factory() as session:
            await session.execute(upsert_stmt)
            await session.commit()
        logger.debug("Wrote memory file user=%s path=%s", user_id, path)
        return None

    async def read_file(self, user_id: str, path: str) -> tuple[str | None, str | None]:
        """Read the content of a memory file.

        Args:
            user_id: Unique identifier of the owning user.
            path: Virtual file path.

        Returns:
            Tuple ``(content, error)`` — ``error`` is ``None`` on success,
            ``content`` is ``None`` on error.
        """
        stmt = select(MemoryFile).where(
            MemoryFile.user_id == user_id, MemoryFile.path == path
        )
        async with self._session_factory() as session:
            result = await session.exec(stmt)
            row = result.first()
        if row is None:
            return None, f"File not found: {path}"
        return row.content, None

    async def list_files(self, user_id: str, path_prefix: str = "/memories/") -> list[dict]:
        """List memory files for a user under a path prefix.

        Args:
            user_id: Unique identifier of the owning user.
            path_prefix: Path prefix to filter results.
                Default: ``/memories/``.

        Returns:
            List of dicts ``{"path": str, "size": int, "modified_at": str}``
            sorted by ``path`` ascending.
        """
        stmt = (
            select(MemoryFile)
            .where(
                MemoryFile.user_id == user_id,
                MemoryFile.path.startswith(path_prefix),  # type: ignore[union-attr]
            )
            .order_by(MemoryFile.path)  # type: ignore[arg-type]
        )
        async with self._session_factory() as session:
            result = await session.exec(stmt)
            rows = result.all()
        return [
            {
                "path": row.path,
                "size": len(row.content.encode("utf-8")),
                "modified_at": _epoch_to_iso(row.modified_at),
            }
            for row in rows
        ]

    async def close(self) -> None:
        """Release async engine resources.

        Returns:
            None
        """
        await self._engine.dispose()
        logger.debug("FileStore engine disposed")


def _epoch_to_iso(epoch: float) -> str:
    """Convert a Unix epoch timestamp to an ISO 8601 UTC string.

    Args:
        epoch: Timestamp in seconds since the Unix epoch.

    Returns:
        ISO 8601 string, e.g. ``"2026-04-03T12:00:00Z"``.
    """
    import datetime

    return (
        datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
