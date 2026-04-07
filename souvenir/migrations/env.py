"""Alembic environment — async SQLite via aiosqlite."""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from common.config_loader import resolve_storage_dir

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def _get_url() -> str:
    """Return the database URL.

    Priority: ``RELAIS_DB_PATH`` environment variable, then the default path
    ``~/.relais/storage/memory.db`` (via ``resolve_storage_dir()``).

    Returns:
        Async SQLAlchemy URL (``sqlite+aiosqlite:///...``).
    """
    db_path = os.environ.get(
        "RELAIS_DB_PATH",
        str(resolve_storage_dir() / "memory.db"),
    )
    return f"sqlite+aiosqlite:///{db_path}"


def run_migrations_offline() -> None:
    """Generate SQL without an active connection (--sql mode).

    Returns:
        None
    """
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations via an async connection.

    Returns:
        None
    """

    async def _do_run() -> None:
        engine = create_async_engine(_get_url())
        async with engine.connect() as connection:
            await connection.run_sync(
                lambda conn: context.configure(
                    connection=conn,
                    target_metadata=target_metadata,
                    render_as_batch=True,
                )
            )
            async with connection.begin():
                await connection.run_sync(lambda conn: context.run_migrations())
        await engine.dispose()

    asyncio.run(_do_run())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
