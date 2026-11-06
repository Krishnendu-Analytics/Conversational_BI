"""Add domain column to metrics/dimensions for the multi-domain investigator.

Revision ID: 0017_semantic_domain
Revises: 0016_conversation_archive
Create Date: 2026-09-22

Nullable by design: when unset, app.agent.domain_catalog.resolve_domains()
auto-derives a domain name from the table's fact_/f_ prefix (the same
convention dashboard.py already uses for dashboard section labels), so
existing rows work without a backfill.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_semantic_domain"
down_revision: Union[str, None] = "0016_conversation_archive"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("metrics", sa.Column("domain", sa.String(length=120), nullable=True))
    op.add_column("dimensions", sa.Column("domain", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("dimensions", "domain")
    op.drop_column("metrics", "domain")
