"""Org admin: invite and manage members, departments and their data grants."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import invalidate_access_scopes
from app.audit import log_audit
from app.database import get_db
from app.models import Department, User
from app.routes.auth import require_role
from app.schemas import (
    DepartmentCreate,
    DepartmentGrantsUpdate,
    DepartmentMembersUpdate,
    DepartmentResponse,
    DepartmentUpdate,
    InviteResult,
    OrgUserInviteRequest,
    OrgUserInviteResponse,
    OrgUserResponse,
    OrgUserUpdate,
)
from app.services import org_service

router = APIRouter(prefix="/api/v1/org", tags=["org"])

require_org_admin = require_role("admin")


# ----- users -----


@router.get("/users", response_model=list[OrgUserResponse])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    return org_service.list_org_users(db, current_user.tenant_id)


@router.post(
    "/users/invite", response_model=OrgUserInviteResponse, status_code=status.HTTP_201_CREATED
)
def invite_user(
    body: OrgUserInviteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    user = org_service.create_invited_user(
        db,
        tenant_id=current_user.tenant_id,
        email=body.email,
        role=body.role,
        invited_by=current_user,
    )
    org_service.set_user_departments(db, user, body.department_ids)
    invite = org_service.issue_invite(db, user=user, invited_by=current_user)
    log_audit(
        db,
        user=current_user,
        action="org.user.invite",
        resource_type="user",
        resource_id=user.id,
        metadata={"email": user.email, "role": user.role},
    )
    db.commit()
    invalidate_access_scopes(current_user.tenant_id)
    return OrgUserInviteResponse(
        user=org_service.org_user_response(user, org_service.user_department_ids(db, user)),
        invite=invite,
    )


@router.post("/users/{user_id}/resend-invite", response_model=InviteResult)
def resend_invite(
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    user = org_service.get_org_user(db, tenant_id=current_user.tenant_id, user_id=user_id)
    if user.is_verified:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This person has already joined")
    invite = org_service.issue_invite(db, user=user, invited_by=current_user)
    log_audit(
        db,
        user=current_user,
        action="org.user.resend_invite",
        resource_type="user",
        resource_id=user.id,
    )
    db.commit()
    invalidate_access_scopes(current_user.tenant_id)
    return invite


@router.patch("/users/{user_id}", response_model=OrgUserResponse)
def update_user(
    user_id: UUID,
    body: OrgUserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    user = org_service.get_org_user(db, tenant_id=current_user.tenant_id, user_id=user_id)
    changes: dict[str, object] = {}
    if body.role is not None and body.role != user.role:
        if user.id == current_user.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't change your own role")
        org_service.ensure_not_last_admin(db, user)
        changes["role"] = {"from": user.role, "to": body.role}
        user.role = body.role
    if body.department_ids is not None:
        org_service.set_user_departments(db, user, body.department_ids)
        changes["department_ids"] = [str(d) for d in body.department_ids]
    log_audit(
        db,
        user=current_user,
        action="org.user.update",
        resource_type="user",
        resource_id=user.id,
        metadata=changes,
    )
    db.commit()
    invalidate_access_scopes(current_user.tenant_id)
    db.refresh(user)
    return org_service.org_user_response(user, org_service.user_department_ids(db, user))


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_user(
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    user = org_service.get_org_user(db, tenant_id=current_user.tenant_id, user_id=user_id)
    if user.id == current_user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't remove yourself")
    org_service.ensure_not_last_admin(db, user)
    org_service.remove_user(db, user)
    log_audit(
        db,
        user=current_user,
        action="org.user.remove",
        resource_type="user",
        resource_id=user.id,
        metadata={"email": user.email},
    )
    db.commit()
    invalidate_access_scopes(current_user.tenant_id)


# ----- departments -----


@router.get("/departments", response_model=list[DepartmentResponse])
def list_departments(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    rows = db.scalars(
        select(Department)
        .where(Department.tenant_id == current_user.tenant_id)
        .order_by(Department.name)
    ).all()
    return [org_service.department_response(db, d) for d in rows]


@router.post("/departments", response_model=DepartmentResponse, status_code=status.HTTP_201_CREATED)
def create_department(
    body: DepartmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    org_service.ensure_unique_department_name(db, tenant_id=current_user.tenant_id, name=body.name)
    dept = Department(
        tenant_id=current_user.tenant_id,
        name=body.name.strip(),
        description=(body.description or "").strip() or None,
    )
    db.add(dept)
    db.flush()
    log_audit(
        db,
        user=current_user,
        action="org.department.create",
        resource_type="department",
        resource_id=dept.id,
        metadata={"name": dept.name},
    )
    db.commit()
    invalidate_access_scopes(current_user.tenant_id)
    db.refresh(dept)
    return org_service.department_response(db, dept)


@router.patch("/departments/{department_id}", response_model=DepartmentResponse)
def update_department(
    department_id: UUID,
    body: DepartmentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    dept = org_service.get_department(
        db, tenant_id=current_user.tenant_id, department_id=department_id
    )
    if body.name is not None:
        org_service.ensure_unique_department_name(
            db, tenant_id=current_user.tenant_id, name=body.name, exclude_id=dept.id
        )
        dept.name = body.name.strip()
    if "description" in body.model_fields_set:
        dept.description = (body.description or "").strip() or None
    log_audit(
        db,
        user=current_user,
        action="org.department.update",
        resource_type="department",
        resource_id=dept.id,
    )
    db.commit()
    invalidate_access_scopes(current_user.tenant_id)
    db.refresh(dept)
    return org_service.department_response(db, dept)


@router.delete("/departments/{department_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_department(
    department_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    dept = org_service.get_department(
        db, tenant_id=current_user.tenant_id, department_id=department_id
    )
    name = dept.name
    org_service.delete_department(db, dept)
    log_audit(
        db,
        user=current_user,
        action="org.department.delete",
        resource_type="department",
        resource_id=department_id,
        metadata={"name": name},
    )
    db.commit()
    invalidate_access_scopes(current_user.tenant_id)


@router.put("/departments/{department_id}/members", response_model=DepartmentResponse)
def set_members(
    department_id: UUID,
    body: DepartmentMembersUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    dept = org_service.get_department(
        db, tenant_id=current_user.tenant_id, department_id=department_id
    )
    org_service.set_department_members(db, dept, body.user_ids)
    log_audit(
        db,
        user=current_user,
        action="org.department.members",
        resource_type="department",
        resource_id=dept.id,
        metadata={"user_ids": [str(u) for u in body.user_ids]},
    )
    db.commit()
    invalidate_access_scopes(current_user.tenant_id)
    return org_service.department_response(db, dept)


@router.put("/departments/{department_id}/grants", response_model=DepartmentResponse)
def set_grants(
    department_id: UUID,
    body: DepartmentGrantsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_org_admin),
):
    dept = org_service.get_department(
        db, tenant_id=current_user.tenant_id, department_id=department_id
    )
    org_service.set_department_grants(db, dept, body.grants)
    log_audit(
        db,
        user=current_user,
        action="org.department.grants",
        resource_type="department",
        resource_id=dept.id,
        metadata={
            "grants": [
                {"connection_id": str(g.connection_id), "schema_name": g.schema_name}
                for g in body.grants
            ]
        },
    )
    db.commit()
    invalidate_access_scopes(current_user.tenant_id)
    return org_service.department_response(db, dept)
