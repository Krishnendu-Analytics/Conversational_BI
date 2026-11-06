"""Trigram indexes for sidebar search (Postgres only).

Revision ID: 0024_search_trigram_indexes
Revises: 0023_semantic_examples
Create Date: 2026-09-24

Search uses ILIKE '%term%', which a normal b-tree index can't serve.
pg_trgm GIN indexes can, so chat/dashboard search stays fast as message
history grows. Other databases (SQLite in tests) are left untouched.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0024_search_trigram_indexes"
down_revision: Union[str, None] = "0023_semantic_examples"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = (
    ("ix_messages_content_trgm", "messages", "content"),
    ("ix_conversations_title_trgm", "conversations", "title"),
    ("ix_dashboards_name_trgm", "dashboards", "name"),
    ("ix_dashboard_widgets_title_trgm", "dashboard_widgets", "title"),
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in _INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} USING gin ({column} gin_trgm_ops)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for name, _table, _column in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
