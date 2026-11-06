"""Tenant-scoped resource authorization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Connection, Conversation, Dashboard, User
from app.security import TokenManager


class DashboardConnectionMismatchError(ValueError):
    """Raised when request context connection differs from dashboard.connection_id."""

    code = "dashboard_connection_mismatch"

    def __init__(
        self,
        message: str = "Conversation connection does not match dashboard connection",
    ) -> None:
        super().__init__(message)

ANALYTICS_ROLES = frozenset({"admin", "analyst"})


@dataclass(frozen=True)
class UserContext:
    user_id: UUID
    tenant_id: UUID
    role: str


def user_context(user: User) -> UserContext:
    return UserContext(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
    )


def can_run_analytics(role: str) -> bool:
    return role in ANALYTICS_ROLES


def resolve_user_from_access_token(
    token: str,
    db: Session,
    *,
    require_verified: bool = True,
) -> User | None:
    """Validate access JWT and load a non-deleted user."""
    try:
        payload = TokenManager.decode_access_token(token)
        user_id = UUID(str(payload["sub"]))
        tenant_id = UUID(str(payload["tenant_id"]))
    except (ValueError, KeyError, TypeError):
        return None

    user = db.get(User, user_id)
    if user is None:
        return None
    if user.tenant_id != tenant_id:
        return None
    if user.deleted_at is not None:
        return None
    if require_verified and not user.is_verified:
        return None
    return user


def get_tenant_connection(
    db: Session,
    *,
    connection_id: UUID,
    tenant_id: UUID,
) -> Connection | None:
    return db.scalar(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.tenant_id == tenant_id,
        )
    )


def get_user_conversation(
    db: Session,
    *,
    conversation_id: UUID,
    user: User,
) -> Conversation | None:
    return db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == user.tenant_id,
            Conversation.user_id == user.id,
            Conversation.deleted_at.is_(None),
        )
    )


def authorize_conversation_for_ask(
    db: Session,
    *,
    conversation_id: UUID,
    user: User,
) -> bool:
    """Return True when the conversation is new or owned by the current user."""
    row = db.get(Conversation, conversation_id)
    if row is None:
        return True
    if row.deleted_at is not None:
        return False
    if row.tenant_id != user.tenant_id:
        return False
    if row.user_id != user.id:
        return False
    return True


def get_user_dashboard(
    db: Session,
    *,
    dashboard_id: UUID,
    user: User,
) -> Dashboard | None:
    return db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.tenant_id == user.tenant_id,
            Dashboard.user_id == user.id,
            Dashboard.deleted_at.is_(None),
        )
    )


def resolve_dashboard_execution_connection(
    db: Session,
    *,
    dashboard: dict[str, Any] | Dashboard,
    request_connection_id: UUID | None,
    tenant_id: UUID,
) -> UUID:
    """Return authoritative dashboard.connection_id; reject on mismatch or missing connection."""
    if isinstance(dashboard, dict):
        dashboard_connection_id = UUID(str(dashboard["connection_id"]))
    else:
        dashboard_connection_id = dashboard.connection_id

    conn = get_tenant_connection(
        db,
        connection_id=dashboard_connection_id,
        tenant_id=tenant_id,
    )
    if conn is None:
        raise ValueError("Dashboard connection not found or access denied")

    if (
        request_connection_id is not None
        and request_connection_id != dashboard_connection_id
    ):
        raise DashboardConnectionMismatchError()

    return dashboard_connection_id
