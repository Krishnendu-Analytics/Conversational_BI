"""Add archived_at to conversations for archive (separate from delete).

Revision ID: 0016_conversation_archive
Revises: 0015_semantic_connection_identity
Create Date: 2026-09-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_conversation_archive"
down_revision: Union[str, None] = "0015_semantic_connection_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_conversations_archived_at",
        "conversations",
        ["archived_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_archived_at", table_name="conversations")
    op.drop_column("conversations", "archived_at")
