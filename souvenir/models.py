"""SQLModel ORM models for the Souvenir brick."""

import time
import uuid

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class Memory(SQLModel, table=True):
    """Représente un souvenir long terme associé à un utilisateur.

    Chaque entrée est identifiée par le couple ``(user_id, key)`` qui est
    contraint à être unique — un upsert sur ce couple met à jour la valeur
    existante plutôt que d'insérer un doublon.
    """

    __tablename__ = "memories"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_key"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    key: str
    value: str
    source: str = Field(default="manual")
    created_at: float
    updated_at: float


class UserFact(SQLModel, table=True):
    """Fait durable extrait d'un échange, lié à un utilisateur.

    Upsert sur ``(sender_id, fact_hash)`` pour éviter les doublons.
    """

    __tablename__ = "user_facts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    sender_id: str = Field(index=True)
    fact: str
    category: str | None = None
    confidence: float = Field(default=1.0)
    source_corr: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    fact_hash: str = Field(index=True)


class ArchivedMessage(SQLModel, table=True):
    """Message archivé depuis le stream relais:messages:outgoing.

    Permet la reconstruction de l'historique d'une session depuis SQLite
    en cas de cache Redis manquant.
    """

    __tablename__ = "archived_messages"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: str = Field(index=True)
    sender_id: str = Field(index=True)
    channel: str
    role: str  # "user" or "assistant"
    content: str
    correlation_id: str
    created_at: float = Field(default_factory=time.time)
