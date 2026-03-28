"""SQLModel ORM models for the Souvenir brick."""

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
