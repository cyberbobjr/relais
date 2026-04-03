"""Mémoire longue durée via SQLModel + SQLite async."""

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
from souvenir.models import ArchivedMessage

if TYPE_CHECKING:
    from common.envelope import Envelope


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

    async def archive(
        self,
        envelope: "Envelope",
        messages_raw: list[dict],
    ) -> None:
        """Archive un tour agent complet (upsert sur correlation_id).

        Stocke une seule ligne dans ``archived_messages`` par ``correlation_id``.
        Si une ligne existe déjà pour ce ``correlation_id``, elle est mise à jour.

        Args:
            envelope: L'enveloppe du message sortant (réponse de l'assistant).
            messages_raw: Liste sérialisable JSON de tous les messages LangChain
                du tour, telle que produite par
                ``atelier.message_serializer.serialize_messages()``.
        """
        now = time.time()
        user_content = envelope.metadata.get("user_message", "")
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
        """Supprime tous les messages archivés d'une session SQLite et efface
        le thread LangGraph du checkpointer pour l'utilisateur.

        La table ``archived_messages`` est nettoyée pour ``session_id``.
        Si ``user_id`` est fourni, le thread correspondant est également
        supprimé du checkpointer ``AsyncSqliteSaver`` (``checkpoints.db``).

        Args:
            session_id: Identifiant de la session à effacer (archived_messages).
            user_id: Identifiant stable de l'utilisateur (``thread_id`` du
                checkpointer). Si ``None``, le checkpointer n'est pas modifié.

        Returns:
            Nombre de lignes supprimées de ``archived_messages``.
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
        """Supprime le thread LangGraph du checkpointer pour ``user_id``.

        Ouvre ``checkpoints.db`` via ``AsyncSqliteSaver`` et appelle
        ``adelete_thread(user_id)`` pour effacer tout l'historique du
        graphe LangGraph associé à cet utilisateur.

        Args:
            user_id: ``thread_id`` utilisé dans le checkpointer (identifiant
                stable cross-channel, ex. ``"usr_admin"``).
        """
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as exc:
            raise ImportError(
                "langgraph-checkpoint-sqlite est requis pour effacer le checkpointer"
            ) from exc

        checkpoints_db = str(self._db_path.parent / "checkpoints.db")
        async with AsyncSqliteSaver.from_conn_string(checkpoints_db) as checkpointer:
            await checkpointer.adelete_thread(user_id)
        logger.info("Cleared LangGraph checkpointer thread for user_id=%s", user_id)

    async def close(self) -> None:
        """Libère les ressources de l'engine async (connexions aiosqlite).

        Doit être appelé à la fermeture du service ou en fin de test pour éviter
        les fuites de threads aiosqlite.

        Returns:
            None
        """
        await self._engine.dispose()
        logger.debug("LongTermStore engine disposed")
