"""Shared async SQLAlchemy base for Forgeron SQLite stores."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession


class BaseAsyncStore:
    """Async SQLite store base providing engine setup and lifecycle management.

    Args:
        db_path: Absolute path to the SQLite file.  Parent directory is created
            automatically when it does not exist.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{db_path}"
        self._engine = create_async_engine(url, echo=False)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def close(self) -> None:
        """Dispose the async engine and release aiosqlite threads."""
        await self._engine.dispose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
