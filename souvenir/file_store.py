"""Stockage async de fichiers mémoire dans SQLite pour le backend SouvenirBackend.

Ce module expose :class:`FileStore`, analogue à :class:`LongTermStore` mais
dédié à la table ``memory_files``. Il utilise SQLModel + aiosqlite et partage
le même fichier ``memory.db`` que ``LongTermStore``.
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
    """Stockage de fichiers mémoire dans SQLite (table ``memory_files``).

    Partage le même fichier ``memory.db`` que ``LongTermStore``. Toutes les
    opérations I/O sont non-bloquantes grâce à ``aiosqlite``.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialise le store et crée l'engine async.

        Args:
            db_path: Chemin vers le fichier SQLite. Par défaut
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
        """Crée toutes les tables déclarées dans SQLModel.metadata.

        Réservé aux tests et à l'initialisation hors-Alembic.

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
        """Écrit ou crée un fichier mémoire.

        Args:
            user_id: Identifiant unique de l'utilisateur propriétaire.
            path: Chemin virtuel du fichier (ex: ``/memories/notes.md``).
            content: Contenu textuel du fichier.
            overwrite: Si ``True``, remplace un fichier existant.
                Si ``False`` (défaut), retourne une erreur si le fichier existe.

        Returns:
            ``None`` en cas de succès, ou un message d'erreur string en cas d'échec.
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
        """Lit le contenu d'un fichier mémoire.

        Args:
            user_id: Identifiant unique de l'utilisateur propriétaire.
            path: Chemin virtuel du fichier.

        Returns:
            Tuple ``(content, error)`` — ``error`` est ``None`` en cas de
            succès, ``content`` est ``None`` en cas d'erreur.
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
        """Liste les fichiers mémoire d'un utilisateur sous un préfixe de chemin.

        Args:
            user_id: Identifiant unique de l'utilisateur propriétaire.
            path_prefix: Préfixe de chemin pour filtrer les résultats.
                Défaut : ``/memories/``.

        Returns:
            Liste de dicts ``{"path": str, "size": int, "modified_at": str}``
            triés par ``path`` ascendant.
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
        """Libère les ressources de l'engine async.

        Returns:
            None
        """
        await self._engine.dispose()
        logger.debug("FileStore engine disposed")


def _epoch_to_iso(epoch: float) -> str:
    """Convertit un timestamp epoch en chaîne ISO 8601 UTC.

    Args:
        epoch: Timestamp en secondes depuis l'epoch Unix.

    Returns:
        Chaîne ISO 8601, ex: ``"2026-04-03T12:00:00Z"``.
    """
    import datetime

    return (
        datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
