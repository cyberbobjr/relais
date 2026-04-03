"""SQLModel ORM models for the Souvenir brick."""

import time
import uuid

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class MemoryFile(SQLModel, table=True):
    """Fichier de mémoire long terme géré par le backend SouvenirBackend.

    Chaque fichier est identifié par le couple ``(user_id, path)`` qui est
    contraint à être unique — un upsert sur ce couple met à jour le contenu
    existant plutôt que d'insérer un doublon.
    """

    __tablename__ = "memory_files"
    __table_args__ = (UniqueConstraint("user_id", "path", name="uq_user_path"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    path: str = Field(index=True)
    content: str
    created_at: float = Field(default_factory=time.time)
    modified_at: float = Field(default_factory=time.time)


class ArchivedMessage(SQLModel, table=True):
    """Un tour agent complet archivé depuis le stream relais:messages:outgoing.

    Un seul enregistrement par tour, identifié de manière unique par
    ``correlation_id``.  Le champ ``messages_raw`` contient le blob JSON de
    l'intégralité de la liste de messages LangChain du tour (serialisée via
    ``atelier.message_serializer.serialize_messages``).

    Permet la reconstruction de l'historique d'une session depuis SQLite
    en cas de cache Redis manquant.
    """

    __tablename__ = "archived_messages"
    __table_args__ = (UniqueConstraint("correlation_id", name="uq_correlation_id"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: str = Field(index=True)
    sender_id: str = Field(index=True)
    channel: str
    user_content: str = Field(default="")
    assistant_content: str = Field(default="")
    messages_raw: str = Field(default="[]")  # JSON-serialized list[dict]
    correlation_id: str = Field(index=True)
    created_at: float = Field(default_factory=time.time)
