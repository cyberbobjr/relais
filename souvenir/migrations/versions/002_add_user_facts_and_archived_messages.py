"""Add user_facts and archived_messages tables.

Revision ID: 002
Revises: 001
Create Date: 2026-03-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crée les tables ``user_facts`` et ``archived_messages``.

    Returns:
        None
    """
    op.create_table(
        "user_facts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("sender_id", sa.String(), nullable=False),
        sa.Column("fact", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("source_corr", sa.String(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("fact_hash", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_facts_sender_id", "user_facts", ["sender_id"])
    op.create_index("ix_user_facts_fact_hash", "user_facts", ["fact_hash"])

    op.create_table(
        "archived_messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("sender_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_archived_messages_session_id", "archived_messages", ["session_id"])
    op.create_index("ix_archived_messages_sender_id", "archived_messages", ["sender_id"])


def downgrade() -> None:
    """Supprime les tables ``user_facts`` et ``archived_messages``.

    Returns:
        None
    """
    op.drop_index("ix_archived_messages_sender_id", table_name="archived_messages")
    op.drop_index("ix_archived_messages_session_id", table_name="archived_messages")
    op.drop_table("archived_messages")

    op.drop_index("ix_user_facts_fact_hash", table_name="user_facts")
    op.drop_index("ix_user_facts_sender_id", table_name="user_facts")
    op.drop_table("user_facts")
