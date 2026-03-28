"""Mémoire longue durée via SQLModel + SQLite async."""

import logging
import time
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from common.config_loader import resolve_storage_dir
from souvenir.models import Memory

logger = logging.getLogger(__name__)


class LongTermStore:
    """Mémoire longue durée dans SQLite (~/.relais/memory.db).

    Utilise SQLModel + SQLAlchemy async pour toutes les opérations I/O afin de
    ne pas bloquer la boucle asyncio. Le schéma est géré par Alembic en
    production ; en test, ``_create_tables()`` peut être appelé directement.
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

        Réservé aux tests et à l'initialisation hors-Alembic. En production,
        utilisez ``alembic upgrade head``.

        Returns:
            None
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def store(
        self,
        user_id: str,
        key: str,
        value: str,
        source: str = "manual",
    ) -> None:
        """Stocke ou met à jour un fait mémorisé pour un utilisateur.

        Si un enregistrement avec le même ``(user_id, key)`` existe déjà, la
        valeur et ``updated_at`` sont mis à jour (upsert via ON CONFLICT).

        Args:
            user_id: Identifiant de l'utilisateur propriétaire du souvenir.
            key: Clé nommée du souvenir (ex: ``"prénom"``).
            value: Valeur textuelle associée à la clé.
            source: Origine de l'enregistrement (``"manual"``, ``"llm"``, …).

        Returns:
            None
        """
        now = time.time()
        stmt = (
            sqlite_insert(Memory)
            .values(
                user_id=user_id,
                key=key,
                value=value,
                source=source,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "key"],
                set_={"value": value, "source": source, "updated_at": now},
            )
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()
        logger.debug("Stored memory for user=%s key=%s", user_id, key)

    async def retrieve(
        self, user_id: str, key: str | None = None
    ) -> list[dict]:
        """Récupère les souvenirs d'un utilisateur.

        Args:
            user_id: Identifiant de l'utilisateur.
            key: Si fourni, filtre les résultats sur cette clé exacte.

        Returns:
            Liste de dicts ``{"id", "user_id", "key", "value", "source",
            "created_at", "updated_at"}`` triés par ``updated_at`` descendant.
        """
        stmt = select(Memory).where(Memory.user_id == user_id)
        if key is not None:
            stmt = stmt.where(Memory.key == key)
        stmt = stmt.order_by(Memory.updated_at.desc())  # type: ignore[arg-type]
        async with self._session_factory() as session:
            result = await session.exec(stmt)
            rows = result.all()
        return [r.model_dump() for r in rows]

    async def delete(self, user_id: str, key: str) -> None:
        """Supprime un souvenir spécifique.

        Args:
            user_id: Identifiant de l'utilisateur.
            key: Clé du souvenir à supprimer.

        Returns:
            None
        """
        stmt = select(Memory).where(Memory.user_id == user_id, Memory.key == key)
        async with self._session_factory() as session:
            result = await session.exec(stmt)
            row = result.first()
            if row:
                await session.delete(row)
                await session.commit()
        logger.debug("Deleted memory for user=%s key=%s", user_id, key)

    async def search(self, user_id: str, query: str) -> list[dict]:
        """Recherche LIKE dans les valeurs des souvenirs d'un utilisateur.

        Args:
            user_id: Identifiant de l'utilisateur.
            query: Terme de recherche (sous-chaîne). Les wildcards SQL ``%``
                sont ajoutés automatiquement.

        Returns:
            Liste de dicts correspondant aux souvenirs dont la valeur contient
            ``query``, triés par ``updated_at`` descendant.
        """
        pattern = f"%{query}%"
        stmt = (
            select(Memory)
            .where(Memory.user_id == user_id, Memory.value.like(pattern))  # type: ignore[union-attr]
            .order_by(Memory.updated_at.desc())  # type: ignore[arg-type]
        )
        async with self._session_factory() as session:
            result = await session.exec(stmt)
            rows = result.all()
        return [r.model_dump() for r in rows]

    async def close(self) -> None:
        """Libère les ressources de l'engine async (connexions aiosqlite).

        Doit être appelé à la fermeture du service ou en fin de test pour éviter
        les fuites de threads aiosqlite.

        Returns:
            None
        """
        await self._engine.dispose()
        logger.debug("LongTermStore engine disposed")
