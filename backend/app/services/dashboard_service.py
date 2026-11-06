"""CRUD and execution for persistent dashboards."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.dashboard import execute_dashboard
from app.agent.dashboard_insight_service import (
    merge_insight_config_into_widget_config,
    refresh_ai_insight_widgets,
)
from app.authorization import resolve_dashboard_execution_connection
from app.services.semantic_service import load_tenant_semantic_catalog
from app.models import Conversation, Dashboard, DashboardVersion, DashboardWidget, User


def _widget_to_dict(widget: DashboardWidget) -> dict[str, Any]:
    config = {}
    position = {}
    try:
        config = json.loads(widget.config_json or "{}")
    except json.JSONDecodeError:
        config = {}
    try:
        position = json.loads(widget.position_json or "{}")
    except json.JSONDecodeError:
        position = {}
    return {
        "id": str(widget.id),
        "type": widget.type,
        "title": widget.title,
        "sql": widget.sql_template,
        "chart": config.get("chart"),
        "config": config,
        "position": position,
        "rows": config.get("rows", []),
        "value": config.get("value"),
        "error": config.get("error"),
        "content": config.get("content"),
        "insight_status": config.get("insight_status"),
        "insight_generated_at": config.get("generated_at"),
        "result": config.get("result"),
        "value_kind": config.get("value_kind"),
        "aggregation": config.get("aggregation"),
        "metric": config.get("metric"),
        "dimension": config.get("dimension"),
        "purpose": config.get("purpose"),
        "quality": config.get("quality"),
        # Whole-database dashboards group widgets by business domain.
        "section": config.get("section"),
        # "higher_better" / "lower_better": colours a KPI's change as good or bad.
        "polarity": config.get("polarity"),
        # Multi-series widgets (e.g. actual vs target) name each value column.
        "measure_labels": config.get("measure_labels"),
        # Display unit ("MT", "days") and a one-line definition for overview KPIs.
        "unit": config.get("unit"),
        "note": config.get("note"),
        # Set when the value is implausible (e.g. targets off in scale).
        "data_warning": config.get("data_warning"),
        "period_kind": config.get("period_kind"),
        # "document" widgets hold rows extracted from an attached file.
        "source": config.get("source"),
    }


def _dashboard_to_dict(dashboard: Dashboard, widgets: list[DashboardWidget]) -> dict[str, Any]:
    try:
        filters = json.loads(dashboard.filters_json or "[]")
    except json.JSONDecodeError:
        filters = []
    try:
        layout = json.loads(dashboard.layout_json or "{}")
    except json.JSONDecodeError:
        layout = {}
    return {
        "id": str(dashboard.id),
        "name": dashboard.name,
        "title": dashboard.name,
        "tenant_id": str(dashboard.tenant_id),
        "user_id": str(dashboard.user_id),
        "connection_id": str(dashboard.connection_id),
        "conversation_id": str(dashboard.conversation_id) if dashboard.conversation_id else None,
        "filters": filters,
        "layout": layout,
        "source_table": layout.get("source_table"),
        "widgets": [_widget_to_dict(widget) for widget in widgets],
        "created_at": dashboard.created_at.isoformat() if dashboard.created_at else None,
        "updated_at": dashboard.updated_at.isoformat() if dashboard.updated_at else None,
    }


def _snapshot_from_dashboard(dashboard: Dashboard, widgets: list[DashboardWidget]) -> dict[str, Any]:
    return _dashboard_to_dict(dashboard, widgets)


def _widget_created_at(base: datetime, idx: int) -> datetime:
    """Widgets are read back ordered by created_at; rows inserted in one
    transaction would all share now(), so space them 1 ms apart in spec
    order to keep the dashboard in the order it was designed."""
    return base + timedelta(milliseconds=idx)


def create_dashboard_from_spec(
    db: Session,
    user: User,
    connection_id: UUID,
    spec: dict[str, Any],
    conversation_id: UUID | None = None,
) -> dict[str, Any]:
    """Persist a dashboard and widgets from an in-memory spec."""
    name = str(spec.get("title") or spec.get("name") or "Dashboard").strip()
    filters = spec.get("filters") or []
    layout = spec.get("layout") or {}
    if spec.get("source_table") and "source_table" not in layout:
        layout = {**layout, "source_table": spec.get("source_table")}

    dashboard = Dashboard(
        id=uuid4(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        connection_id=connection_id,
        conversation_id=conversation_id,
        name=name,
        filters_json=json.dumps(filters, default=str),
        layout_json=json.dumps(layout, default=str),
    )
    db.add(dashboard)

    widget_rows: list[DashboardWidget] = []
    base_time = datetime.now(timezone.utc)
    for idx, raw in enumerate(spec.get("widgets") or []):
        if not isinstance(raw, dict):
            continue
        widget_id = raw.get("id")
        config = merge_insight_config_into_widget_config(raw)
        widget = DashboardWidget(
            id=UUID(str(widget_id)) if widget_id and _is_uuid(str(widget_id)) else uuid4(),
            dashboard_id=dashboard.id,
            tenant_id=user.tenant_id,
            type=str(raw.get("type") or "kpi"),
            title=str(raw.get("title") or f"Widget {idx + 1}"),
            config_json=json.dumps(config, default=str),
            position_json=json.dumps(raw.get("position") or {}, default=str),
            sql_template=str(raw.get("sql") or raw.get("sql_template") or "") or None,
            created_at=_widget_created_at(base_time, idx),
        )
        db.add(widget)
        widget_rows.append(widget)

    db.commit()
    db.refresh(dashboard)
    return _dashboard_to_dict(dashboard, widget_rows)


def update_dashboard_from_spec(
    db: Session,
    *,
    dashboard_id: UUID,
    user: User,
    spec: dict[str, Any],
    save_version: bool = True,
) -> dict[str, Any]:
    """Replace dashboard widgets from an in-memory spec."""
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.tenant_id == user.tenant_id,
            Dashboard.user_id == user.id,
            Dashboard.deleted_at.is_(None),
        )
    )
    if dashboard is None:
        raise ValueError("Dashboard not found")

    if save_version:
        save_version_snapshot(db, dashboard_id, user)

    if spec.get("title") or spec.get("name"):
        dashboard.name = str(spec.get("title") or spec.get("name")).strip()
    if spec.get("filters") is not None:
        dashboard.filters_json = json.dumps(spec.get("filters") or [], default=str)
    layout = spec.get("layout") or {}
    if spec.get("source_table") and "source_table" not in layout:
        layout = {**layout, "source_table": spec.get("source_table")}
    dashboard.layout_json = json.dumps(layout, default=str)

    existing = list(
        db.scalars(
            select(DashboardWidget).where(DashboardWidget.dashboard_id == dashboard.id)
        ).all()
    )
    for row in existing:
        db.delete(row)
    db.flush()

    widget_rows: list[DashboardWidget] = []
    base_time = datetime.now(timezone.utc)
    for idx, raw in enumerate(spec.get("widgets") or []):
        if not isinstance(raw, dict):
            continue
        widget_id = raw.get("id")
        config = merge_insight_config_into_widget_config(raw)
        widget = DashboardWidget(
            id=UUID(str(widget_id)) if widget_id and _is_uuid(str(widget_id)) else uuid4(),
            dashboard_id=dashboard.id,
            tenant_id=user.tenant_id,
            type=str(raw.get("type") or "kpi"),
            title=str(raw.get("title") or f"Widget {idx + 1}"),
            config_json=json.dumps(config, default=str),
            position_json=json.dumps(raw.get("position") or {}, default=str),
            sql_template=str(raw.get("sql") or raw.get("sql_template") or "") or None,
            created_at=_widget_created_at(base_time, idx),
        )
        db.add(widget)
        widget_rows.append(widget)

    dashboard.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(dashboard)
    return _dashboard_to_dict(dashboard, widget_rows)


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def get_dashboard(db: Session, dashboard_id: UUID, user: User) -> dict[str, Any]:
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.tenant_id == user.tenant_id,
            Dashboard.user_id == user.id,
            Dashboard.deleted_at.is_(None),
        )
    )
    if dashboard is None:
        raise ValueError("Dashboard not found")

    widgets = list(
        db.scalars(
            select(DashboardWidget)
            .where(
                DashboardWidget.dashboard_id == dashboard.id,
                DashboardWidget.tenant_id == user.tenant_id,
            )
            .order_by(DashboardWidget.created_at)
        ).all()
    )
    return _dashboard_to_dict(dashboard, widgets)


def delete_dashboard(db: Session, dashboard_id: UUID, user: User) -> None:
    """Soft-delete: same `deleted_at` convention every read path here already
    filters on, so no other query needs to change."""
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.tenant_id == user.tenant_id,
            Dashboard.user_id == user.id,
            Dashboard.deleted_at.is_(None),
        )
    )
    if dashboard is None:
        raise ValueError("Dashboard not found")
    dashboard.deleted_at = datetime.now(timezone.utc)
    # Any conversation still pointing at this dashboard would otherwise hit
    # "Dashboard not found" the next time it tries to edit/resolve it —
    # unlink so it instead gets the friendlier "No dashboard is open" path.
    for conv in db.scalars(
        select(Conversation).where(Conversation.dashboard_id == dashboard_id)
    ):
        conv.dashboard_id = None
    # Caller commits (kept in the same transaction as its own audit log entry).


def list_dashboards(db: Session, tenant_id: UUID, user_id: UUID) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Dashboard)
        .where(
            Dashboard.tenant_id == tenant_id,
            Dashboard.user_id == user_id,
            Dashboard.deleted_at.is_(None),
        )
        .order_by(Dashboard.updated_at.desc())
    ).all()
    summaries: list[dict[str, Any]] = []
    for row in rows:
        widget_count = db.scalar(
            select(func.count())
            .select_from(DashboardWidget)
            .where(DashboardWidget.dashboard_id == row.id)
        )
        summaries.append(
            {
                "id": str(row.id),
                "name": row.name,
                "connection_id": str(row.connection_id),
                "conversation_id": str(row.conversation_id) if row.conversation_id else None,
                "widget_count": int(widget_count or 0),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )
    return summaries


def update_dashboard_filters(
    db: Session,
    *,
    dashboard_id: UUID,
    user: User,
    filters: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replace dashboard filters (caller must validate against semantic catalog)."""
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.tenant_id == user.tenant_id,
            Dashboard.user_id == user.id,
            Dashboard.deleted_at.is_(None),
        )
    )
    if dashboard is None:
        raise ValueError("Dashboard not found")

    dashboard.filters_json = json.dumps(filters, default=str)
    dashboard.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(dashboard)

    widgets = list(
        db.scalars(
            select(DashboardWidget).where(DashboardWidget.dashboard_id == dashboard.id)
        ).all()
    )
    return _dashboard_to_dict(dashboard, widgets)


def execute_dashboard_widgets(
    db: Session,
    dashboard_id: UUID,
    connection_id: UUID,
    schema: dict[str, Any],
    db_type: str,
    *,
    user: User,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Run the dashboard's widgets and store their results. Opening a
    dashboard reuses recent results from Redis; Refresh passes
    ``use_cache=False`` to re-query the database."""
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.tenant_id == user.tenant_id,
            Dashboard.user_id == user.id,
            Dashboard.deleted_at.is_(None),
        )
    )
    if dashboard is None:
        raise ValueError("Dashboard not found")

    execution_connection_id = resolve_dashboard_execution_connection(
        db,
        dashboard=dashboard,
        request_connection_id=connection_id,
        tenant_id=user.tenant_id,
    )

    widgets = list(
        db.scalars(
            select(DashboardWidget)
            .where(DashboardWidget.dashboard_id == dashboard.id)
            .order_by(DashboardWidget.created_at)
        ).all()
    )
    spec = _dashboard_to_dict(dashboard, widgets)
    semantic = load_tenant_semantic_catalog(db, user.tenant_id, execution_connection_id)
    executed = execute_dashboard(
        spec,
        connection_id=str(execution_connection_id),
        schema=schema,
        db_type=db_type,
        tenant_id=user.tenant_id,
        semantic=semantic,
        use_cache=use_cache,
    )

    # Match results to rows by id, not position: execution caps and prunes
    # the widget list, so positions shift (and an insight's cache would land
    # on a chart).
    executed_by_id = {
        str(raw.get("id")): raw
        for raw in executed.get("widgets") or []
        if isinstance(raw, dict) and raw.get("id")
    }
    for widget in widgets:
        raw = executed_by_id.get(str(widget.id))
        if raw is None:
            continue
        config = {}
        try:
            config = json.loads(widget.config_json or "{}")
        except json.JSONDecodeError:
            config = {}
        if isinstance(raw, dict):
            if raw.get("rows") is not None:
                config["rows"] = raw.get("rows")
            if raw.get("value") is not None:
                config["value"] = raw.get("value")
            if raw.get("error"):
                config["error"] = raw.get("error")
            # Recomputed on every run: a warning that no longer applies goes.
            if raw.get("data_warning"):
                config["data_warning"] = raw.get("data_warning")
            else:
                config.pop("data_warning", None)
            if raw.get("sql"):
                config["executed_sql"] = str(raw.get("sql"))
            raw_config = raw.get("config") or {}
            for key in (
                "content",
                "insight_status",
                "insight_facts",
                "facts_hash",
                "generated_at",
                "narrator",
            ):
                if raw.get(key) is not None:
                    config[key] = raw.get(key)
                elif raw_config.get(key) is not None:
                    config[key] = raw_config[key]
        widget.config_json = json.dumps(config, default=str)

    dashboard.updated_at = datetime.now(timezone.utc)
    db.commit()
    return executed


def save_version_snapshot(db: Session, dashboard_id: UUID, user: User) -> dict[str, Any]:
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.tenant_id == user.tenant_id,
            Dashboard.user_id == user.id,
            Dashboard.deleted_at.is_(None),
        )
    )
    if dashboard is None:
        raise ValueError("Dashboard not found")

    widgets = list(
        db.scalars(
            select(DashboardWidget).where(DashboardWidget.dashboard_id == dashboard.id)
        ).all()
    )
    latest = db.scalar(
        select(func.max(DashboardVersion.version_number)).where(
            DashboardVersion.dashboard_id == dashboard.id
        )
    )
    version_number = int(latest or 0) + 1
    snapshot = _snapshot_from_dashboard(dashboard, widgets)
    row = DashboardVersion(
        dashboard_id=dashboard.id,
        tenant_id=user.tenant_id,
        version_number=version_number,
        snapshot_json=json.dumps(snapshot, default=str),
        created_by=user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "version_number": row.version_number,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by": str(row.created_by) if row.created_by else None,
    }


def restore_version(
    db: Session,
    dashboard_id: UUID,
    version_number: int,
    user: User,
) -> dict[str, Any]:
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.tenant_id == user.tenant_id,
            Dashboard.user_id == user.id,
            Dashboard.deleted_at.is_(None),
        )
    )
    if dashboard is None:
        raise ValueError("Dashboard not found")

    version = db.scalar(
        select(DashboardVersion).where(
            DashboardVersion.dashboard_id == dashboard.id,
            DashboardVersion.version_number == version_number,
            DashboardVersion.tenant_id == user.tenant_id,
        )
    )
    if version is None:
        raise ValueError("Dashboard version not found")

    snapshot = json.loads(version.snapshot_json or "{}")
    dashboard.name = str(snapshot.get("name") or snapshot.get("title") or dashboard.name)
    dashboard.filters_json = json.dumps(snapshot.get("filters") or [], default=str)
    dashboard.layout_json = json.dumps(snapshot.get("layout") or {}, default=str)
    dashboard.updated_at = datetime.now(timezone.utc)

    db.query(DashboardWidget).filter(DashboardWidget.dashboard_id == dashboard.id).delete()
    widget_rows: list[DashboardWidget] = []
    base_time = datetime.now(timezone.utc)
    for idx, raw in enumerate(snapshot.get("widgets") or []):
        if not isinstance(raw, dict):
            continue
        widget_id = raw.get("id")
        config = dict(raw.get("config") or {})
        if raw.get("chart") is not None:
            config["chart"] = raw.get("chart")
        widget = DashboardWidget(
            id=UUID(str(widget_id)) if widget_id and _is_uuid(str(widget_id)) else uuid4(),
            dashboard_id=dashboard.id,
            tenant_id=user.tenant_id,
            type=str(raw.get("type") or "kpi"),
            title=str(raw.get("title") or "Widget"),
            config_json=json.dumps(config, default=str),
            position_json=json.dumps(raw.get("position") or {}, default=str),
            sql_template=str(raw.get("sql") or "") or None,
            created_at=_widget_created_at(base_time, idx),
        )
        db.add(widget)
        widget_rows.append(widget)

    db.commit()
    db.refresh(dashboard)
    return _dashboard_to_dict(dashboard, widget_rows)
