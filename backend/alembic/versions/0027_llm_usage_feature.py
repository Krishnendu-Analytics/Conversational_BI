"""Split LLM usage rows by feature (chat, schema_discovery, semantic_model, …).

Revision ID: 0027_llm_usage_feature
Revises: 0026_llm_usage_daily
Create Date: 2026-09-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_llm_usage_feature"
down_revision: Union[str, None] = "0026_llm_usage_daily"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_KEY = ["tenant_id", "user_id", "usage_date", "model"]


def upgrade() -> None:
    op.add_column(
        "llm_usage_daily",
        sa.Column("feature", sa.String(length=40), nullable=False, server_default="other"),
    )
    op.drop_constraint("uq_llm_usage_daily_key", "llm_usage_daily", type_="unique")
    op.create_unique_constraint(
        "uq_llm_usage_daily_key", "llm_usage_daily", [*_OLD_KEY, "feature"]
    )


def downgrade() -> None:
    # Fold feature rows back into one row per (tenant, user, day, model):
    # sum into the row with the smallest feature, then drop the rest.
    op.execute(
        """
        UPDATE llm_usage_daily k
        SET calls = s.calls, prompt_tokens = s.prompt_tokens,
            completion_tokens = s.completion_tokens, cost_usd = s.cost_usd
        FROM (
            SELECT tenant_id, user_id, usage_date, model, MIN(feature) AS feature,
                   SUM(calls) AS calls, SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens, SUM(cost_usd) AS cost_usd
            FROM llm_usage_daily
            GROUP BY tenant_id, user_id, usage_date, model
        ) s
        WHERE k.tenant_id = s.tenant_id AND k.user_id = s.user_id
          AND k.usage_date = s.usage_date AND k.model = s.model
          AND k.feature = s.feature
        """
    )
    op.execute(
        """
        DELETE FROM llm_usage_daily a
        USING llm_usage_daily b
        WHERE a.tenant_id = b.tenant_id AND a.user_id = b.user_id
          AND a.usage_date = b.usage_date AND a.model = b.model
          AND a.feature > b.feature
        """
    )
    op.drop_constraint("uq_llm_usage_daily_key", "llm_usage_daily", type_="unique")
    op.create_unique_constraint("uq_llm_usage_daily_key", "llm_usage_daily", _OLD_KEY)
    op.drop_column("llm_usage_daily", "feature")
