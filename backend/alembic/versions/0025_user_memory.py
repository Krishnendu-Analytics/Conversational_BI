"""Long-term chat memory: per-user facts and past-conversation summaries.

Revision ID: 0025_user_memory
Revises: 0024_search_trigram_indexes
Create Date: 2026-09-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_user_memory"
down_revision: Union[str, None] = "0024_search_trigram_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must match Settings.embedding_dimension (and document_chunks.embedding)
_EMBEDDING_DIM = 384


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "users",
        sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "user_memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("conversation_id", name="uq_user_memories_conversation"),
    )
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE user_memories ADD COLUMN embedding vector({_EMBEDDING_DIM})")
    else:
        op.add_column("user_memories", sa.Column("embedding", sa.Text(), nullable=True))
    op.create_index(
        "ix_user_memories_owner_kind", "user_memories", ["tenant_id", "user_id", "kind"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_memories_owner_kind", table_name="user_memories")
    op.drop_table("user_memories")
    op.drop_column("users", "memory_enabled")
