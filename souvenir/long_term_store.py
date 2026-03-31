"""Mémoire longue durée via SQLModel + SQLite async."""

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from common.config_loader import resolve_storage_dir
from souvenir.models import ArchivedMessage, Memory, UserFact

if TYPE_CHECKING:
    from common.envelope import Envelope


@dataclass(frozen=True)
class PaginatedResult:
    """Result container for a paginated query over archived messages.

    Attributes:
        items: Tuple of ArchivedMessage objects matching the query filters for
            the requested page.
        total: Total count of rows matching the filters, regardless of
            limit/offset (used to build pagination UI).
        limit: Maximum number of items requested per page.
        offset: Number of matching rows skipped before this page.
        has_more: True when additional pages exist beyond this one, i.e.
            ``total > offset + len(items)``.
    """

    items: tuple
    total: int
    limit: int
    offset: int
    has_more: bool

logger = logging.getLogger(__name__)

# TODO : il faudrait une méthode pour lister les clés existantes de souvenir d'un utilisateur
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

    async def upsert_facts(self, sender_id: str, facts: list[dict]) -> None:
        """Insère ou met à jour des faits utilisateur par ``(sender_id, fact_hash)``.

        Le hash est calculé sur ``sender_id:fact_text`` — deux appels avec le
        même fait mettent à jour ``confidence``, ``category`` et ``updated_at``
        sans créer de doublon.

        Args:
            sender_id: Identifiant de l'utilisateur propriétaire des faits.
            facts: Liste de dicts avec les clés ``fact``, ``category``,
                ``confidence`` et optionnellement ``source_corr``.
        """
        now = time.time()
        async with self._session_factory() as session:
            for fact_data in facts:
                fact_text = fact_data["fact"]
                fact_hash = hashlib.md5(
                    f"{sender_id}:{fact_text}".encode()
                ).hexdigest()
                stmt = select(UserFact).where(
                    UserFact.sender_id == sender_id,
                    UserFact.fact_hash == fact_hash,
                )
                result = await session.exec(stmt)
                existing = result.first()
                if existing:
                    existing.confidence = fact_data.get("confidence", existing.confidence)
                    existing.category = fact_data.get("category", existing.category)
                    existing.updated_at = now
                    session.add(existing)
                else:
                    new_fact = UserFact(
                        sender_id=sender_id,
                        fact=fact_text,
                        category=fact_data.get("category"),
                        confidence=fact_data.get("confidence", 1.0),
                        source_corr=fact_data.get("source_corr"),
                        fact_hash=fact_hash,
                    )
                    session.add(new_fact)
            await session.commit()
        logger.debug("Upserted %d facts for sender=%s", len(facts), sender_id)

    async def get_user_facts(self, sender_id: str, limit: int = 20) -> list[str]:
        """Retourne la liste des faits connus d'un utilisateur (chaînes de texte).

        Triés par ``updated_at`` descendant pour obtenir les plus récents en
        premier.

        Args:
            sender_id: Identifiant de l'utilisateur.
            limit: Nombre maximum de faits retournés (défaut: 20).

        Returns:
            Liste de chaînes représentant les faits.
        """
        async with self._session_factory() as session:
            stmt = (
                select(UserFact)
                .where(UserFact.sender_id == sender_id)
                .order_by(UserFact.updated_at.desc())  # type: ignore[arg-type]
                .limit(limit)
            )
            result = await session.exec(stmt)
            return [row.fact for row in result.all()]

    async def archive(self, envelope: "Envelope") -> None:
        """Archive un message sortant (réponse assistant + message utilisateur).

        Stocke deux lignes dans ``archived_messages`` : le message utilisateur
        (extrait de ``envelope.metadata["user_message"]``) et la réponse de
        l'assistant (``envelope.content``).

        Args:
            envelope: L'enveloppe du message sortant.
        """
        now = time.time()
        user_message = envelope.metadata.get("user_message", "")
        async with self._session_factory() as session:
            if user_message:
                session.add(
                    ArchivedMessage(
                        session_id=envelope.session_id,
                        sender_id=envelope.sender_id,
                        channel=envelope.channel,
                        role="user",
                        content=user_message,
                        correlation_id=envelope.correlation_id,
                        created_at=now - 0.001,  # slightly before assistant
                    )
                )
            session.add(
                ArchivedMessage(
                    session_id=envelope.session_id,
                    sender_id=envelope.sender_id,
                    channel=envelope.channel,
                    role="assistant",
                    content=envelope.content,
                    correlation_id=envelope.correlation_id,
                    created_at=now,
                )
            )
            await session.commit()
        logger.debug("Archived message for session=%s", envelope.session_id)

    async def get_recent_messages(
        self, session_id: str, limit: int = 20
    ) -> list[dict]:
        """Retourne les N derniers messages d'une session depuis SQLite.

        Fallback utilisé quand le cache Redis est vide. Retourne les messages
        dans l'ordre chronologique (plus ancien en premier).

        Args:
            session_id: Identifiant de la session.
            limit: Nombre maximum de messages (défaut: 20).

        Returns:
            Liste de dicts ``{"role": str, "content": str}``.
        """
        async with self._session_factory() as session:
            stmt = (
                select(ArchivedMessage)
                .where(ArchivedMessage.session_id == session_id)
                .order_by(ArchivedMessage.created_at.desc())  # type: ignore[arg-type]
                .limit(limit)
            )
            result = await session.exec(stmt)
            rows = result.all()
        rows = list(reversed(rows))
        return [{"role": row.role, "content": row.content} for row in rows]

    async def clear_session(self, session_id: str) -> int:
        """Supprime tous les messages archivés d'une session SQLite.

        Seule la table ``archived_messages`` est affectée. Les ``UserFact``
        (table ``user_facts``) et les ``Memory`` (table ``memories``) sont
        conservés.

        Args:
            session_id: Identifiant de la session à effacer.

        Returns:
            Nombre de lignes supprimées.
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
        return deleted_count

    async def query(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
        since: float | None = None,
        until: float | None = None,
        search: str | None = None,
    ) -> PaginatedResult:
        """Return a paginated slice of archived messages for a user.

        Filters are cumulative: all provided filters must match. Results are
        ordered by ``created_at`` descending (most recent first).

        Args:
            user_id: The ``sender_id`` of the user whose messages to query.
            limit: Maximum number of rows to return in this page (default 20).
            offset: Number of matching rows to skip before this page (default 0).
            since: If provided, only include messages with
                ``created_at >= since`` (epoch seconds, inclusive).
            until: If provided, only include messages with
                ``created_at <= until`` (epoch seconds, inclusive).
            search: If provided, only include messages whose ``content`` field
                contains this substring (case-insensitive).

        Returns:
            A frozen ``PaginatedResult`` with ``items`` (tuple of
            ``ArchivedMessage`` objects), ``total`` (unsliced count), ``limit``,
            ``offset``, and ``has_more``.
        """
        base_stmt = select(ArchivedMessage).where(
            ArchivedMessage.sender_id == user_id
        )
        if since is not None:
            base_stmt = base_stmt.where(
                ArchivedMessage.created_at >= since  # type: ignore[arg-type]
            )
        if until is not None:
            base_stmt = base_stmt.where(
                ArchivedMessage.created_at <= until  # type: ignore[arg-type]
            )
        if search is not None:
            pattern = f"%{search}%"
            base_stmt = base_stmt.where(
                ArchivedMessage.content.ilike(pattern)  # type: ignore[union-attr]
            )

        count_stmt = select(func.count()).select_from(
            base_stmt.subquery()
        )

        page_stmt = (
            base_stmt
            .order_by(ArchivedMessage.created_at.desc())  # type: ignore[arg-type]
            .offset(offset)
            .limit(limit)
        )

        async with self._session_factory() as session:
            count_result = await session.exec(count_stmt)
            total: int = count_result.one()

            page_result = await session.exec(page_stmt)
            rows = page_result.all()

        items = tuple(rows)
        has_more = total > offset + len(items)
        return PaginatedResult(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

    async def close(self) -> None:
        """Libère les ressources de l'engine async (connexions aiosqlite).

        Doit être appelé à la fermeture du service ou en fin de test pour éviter
        les fuites de threads aiosqlite.

        Returns:
            None
        """
        await self._engine.dispose()
        logger.debug("LongTermStore engine disposed")
