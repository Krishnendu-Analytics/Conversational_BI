"""Organizations RBAC: super admin flag, invites, departments and their grants.

Revision ID: 0029_org_rbac
Revises: 0028_llm_usage_buckets
Create Date: 2026-09-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_org_rbac"
down_revision: Union[str, None] = "0028_llm_usage_buckets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_super_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "user_invites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False, unique=True),
        sa.Column("invited_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_user_invites_tenant_id", "user_invites", ["tenant_id"])
    op.create_index("ix_user_invites_user_id", "user_invites", ["user_id"])

    op.create_table(
        "departments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "name", name="uq_departments_tenant_name"),
    )
    op.create_index("ix_departments_tenant_id", "departments", ["tenant_id"])

    op.create_table(
        "department_members",
        sa.Column(
            "department_id",
            sa.Uuid(),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
    )
    op.create_index("ix_department_members_user_id", "department_members", ["user_id"])
    op.create_index("ix_department_members_tenant_id", "department_members", ["tenant_id"])

    op.create_table(
        "department_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "department_id",
            sa.Uuid(),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_name", sa.String(length=200), nullable=True),
    )
    op.create_index("ix_department_grants_tenant_id", "department_grants", ["tenant_id"])
    op.create_index("ix_department_grants_department_id", "department_grants", ["department_id"])
    op.create_index("ix_department_grants_connection_id", "department_grants", ["connection_id"])


def downgrade() -> None:
    op.drop_table("department_grants")
    op.drop_table("department_members")
    op.drop_table("departments")
    op.drop_table("user_invites")
    op.drop_column("users", "is_super_admin")
