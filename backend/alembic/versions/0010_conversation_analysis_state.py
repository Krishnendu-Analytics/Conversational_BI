"""Add persistent analysis_state_json to conversations.

Revision ID: 0010_conversation_analysis_state
Revises: 0009_dashboard_versions
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_conversation_analysis_state"
down_revision: Union[str, None] = "0009_dashboard_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "analysis_state_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_index(
        "ix_conversations_updated_at",
        "conversations",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_updated_at", table_name="conversations")
    op.drop_column("conversations", "analysis_state_json")
