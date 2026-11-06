"""Organization administration: invites, members, departments and grants.

Routes stay thin; the rules live here:
- an org always keeps at least one active admin;
- department members and grant connections must belong to the same org;
- every grant/membership change drops cached access scopes.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.access import invalidate_access_scopes
from app.config import get_settings
from app.email_service import send_invite_email
from app.models import (
    Connection,
    Department,
    DepartmentGrant,
    DepartmentMember,
    RefreshToken,
    Tenant,
    User,
    UserInvite,
)
from app.schemas import (
    DepartmentGrantItem,
    DepartmentResponse,
    InviteResult,
    OrgUserResponse,
)
from app.security import PasswordManager, TokenManager


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ----- invites -----


def hash_invite_token(token: str) -> str:
    # Same keyed HMAC as refresh tokens: the raw token never touches the DB.
    return TokenManager.hash_refresh_token(f"invite:{token}")


def issue_invite(db: Session, *, user: User, invited_by: User | None) -> InviteResult:
    """Revoke earlier open invites for the user, create a new one and email it.
    Caller commits."""
    settings = get_settings()
    db.execute(
        update(UserInvite)
        .where(
            UserInvite.user_id == user.id,
            UserInvite.accepted_at.is_(None),
            UserInvite.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    token = secrets.token_urlsafe(32)
    db.add(
        UserInvite(
            tenant_id=user.tenant_id,
            user_id=user.id,
            token_hash=hash_invite_token(token),
            invited_by=invited_by.id if invited_by is not None else None,
            expires_at=_now() + timedelta(days=settings.invite_expire_days),
        )
    )
    db.flush()
    tenant = db.get(Tenant, user.tenant_id)
    url = f"{settings.app_base_url.rstrip('/')}/accept-invite?token={token}"
    sent = send_invite_email(
        user.email, org_name=tenant.name if tenant else "your organization", invite_url=url
    )
    return InviteResult(email_sent=sent, invite_url=None if sent else url)


def find_open_invite(db: Session, token: str) -> UserInvite | None:
    invite = db.scalar(select(UserInvite).where(UserInvite.token_hash == hash_invite_token(token)))
    if invite is None or invite.accepted_at is not None or invite.revoked_at is not None:
        return None
    if _now() > _aware(invite.expires_at):
        return None
    user = db.get(User, invite.user_id)
    if user is None or user.deleted_at is not None:
        return None
    tenant = db.get(Tenant, invite.tenant_id)
    if tenant is None or tenant.deleted_at is not None:
        return None
    return invite


def accept_invite(db: Session, *, token: str, password: str) -> User:
    invite = find_open_invite(db, token)
    if invite is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This invite link is invalid or has expired")
    user = db.get(User, invite.user_id)
    assert user is not None
    user.password_hash = PasswordManager.hash(password)
    user.is_verified = True
    user.otp_hash = None
    user.otp_expires_at = None
    invite.accepted_at = _now()
    db.flush()
    return user


def create_invited_user(
    db: Session,
    *,
    tenant_id: UUID,
    email: str,
    role: str,
    invited_by: User | None,
) -> User:
    """New (or restored) unverified user in this org. Emails are globally unique,
    so an address that belongs to another org is rejected. Caller commits."""
    email = email.strip().lower()
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        if existing.tenant_id != tenant_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email already registered")
        if existing.deleted_at is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This person is already in the organization")
        existing.deleted_at = None
        existing.is_verified = False
        existing.role = role
        existing.password_hash = PasswordManager.hash(secrets.token_urlsafe(32))
        db.flush()
        return existing

    user = User(
        tenant_id=tenant_id,
        email=email,
        role=role,
        is_verified=False,
        # Unusable until the invite sets a real one.
        password_hash=PasswordManager.hash(secrets.token_urlsafe(32)),
    )
    db.add(user)
    db.flush()
    return user


# ----- users -----


def _department_ids_by_user(db: Session, tenant_id: UUID) -> dict[UUID, list[UUID]]:
    out: dict[UUID, list[UUID]] = {}
    for dept_id, user_id in db.execute(
        select(DepartmentMember.department_id, DepartmentMember.user_id).where(
            DepartmentMember.tenant_id == tenant_id
        )
    ).all():
        out.setdefault(user_id, []).append(dept_id)
    return out


def org_user_response(user: User, department_ids: list[UUID]) -> OrgUserResponse:
    return OrgUserResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        status="active" if user.is_verified else "invited",
        department_ids=department_ids,
        created_at=user.created_at,
    )


def list_org_users(db: Session, tenant_id: UUID) -> list[OrgUserResponse]:
    users = db.scalars(
        select(User)
        .where(User.tenant_id == tenant_id, User.deleted_at.is_(None))
        .order_by(User.email)
    ).all()
    by_user = _department_ids_by_user(db, tenant_id)
    return [org_user_response(u, by_user.get(u.id, [])) for u in users]


def get_org_user(db: Session, *, tenant_id: UUID, user_id: UUID) -> User:
    user = db.get(User, user_id)
    if user is None or user.tenant_id != tenant_id or user.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


def user_department_ids(db: Session, user: User) -> list[UUID]:
    return list(
        db.scalars(select(DepartmentMember.department_id).where(DepartmentMember.user_id == user.id))
    )


def _active_admin_count(db: Session, tenant_id: UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.tenant_id == tenant_id,
                User.deleted_at.is_(None),
                User.role == "admin",
            )
        )
        or 0
    )


def ensure_not_last_admin(db: Session, user: User) -> None:
    if user.role == "admin" and _active_admin_count(db, user.tenant_id) <= 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "An organization needs at least one admin"
        )


def _require_departments(db: Session, tenant_id: UUID, department_ids: list[UUID]) -> list[UUID]:
    wanted = list(dict.fromkeys(department_ids))
    if not wanted:
        return []
    found = set(
        db.scalars(
            select(Department.id).where(Department.tenant_id == tenant_id, Department.id.in_(wanted))
        )
    )
    if len(found) != len(wanted):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown department")
    return wanted


def set_user_departments(db: Session, user: User, department_ids: list[UUID]) -> None:
    wanted = _require_departments(db, user.tenant_id, department_ids)
    db.execute(delete(DepartmentMember).where(DepartmentMember.user_id == user.id))
    for dept_id in wanted:
        db.add(DepartmentMember(department_id=dept_id, user_id=user.id, tenant_id=user.tenant_id))
    db.flush()
    invalidate_access_scopes(user.tenant_id)


def remove_user(db: Session, user: User) -> None:
    """Soft-delete, sign out everywhere, drop memberships and open invites."""
    now = _now()
    user.deleted_at = now
    db.execute(delete(DepartmentMember).where(DepartmentMember.user_id == user.id))
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.execute(
        update(UserInvite)
        .where(
            UserInvite.user_id == user.id,
            UserInvite.accepted_at.is_(None),
            UserInvite.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    db.flush()
    invalidate_access_scopes(user.tenant_id)


# ----- departments -----


def department_response(db: Session, dept: Department) -> DepartmentResponse:
    member_ids = list(
        db.scalars(
            select(DepartmentMember.user_id).where(DepartmentMember.department_id == dept.id)
        )
    )
    grants = db.scalars(
        select(DepartmentGrant)
        .where(DepartmentGrant.department_id == dept.id)
        .order_by(DepartmentGrant.connection_id, DepartmentGrant.schema_name)
    ).all()
    return DepartmentResponse(
        id=dept.id,
        name=dept.name,
        description=dept.description,
        member_ids=member_ids,
        grants=[
            DepartmentGrantItem(connection_id=g.connection_id, schema_name=g.schema_name)
            for g in grants
        ],
    )


def get_department(db: Session, *, tenant_id: UUID, department_id: UUID) -> Department:
    dept = db.get(Department, department_id)
    if dept is None or dept.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    return dept


def ensure_unique_department_name(
    db: Session, *, tenant_id: UUID, name: str, exclude_id: UUID | None = None
) -> None:
    q = select(Department.id).where(
        Department.tenant_id == tenant_id, func.lower(Department.name) == name.strip().lower()
    )
    if exclude_id is not None:
        q = q.where(Department.id != exclude_id)
    if db.scalar(q) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A department with that name already exists")


def set_department_members(db: Session, dept: Department, user_ids: list[UUID]) -> None:
    wanted = list(dict.fromkeys(user_ids))
    if wanted:
        found = set(
            db.scalars(
                select(User.id).where(
                    User.tenant_id == dept.tenant_id,
                    User.deleted_at.is_(None),
                    User.id.in_(wanted),
                )
            )
        )
        if len(found) != len(wanted):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown user")
    db.execute(delete(DepartmentMember).where(DepartmentMember.department_id == dept.id))
    for user_id in wanted:
        db.add(DepartmentMember(department_id=dept.id, user_id=user_id, tenant_id=dept.tenant_id))
    db.flush()
    invalidate_access_scopes(dept.tenant_id)


def set_department_grants(
    db: Session, dept: Department, grants: list[DepartmentGrantItem]
) -> None:
    """Replace the department's grants. A whole-connection grant makes schema
    grants on the same connection redundant, so they're dropped."""
    conn_ids = {g.connection_id for g in grants}
    if conn_ids:
        found = set(
            db.scalars(
                select(Connection.id).where(
                    Connection.tenant_id == dept.tenant_id, Connection.id.in_(conn_ids)
                )
            )
        )
        if found != conn_ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown connection")

    whole = {g.connection_id for g in grants if not (g.schema_name or "").strip()}
    rows: dict[tuple[UUID, str | None], None] = {}
    for g in grants:
        schema = (g.schema_name or "").strip() or None
        if schema is not None and g.connection_id in whole:
            continue
        rows[(g.connection_id, schema)] = None

    db.execute(delete(DepartmentGrant).where(DepartmentGrant.department_id == dept.id))
    for connection_id, schema in rows:
        db.add(
            DepartmentGrant(
                tenant_id=dept.tenant_id,
                department_id=dept.id,
                connection_id=connection_id,
                schema_name=schema,
            )
        )
    db.flush()
    invalidate_access_scopes(dept.tenant_id)


def delete_department(db: Session, dept: Department) -> None:
    db.execute(delete(DepartmentGrant).where(DepartmentGrant.department_id == dept.id))
    db.execute(delete(DepartmentMember).where(DepartmentMember.department_id == dept.id))
    db.delete(dept)
    db.flush()
    invalidate_access_scopes(dept.tenant_id)
