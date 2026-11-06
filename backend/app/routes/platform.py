"""Platform super admin: create organizations and their first admins."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.access import invalidate_access_scopes
from app.audit import log_audit
from app.database import get_db
from app.models import RefreshToken, Tenant, User
from app.routes.auth import get_current_user
from app.schemas import (
    InviteResult,
    PlatformOrgAdminCreate,
    PlatformOrgCreate,
    PlatformOrgCreateResponse,
    PlatformOrgResponse,
    PlatformOrgUpdate,
)
from app.services import org_service

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])


def require_super_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_super_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
    return current_user


def _org_response(db: Session, tenant: Tenant) -> PlatformOrgResponse:
    active = (User.tenant_id == tenant.id, User.deleted_at.is_(None))
    user_count = db.scalar(select(func.count()).select_from(User).where(*active)) or 0
    admin_emails = list(
        db.scalars(select(User.email).where(*active, User.role == "admin").order_by(User.email))
    )
    return PlatformOrgResponse(
        id=tenant.id,
        name=tenant.name,
        created_at=tenant.created_at,
        disabled=tenant.deleted_at is not None,
        user_count=int(user_count),
        admin_emails=admin_emails,
    )


def _get_org(db: Session, org_id: UUID) -> Tenant:
    tenant = db.get(Tenant, org_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    return tenant


@router.get("/orgs", response_model=list[PlatformOrgResponse])
def list_orgs(
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    tenants = db.scalars(select(Tenant).order_by(Tenant.name)).all()
    return [_org_response(db, t) for t in tenants]


@router.post("/orgs", response_model=PlatformOrgCreateResponse, status_code=status.HTTP_201_CREATED)
def create_org(
    body: PlatformOrgCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_super_admin),
):
    tenant = Tenant(name=body.name.strip())
    db.add(tenant)
    db.flush()
    admin = org_service.create_invited_user(
        db, tenant_id=tenant.id, email=body.admin_email, role="admin", invited_by=current_user
    )
    invite = org_service.issue_invite(db, user=admin, invited_by=current_user)
    log_audit(
        db,
        user=current_user,
        action="platform.org.create",
        resource_type="tenant",
        resource_id=tenant.id,
        metadata={"name": tenant.name, "admin_email": admin.email},
    )
    db.commit()
    db.refresh(tenant)
    return PlatformOrgCreateResponse(org=_org_response(db, tenant), invite=invite)


@router.patch("/orgs/{org_id}", response_model=PlatformOrgResponse)
def update_org(
    org_id: UUID,
    body: PlatformOrgUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_super_admin),
):
    tenant = _get_org(db, org_id)
    if body.name is not None:
        tenant.name = body.name.strip()
    if body.disabled is not None:
        if body.disabled and tenant.id == current_user.tenant_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't disable your own organization")
        now = datetime.now(timezone.utc)
        tenant.deleted_at = now if body.disabled else None
        if body.disabled:
            # Sign everyone out; access tokens lapse within their short lifetime
            # and login/refresh reject disabled orgs.
            member_ids = select(User.id).where(User.tenant_id == tenant.id)
            db.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id.in_(member_ids), RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
    log_audit(
        db,
        user=current_user,
        action="platform.org.update",
        resource_type="tenant",
        resource_id=tenant.id,
        metadata=body.model_dump(exclude_none=True),
    )
    db.commit()
    invalidate_access_scopes(tenant.id)
    db.refresh(tenant)
    return _org_response(db, tenant)


@router.post("/orgs/{org_id}/admins", response_model=InviteResult, status_code=status.HTTP_201_CREATED)
def add_org_admin(
    org_id: UUID,
    body: PlatformOrgAdminCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_super_admin),
):
    """Invite another admin (or re-invite a pending one) into an organization."""
    tenant = _get_org(db, org_id)
    email = str(body.email).strip().lower()
    existing = db.scalar(
        select(User).where(User.email == email, User.tenant_id == tenant.id, User.deleted_at.is_(None))
    )
    if existing is not None:
        if existing.is_verified:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This person has already joined")
        existing.role = "admin"
        user = existing
    else:
        user = org_service.create_invited_user(
            db, tenant_id=tenant.id, email=email, role="admin", invited_by=current_user
        )
    invite = org_service.issue_invite(db, user=user, invited_by=current_user)
    log_audit(
        db,
        user=current_user,
        action="platform.org.invite_admin",
        resource_type="tenant",
        resource_id=tenant.id,
        metadata={"email": user.email},
    )
    db.commit()
    return invite
