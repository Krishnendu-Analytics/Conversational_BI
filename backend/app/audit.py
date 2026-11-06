"""Append-only audit log helper."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import AuditLog, User


def _insert_audit_row(
    db: Session,
    *,
    tenant_id: UUID | None,
    user_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None,
    metadata: dict[str, Any] | None,
) -> None:
    if tenant_id is None:
        return

    row = AuditLog(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        metadata_json=json.dumps(metadata or {}, default=str),
    )
    db.add(row)


def log_audit(
    db: Session,
    *,
    user: User | None,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a tenant-scoped audit event (no secrets in metadata)."""
    _insert_audit_row(
        db,
        tenant_id=user.tenant_id if user is not None else None,
        user_id=user.id if user is not None else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=metadata,
    )


def log_agent_audit(
    db: Session,
    *,
    tenant_id: str | UUID | None,
    user_id: str | UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Audit helper for agent/investigation code paths (``app.agent.*``)
    that only have raw tenant/user id strings from workflow state, not a
    loaded ``User`` ORM object the way request handlers do. Invalid ids are
    treated as absent rather than raising, since callers here run inside a
    best-effort "never fail the answer over audit logging" pattern."""
    try:
        tid = UUID(str(tenant_id)) if tenant_id else None
    except (ValueError, TypeError):
        tid = None
    try:
        uid = UUID(str(user_id)) if user_id else None
    except (ValueError, TypeError):
        uid = None
    _insert_audit_row(
        db,
        tenant_id=tid,
        user_id=uid,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=metadata,
    )
