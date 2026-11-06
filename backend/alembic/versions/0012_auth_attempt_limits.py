"""OTP verify attempt counters and lockout timestamps.

Revision ID: 0012_auth_attempt_limits
Revises: 0011_schema_discovery_semantic_scope
Create Date: 2026-09-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_auth_attempt_limits"
down_revision: Union[str, None] = "0011_schema_discovery_semantic_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("otp_verify_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("otp_locked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "otp_locked_until")
    op.drop_column("users", "otp_verify_attempts")
