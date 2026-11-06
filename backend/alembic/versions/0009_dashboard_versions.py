"""Dashboard version history.

Revision ID: 0009_dashboard_versions
Revises: 0008_dashboards
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_dashboard_versions"
down_revision: Union[str, None] = "0008_dashboards"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dashboard_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dashboard_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["dashboard_id"], ["dashboards.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dashboard_id",
            "version_number",
            name="uq_dashboard_versions_dashboard_version",
        ),
    )
    op.create_index(
        op.f("ix_dashboard_versions_dashboard_id"),
        "dashboard_versions",
        ["dashboard_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_dashboard_versions_dashboard_id"), table_name="dashboard_versions")
    op.drop_table("dashboard_versions")
