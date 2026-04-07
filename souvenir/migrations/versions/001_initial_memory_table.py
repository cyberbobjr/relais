"""Initial memory table.

Revision ID: 001
Revises:
Create Date: 2026-03-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``memories`` table with a UNIQUE constraint on (user_id, key).

    Returns:
        None
    """
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "key", name="uq_user_key"),
    )
    op.create_index("idx_user_key", "memories", ["user_id", "key"])


def downgrade() -> None:
    """Drop the ``memories`` table.

    Returns:
        None
    """
    op.drop_index("idx_user_key", table_name="memories")
    op.drop_table("memories")
