"""Persistent dashboards and widgets.

Revision ID: 0008_dashboards
Revises: 0007_conversations_messages
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_dashboards"
down_revision: Union[str, None] = "0007_conversations_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dashboards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("filters_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("layout_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dashboards_tenant_id"), "dashboards", ["tenant_id"], unique=False)

    op.create_table(
        "dashboard_widgets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dashboard_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("position_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("sql_template", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["dashboard_id"], ["dashboards.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_dashboard_widgets_dashboard_id"),
        "dashboard_widgets",
        ["dashboard_id"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_conversations_dashboard_id",
        "conversations",
        "dashboards",
        ["dashboard_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_conversations_dashboard_id", "conversations", type_="foreignkey")
    op.drop_index(op.f("ix_dashboard_widgets_dashboard_id"), table_name="dashboard_widgets")
    op.drop_table("dashboard_widgets")
    op.drop_index(op.f("ix_dashboards_tenant_id"), table_name="dashboards")
    op.drop_table("dashboards")
