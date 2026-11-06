"""Add conversation_connections for multi-DB investigation (Phase 4).

Revision ID: 0018_conversation_connections
Revises: 0017_semantic_domain
Create Date: 2026-09-22

Additive-only association table: extra connections a conversation can
investigate across, beyond its primary Conversation.connection_id. The
primary connection remains the source of truth for the existing
single-shot chat/dashboard REST paths; those are unaffected by this table.
Multi-connection investigation is opt-in — a conversation with no rows here
behaves exactly as before.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_conversation_connections"
down_revision: Union[str, None] = "0017_semantic_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "connection_id", name="uq_conversation_connections_pair"
        ),
    )
    op.create_index(
        "ix_conversation_connections_tenant_id",
        "conversation_connections",
        ["tenant_id"],
    )
    op.create_index(
        "ix_conversation_connections_conversation_id",
        "conversation_connections",
        ["conversation_id"],
    )
    op.create_index(
        "ix_conversation_connections_connection_id",
        "conversation_connections",
        ["connection_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_connections_connection_id", table_name="conversation_connections"
    )
    op.drop_index(
        "ix_conversation_connections_conversation_id", table_name="conversation_connections"
    )
    op.drop_index("ix_conversation_connections_tenant_id", table_name="conversation_connections")
    op.drop_table("conversation_connections")
