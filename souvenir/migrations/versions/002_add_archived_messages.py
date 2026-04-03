"""Add archived_messages table with full messages_raw blob storage.

Revision ID: 002
Revises: 001
Create Date: 2026-04-03

Replaces the original migration 002 which created the now-deleted ``user_facts``
table and an outdated ``archived_messages`` schema (``role`` / ``content``
columns only).  This migration creates the correct schema that stores one row
per agentic turn with the full serialised message list (``messages_raw``).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crée la table ``archived_messages`` avec le schéma messages_raw.

    Returns:
        None
    """
    op.create_table(
        "archived_messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("sender_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("user_content", sa.String(), nullable=False, server_default=""),
        sa.Column("assistant_content", sa.String(), nullable=False, server_default=""),
        sa.Column("messages_raw", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("correlation_id", name="uq_archived_messages_correlation_id"),
    )
    op.create_index("ix_archived_messages_session_id", "archived_messages", ["session_id"])
    op.create_index("ix_archived_messages_sender_id", "archived_messages", ["sender_id"])
    op.create_index(
        "ix_archived_messages_correlation_id",
        "archived_messages",
        ["correlation_id"],
        unique=True,
    )


def downgrade() -> None:
    """Supprime la table ``archived_messages``.

    Returns:
        None
    """
    op.drop_index("ix_archived_messages_correlation_id", table_name="archived_messages")
    op.drop_index("ix_archived_messages_sender_id", table_name="archived_messages")
    op.drop_index("ix_archived_messages_session_id", table_name="archived_messages")
    op.drop_table("archived_messages")
