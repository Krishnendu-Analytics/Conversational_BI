"""LLM usage in 5-minute buckets (was one row per day), table renamed llm_usage.

The AI usage page charts the last hour / 3 hours, which needs sub-day points.
Existing daily rows land in the bucket at midnight UTC of their day.

Revision ID: 0028_llm_usage_buckets
Revises: 0027_llm_usage_feature
Create Date: 2026-09-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_llm_usage_buckets"
down_revision: Union[str, None] = "0027_llm_usage_feature"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_usage_daily",
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE llm_usage_daily SET bucket_start = usage_date::timestamp AT TIME ZONE 'UTC'"
    )
    op.alter_column("llm_usage_daily", "bucket_start", nullable=False)
    op.drop_constraint("uq_llm_usage_daily_key", "llm_usage_daily", type_="unique")
    op.drop_index("ix_llm_usage_daily_tenant_date", table_name="llm_usage_daily")
    op.drop_column("llm_usage_daily", "usage_date")
    op.rename_table("llm_usage_daily", "llm_usage")
    op.create_unique_constraint(
        "uq_llm_usage_key",
        "llm_usage",
        ["tenant_id", "user_id", "bucket_start", "model", "feature"],
    )
    op.create_index("ix_llm_usage_tenant_bucket", "llm_usage", ["tenant_id", "bucket_start"])
    op.create_index("ix_llm_usage_user_bucket", "llm_usage", ["user_id", "bucket_start"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_user_bucket", table_name="llm_usage")
    op.drop_index("ix_llm_usage_tenant_bucket", table_name="llm_usage")
    op.drop_constraint("uq_llm_usage_key", "llm_usage", type_="unique")
    op.rename_table("llm_usage", "llm_usage_daily")
    op.add_column("llm_usage_daily", sa.Column("usage_date", sa.Date(), nullable=True))
    op.execute(
        "UPDATE llm_usage_daily SET usage_date = (bucket_start AT TIME ZONE 'UTC')::date"
    )
    # Roll the buckets of each day into one row (the smallest id keeps the sums).
    op.execute(
        """
        UPDATE llm_usage_daily k
        SET calls = s.calls, prompt_tokens = s.prompt_tokens,
            completion_tokens = s.completion_tokens, cost_usd = s.cost_usd
        FROM (
            SELECT MIN(id::text)::uuid AS id,
                   SUM(calls) AS calls, SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens, SUM(cost_usd) AS cost_usd
            FROM llm_usage_daily
            GROUP BY tenant_id, user_id, usage_date, model, feature
        ) s
        WHERE k.id = s.id
        """
    )
    op.execute(
        """
        DELETE FROM llm_usage_daily
        WHERE id NOT IN (
            SELECT MIN(id::text)::uuid FROM llm_usage_daily
            GROUP BY tenant_id, user_id, usage_date, model, feature
        )
        """
    )
    op.alter_column("llm_usage_daily", "usage_date", nullable=False)
    op.drop_column("llm_usage_daily", "bucket_start")
    op.create_unique_constraint(
        "uq_llm_usage_daily_key",
        "llm_usage_daily",
        ["tenant_id", "user_id", "usage_date", "model", "feature"],
    )
    op.create_index(
        "ix_llm_usage_daily_tenant_date", "llm_usage_daily", ["tenant_id", "usage_date"]
    )
