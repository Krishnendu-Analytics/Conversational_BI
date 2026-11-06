"""LLM usage per user per day per model (replaces the unused llm_usage table).

Revision ID: 0026_llm_usage_daily
Revises: 0025_user_memory
Create Date: 2026-09-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_llm_usage_daily"
down_revision: Union[str, None] = "0025_user_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # llm_usage was created in 0001 but never written to.
    op.drop_index(op.f("ix_llm_usage_tenant_id"), table_name="llm_usage")
    op.drop_table("llm_usage")

    op.create_table(
        "llm_usage_daily",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "usage_date", "model", name="uq_llm_usage_daily_key"
        ),
    )
    op.create_index(
        "ix_llm_usage_daily_tenant_date", "llm_usage_daily", ["tenant_id", "usage_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_llm_usage_daily_tenant_date", table_name="llm_usage_daily")
    op.drop_table("llm_usage_daily")
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("cost_cents", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_llm_usage_tenant_id"), "llm_usage", ["tenant_id"], unique=False)
