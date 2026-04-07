"""SQLModel ORM models for the Souvenir brick."""

import time
import uuid

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class MemoryFile(SQLModel, table=True):
    """Long-term memory file managed by the SouvenirBackend.

    Each file is identified by the pair ``(user_id, path)``, which is
    constrained to be unique — an upsert on this pair updates the existing
    content rather than inserting a duplicate.
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
    """A complete agent turn archived from the relais:messages:outgoing stream.

    One record per turn, uniquely identified by ``correlation_id``. The
    ``messages_raw`` field contains the JSON blob of the full LangChain message
    list for the turn (serialised via
    ``atelier.message_serializer.serialize_messages``).

    Enables session history reconstruction from SQLite when the Redis cache is
    missing.
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
