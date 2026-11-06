"""Dashboard CRUD, refresh, and version history."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import accessible_connection, filter_schema, scope_for_user
from app.audit import log_audit
from app.database import get_db
from app.models import Connection, DashboardVersion, User
from app.routes.auth import get_current_user, require_role
from app.schemas import (
    DashboardCreate,
    DashboardFiltersUpdate,
    DashboardResponse,
    DashboardSummaryResponse,
    DashboardVersionResponse,
    DashboardWidgetResponse,
)
from app.services.dashboard_service import (
    create_dashboard_from_spec,
    delete_dashboard as delete_dashboard_row,
    execute_dashboard_widgets,
    get_dashboard as load_dashboard,
    list_dashboards as list_dashboard_summaries,
    restore_version,
    update_dashboard_filters,
)
from app.services.semantic_service import load_tenant_semantic_catalog
from app.agent.dashboard_filters import validate_dashboard_filters
from app.tools.customer_db import get_stored_schema

router = APIRouter(prefix="/api/v1/dashboards", tags=["dashboards"])


def _to_response(payload: dict) -> DashboardResponse:
    widgets = [
        DashboardWidgetResponse.model_validate(widget)
        for widget in payload.get("widgets") or []
        if isinstance(widget, dict)
    ]
    return DashboardResponse(
        id=UUID(str(payload["id"])),
        name=str(payload.get("name") or payload.get("title") or "Dashboard"),
        tenant_id=UUID(str(payload["tenant_id"])),
        user_id=UUID(str(payload["user_id"])),
        connection_id=UUID(str(payload["connection_id"])),
        conversation_id=(
            UUID(str(payload["conversation_id"])) if payload.get("conversation_id") else None
        ),
        filters=payload.get("filters") or [],
        layout=payload.get("layout") or {},
        source_table=payload.get("source_table"),
        widgets=widgets,
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
    )


def _get_connection(db: Session, *, connection_id: UUID, user: User) -> Connection:
    """Connection the user's departments grant (admins: any); else 404."""
    conn = accessible_connection(db, connection_id=connection_id, user=user)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    return conn


def _scoped_schema(db: Session, conn: Connection, user: User) -> dict:
    """Stored schema limited to the schemas this user may read."""
    return filter_schema(
        get_stored_schema(db, conn.id), scope_for_user(db, user).allowed_schemas(conn.id)
    )


@router.post("", response_model=DashboardResponse, status_code=status.HTTP_201_CREATED)
def create_dashboard(
    request: DashboardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    _get_connection(db, connection_id=request.connection_id, user=current_user)
    spec = request.spec or {"title": request.name, "widgets": []}
    if "title" not in spec and "name" not in spec:
        spec = {**spec, "title": request.name}
    created = create_dashboard_from_spec(
        db,
        current_user,
        request.connection_id,
        spec,
        conversation_id=request.conversation_id,
    )
    log_audit(
        db,
        user=current_user,
        action="dashboard.create",
        resource_type="dashboard",
        resource_id=created["id"],
    )
    db.commit()
    return _to_response(created)


@router.get("", response_model=list[DashboardSummaryResponse])
def list_dashboards(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = list_dashboard_summaries(db, current_user.tenant_id, current_user.id)
    return [
        DashboardSummaryResponse(
            id=UUID(str(row["id"])),
            name=row["name"],
            connection_id=UUID(str(row["connection_id"])),
            conversation_id=(
                UUID(str(row["conversation_id"])) if row.get("conversation_id") else None
            ),
            widget_count=int(row.get("widget_count") or 0),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
        for row in rows
    ]


@router.get("/{dashboard_id}", response_model=DashboardResponse)
def get_dashboard(
    dashboard_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        payload = load_dashboard(db, dashboard_id, current_user)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    conn = _get_connection(
        db,
        connection_id=UUID(str(payload["connection_id"])),
        user=current_user,
    )
    schema = _scoped_schema(db, conn, current_user)
    executed = execute_dashboard_widgets(
        db,
        dashboard_id,
        conn.id,
        schema,
        conn.db_type,
        user=current_user,
    )
    payload["widgets"] = executed.get("widgets") or payload.get("widgets")
    return _to_response(payload)


@router.patch("/{dashboard_id}", response_model=DashboardResponse)
def patch_dashboard_filters(
    dashboard_id: UUID,
    body: DashboardFiltersUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    try:
        dashboard = load_dashboard(db, dashboard_id, current_user)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    semantic = load_tenant_semantic_catalog(
        db, current_user.tenant_id, UUID(str(dashboard["connection_id"]))
    )
    try:
        validated = validate_dashboard_filters(
            [item.model_dump() for item in body.filters],
            semantic,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    filters_payload = [f.model_dump() for f in validated]
    try:
        payload = update_dashboard_filters(
            db,
            dashboard_id=dashboard_id,
            user=current_user,
            filters=filters_payload,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    conn = _get_connection(
        db,
        connection_id=UUID(str(payload["connection_id"])),
        user=current_user,
    )
    schema = _scoped_schema(db, conn, current_user)
    executed = execute_dashboard_widgets(
        db,
        dashboard_id,
        conn.id,
        schema,
        conn.db_type,
        user=current_user,
    )
    payload = load_dashboard(db, dashboard_id, current_user)
    payload["widgets"] = executed.get("widgets") or payload.get("widgets")
    log_audit(
        db,
        user=current_user,
        action="dashboard.filters.update",
        resource_type="dashboard",
        resource_id=str(dashboard_id),
    )
    db.commit()
    return _to_response(payload)


@router.post("/{dashboard_id}/refresh", response_model=DashboardResponse)
def refresh_dashboard(
    dashboard_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    try:
        payload = load_dashboard(db, dashboard_id, current_user)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    conn = _get_connection(
        db,
        connection_id=UUID(str(payload["connection_id"])),
        user=current_user,
    )
    schema = _scoped_schema(db, conn, current_user)
    executed = execute_dashboard_widgets(
        db,
        dashboard_id,
        conn.id,
        schema,
        conn.db_type,
        user=current_user,
        use_cache=False,
    )
    payload = load_dashboard(db, dashboard_id, current_user)
    payload["widgets"] = executed.get("widgets") or payload.get("widgets")
    log_audit(
        db,
        user=current_user,
        action="dashboard.refresh",
        resource_type="dashboard",
        resource_id=str(dashboard_id),
    )
    db.commit()
    return _to_response(payload)


@router.delete("/{dashboard_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dashboard(
    dashboard_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    try:
        delete_dashboard_row(db, dashboard_id, current_user)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    log_audit(
        db,
        user=current_user,
        action="dashboard.delete",
        resource_type="dashboard",
        resource_id=str(dashboard_id),
    )
    db.commit()


@router.get("/{dashboard_id}/versions", response_model=list[DashboardVersionResponse])
def list_dashboard_versions(
    dashboard_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        load_dashboard(db, dashboard_id, current_user)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    rows = db.scalars(
        select(DashboardVersion)
        .where(
            DashboardVersion.dashboard_id == dashboard_id,
            DashboardVersion.tenant_id == current_user.tenant_id,
        )
        .order_by(DashboardVersion.version_number.desc())
    ).all()
    return [
        DashboardVersionResponse(
            version_number=row.version_number,
            created_at=row.created_at,
            created_by=row.created_by,
        )
        for row in rows
    ]


@router.post("/{dashboard_id}/restore/{version_number}", response_model=DashboardResponse)
def restore_dashboard_version(
    dashboard_id: UUID,
    version_number: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    try:
        restored = restore_version(
            db,
            dashboard_id,
            version_number,
            current_user,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    log_audit(
        db,
        user=current_user,
        action="dashboard.restore",
        resource_type="dashboard",
        resource_id=str(dashboard_id),
        metadata={"version_number": version_number},
    )
    db.commit()
    return _to_response(restored)
