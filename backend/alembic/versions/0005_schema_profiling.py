"""Schema profiling columns on schema_tables.

Revision ID: 0005_schema_profiling
Revises: 0004_rbac_audit
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_schema_profiling"
down_revision: Union[str, None] = "0004_rbac_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "schema_tables",
        sa.Column("estimated_row_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "schema_tables",
        sa.Column("profiling_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("schema_tables", "profiling_json")
    op.drop_column("schema_tables", "estimated_row_count")
