"""Persistent conversations and messages.

Revision ID: 0007_conversations_messages
Revises: 0006_semantic_suggestions
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_conversations_messages"
down_revision: Union[str, None] = "0006_semantic_suggestions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("connection_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("dashboard_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversations_connection_id",
        "conversations",
        "connections",
        ["connection_id"],
        ["id"],
    )
    # ix_conversations_tenant_id is created in 0001_initial; do not recreate it.

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_messages_conversation_id"), "messages", ["conversation_id"], unique=False)
    op.create_index(op.f("ix_messages_tenant_id"), "messages", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_tenant_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_conversation_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_constraint("fk_conversations_connection_id", "conversations", type_="foreignkey")
    op.drop_column("conversations", "deleted_at")
    op.drop_column("conversations", "updated_at")
    op.drop_column("conversations", "created_at")
    op.drop_column("conversations", "dashboard_id")
    op.drop_column("conversations", "connection_id")
