"""Structured conversational dashboard operation tests."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

import app.agent.workflow as workflow_mod
from app.agent.analysis_state import AnalysisState, DashboardFocus, merge_workflow_into_state
from app.agent.dashboard_edit_pipeline import run_dashboard_edit_pipeline
from app.agent.dashboard_operation_applier import apply_resolved_operations
from app.agent.dashboard_operation_schema import (
    ClarificationChoice,
    DashboardEditResult,
    DashboardOperationItem,
    DashboardOperationPlan,
    FilterSpec,
    PendingDashboardOp,
    WidgetAddSpec,
    WidgetPatch,
    WidgetRef,
)
from app.agent.dashboard_operation_validator import validate_operation_plan
from app.agent.dashboard_widget_catalog import build_widget_catalog
from app.agent.dashboard_widget_resolver import resolve_widget_target
from app.agent.events import DashboardUpdatedEvent, ProgressEventType
from app.models import Tenant, User
from app.services.dashboard_service import create_dashboard_from_spec, get_dashboard


ORDERS_SCHEMA = {
    "db_type": "postgres",
    "tables": [
        {
            "schema_name": "public",
            "table_name": "orders",
            "columns": [
                {"name": "order_id", "data_type": "integer"},
                {"name": "region", "data_type": "varchar"},
                {"name": "amount", "data_type": "numeric"},
                {"name": "order_date", "data_type": "date"},
            ],
        },
    ],
}

SEMANTIC = {
    "metrics": [
        {"name": "Revenue", "expression": "SUM(public.orders.amount)"},
        {"name": "Profit Margin", "expression": "SUM(public.orders.amount) * 0.1"},
    ],
    "dimensions": [
        {"name": "Region", "expression": "public.orders.region"},
    ],
}


def _sample_dashboard(dashboard_id: UUID | None = None) -> dict[str, Any]:
    did = str(dashboard_id or uuid4())
    return {
        "id": did,
        "title": "Sales Overview",
        "source_table": "public.orders",
        "filters": [],
        "widgets": [
            {
                "id": "kpi-revenue",
                "type": "kpi",
                "title": "Revenue",
                "sql": "SELECT SUM(amount) AS value FROM public.orders",
            },
            {
                "id": "chart-revenue-region",
                "type": "chart",
                "title": "Revenue by Region",
                "chart": "bar",
                "sql": (
                    "SELECT region AS category, SUM(amount) AS value "
                    "FROM public.orders GROUP BY region"
                ),
            },
            {
                "id": "chart-revenue-trend",
                "type": "chart",
                "title": "Revenue Trend",
                "chart": "bar",
                "sql": (
                    "SELECT order_date AS category, SUM(amount) AS value "
                    "FROM public.orders GROUP BY order_date"
                ),
            },
            {
                "id": "insight-1",
                "type": "ai_insight",
                "title": "AI Insights",
                "sql": None,
            },
        ],
    }


def _catalog(dashboard: dict[str, Any]) -> list:
    return build_widget_catalog(dashboard, SEMANTIC)


def test_widget_resolver_picks_metric_match() -> None:
    catalog = _catalog(_sample_dashboard())
    resolution = resolve_widget_target(
        WidgetRef(metric="Revenue", widget_type="kpi"),
        catalog,
    )
    assert resolution.status == "resolved"
    assert resolution.widget_ids == ["kpi-revenue"]


def test_active_widget_id_does_not_override_explicit_id() -> None:
    catalog = _catalog(_sample_dashboard())
    resolution = resolve_widget_target(
        WidgetRef(widget_id="kpi-revenue"),
        catalog,
        active_widget_id="chart-revenue-trend",
    )
    assert resolution.status == "resolved"
    assert resolution.widget_ids == ["kpi-revenue"]


def test_widget_resolver_ambiguous_revenue_charts() -> None:
    catalog = _catalog(_sample_dashboard())
    resolution = resolve_widget_target(
        WidgetRef(title="revenue"),
        catalog,
    )
    assert resolution.status == "ambiguous"
    assert len(resolution.candidates) >= 2


def test_remove_ai_insights_bulk_type() -> None:
    catalog = _catalog(_sample_dashboard())
    resolution = resolve_widget_target(
        WidgetRef(widget_type="ai_insight"),
        catalog,
        allow_bulk_type=True,
    )
    assert resolution.status == "resolved"
    assert resolution.widget_ids == ["insight-1"]


def test_add_widget_profit_margin_kpi() -> None:
    dashboard = _sample_dashboard()
    dashboard_id = UUID(str(dashboard["id"]))
    catalog = _catalog(dashboard)
    plan = DashboardOperationPlan(
        dashboard_id=dashboard_id,
        operations=[
            DashboardOperationItem(
                action="add_widget",
                widget_spec=WidgetAddSpec(
                    metric="Profit Margin",
                    widget_type="kpi",
                ),
            )
        ],
    )
    validation = validate_operation_plan(
        plan,
        loaded_dashboard_id=dashboard_id,
        catalog=catalog,
        semantic_catalog=SEMANTIC,
    )
    assert validation.ok
    updated, notes, modified = apply_resolved_operations(
        dashboard,
        validation.resolved,
        semantic_catalog=SEMANTIC,
        schema=ORDERS_SCHEMA,
        db_type="postgres",
    )
    assert any(w.get("type") == "kpi" and "Profit" in w.get("title", "") for w in updated["widgets"])
    assert notes
    assert modified


def test_add_widget_ai_insight_content_after_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.dashboard import execute_dashboard_edit

    dashboard = _sample_dashboard()
    dashboard["widgets"] = [
        w for w in dashboard["widgets"] if w.get("type") != "ai_insight"
    ]
    dashboard_id = UUID(str(dashboard["id"]))
    catalog = _catalog(dashboard)
    plan = DashboardOperationPlan(
        dashboard_id=dashboard_id,
        operations=[
            DashboardOperationItem(
                action="add_widget",
                widget_spec=WidgetAddSpec(
                    widget_type="ai_insight",
                    title="AI Insights",
                ),
            )
        ],
    )
    validation = validate_operation_plan(
        plan,
        loaded_dashboard_id=dashboard_id,
        catalog=catalog,
        semantic_catalog=SEMANTIC,
    )
    assert validation.ok
    updated, notes, modified = apply_resolved_operations(
        dashboard,
        validation.resolved,
        semantic_catalog=SEMANTIC,
        schema=ORDERS_SCHEMA,
        db_type="postgres",
    )
    assert any(w.get("type") == "ai_insight" for w in updated["widgets"])
    assert notes

    monkeypatch.setattr(
        "app.agent.dashboard._validate_and_run",
        lambda *a, **k: (
            [{"category": "West", "value": 600}, {"category": "East", "value": 400}],
            "SELECT 1",
        ),
    )
    monkeypatch.setattr("app.llm.client.get_llm", lambda: None)

    executed = execute_dashboard_edit(
        updated,
        connection_id=str(uuid4()),
        schema=ORDERS_SCHEMA,
        db_type="postgres",
        tenant_id=uuid4(),
        semantic=SEMANTIC,
        modified_widget_ids=set(modified),
    )
    insight = next(w for w in executed["widgets"] if w.get("type") == "ai_insight")
    assert insight.get("content")
    assert "- " in insight["content"]


def test_change_chart_type_to_line() -> None:
    dashboard = _sample_dashboard()
    dashboard_id = UUID(str(dashboard["id"]))
    catalog = _catalog(dashboard)
    plan = DashboardOperationPlan(
        dashboard_id=dashboard_id,
        operations=[
            DashboardOperationItem(
                action="update_widget",
                target=WidgetRef(widget_id="chart-revenue-region"),
                widget_patch=WidgetPatch(chart="line"),
            )
        ],
    )
    validation = validate_operation_plan(
        plan,
        loaded_dashboard_id=dashboard_id,
        catalog=catalog,
        semantic_catalog=SEMANTIC,
    )
    assert validation.ok
    updated, _, modified = apply_resolved_operations(
        dashboard,
        validation.resolved,
        semantic_catalog=SEMANTIC,
        schema=ORDERS_SCHEMA,
        db_type="postgres",
    )
    target = next(w for w in updated["widgets"] if w["id"] in modified)
    assert target["chart"] == "line"


def test_move_widget_to_top() -> None:
    dashboard = _sample_dashboard()
    dashboard_id = UUID(str(dashboard["id"]))
    catalog = _catalog(dashboard)
    plan = DashboardOperationPlan(
        dashboard_id=dashboard_id,
        operations=[
            DashboardOperationItem(
                action="move_widget",
                target=WidgetRef(widget_id="kpi-revenue"),
                position_preset="top",
            )
        ],
    )
    validation = validate_operation_plan(
        plan,
        loaded_dashboard_id=dashboard_id,
        catalog=catalog,
        semantic_catalog=SEMANTIC,
    )
    assert validation.ok
    updated, notes, _ = apply_resolved_operations(
        dashboard,
        validation.resolved,
        semantic_catalog=SEMANTIC,
        schema=ORDERS_SCHEMA,
        db_type="postgres",
    )
    kpi = next(w for w in updated["widgets"] if w["id"] == "kpi-revenue")
    assert kpi["position"]["y"] == 0
    assert notes


def test_add_region_filter_requires_value() -> None:
    dashboard = _sample_dashboard()
    dashboard_id = UUID(str(dashboard["id"]))
    catalog = _catalog(dashboard)
    plan = DashboardOperationPlan(
        dashboard_id=dashboard_id,
        operations=[
            DashboardOperationItem(
                action="add_filter",
                filter=FilterSpec(field="Region", operator="eq", value=None),
            )
        ],
    )
    validation = validate_operation_plan(
        plan,
        loaded_dashboard_id=dashboard_id,
        catalog=catalog,
        semantic_catalog=SEMANTIC,
    )
    assert not validation.ok
    assert validation.clarification is not None
    assert validation.clarification.kind == "pick_filter_value"


def test_add_region_filter_with_value() -> None:
    dashboard = _sample_dashboard()
    dashboard_id = UUID(str(dashboard["id"]))
    catalog = _catalog(dashboard)
    plan = DashboardOperationPlan(
        dashboard_id=dashboard_id,
        operations=[
            DashboardOperationItem(
                action="add_filter",
                filter=FilterSpec(field="Region", operator="eq", value="West"),
            )
        ],
    )
    validation = validate_operation_plan(
        plan,
        loaded_dashboard_id=dashboard_id,
        catalog=catalog,
        semantic_catalog=SEMANTIC,
    )
    assert validation.ok
    updated, notes, _ = apply_resolved_operations(
        dashboard,
        validation.resolved,
        semantic_catalog=SEMANTIC,
        schema=ORDERS_SCHEMA,
        db_type="postgres",
    )
    assert updated["filters"] == [{"field": "Region", "operator": "eq", "value": "West"}]
    assert notes


def test_invalid_metric_rejected() -> None:
    dashboard = _sample_dashboard()
    dashboard_id = UUID(str(dashboard["id"]))
    catalog = _catalog(dashboard)
    plan = DashboardOperationPlan(
        dashboard_id=dashboard_id,
        operations=[
            DashboardOperationItem(
                action="add_widget",
                widget_spec=WidgetAddSpec(metric="NotAMetric", widget_type="kpi"),
            )
        ],
    )
    validation = validate_operation_plan(
        plan,
        loaded_dashboard_id=dashboard_id,
        catalog=catalog,
        semantic_catalog=SEMANTIC,
    )
    assert not validation.ok
    assert validation.errors


def test_ambiguous_remove_revenue_clarifies() -> None:
    dashboard = _sample_dashboard()
    dashboard_id = UUID(str(dashboard["id"]))
    catalog = _catalog(dashboard)
    plan = DashboardOperationPlan(
        dashboard_id=dashboard_id,
        operations=[
            DashboardOperationItem(
                action="remove_widget",
                target=WidgetRef(title="revenue"),
            )
        ],
    )
    validation = validate_operation_plan(
        plan,
        loaded_dashboard_id=dashboard_id,
        catalog=catalog,
        semantic_catalog=SEMANTIC,
    )
    assert not validation.ok
    assert validation.clarification is not None
    assert validation.clarification.kind == "pick_widget"


def test_follow_up_picks_widget_by_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.agent.dashboard._validate_and_run",
        lambda *a, **k: ([{"value": 1}], "SELECT 1 AS value"),
    )
    dashboard = _sample_dashboard()
    dashboard_id = UUID(str(dashboard["id"]))
    catalog = _catalog(dashboard)
    choices = [
        ClarificationChoice(id="1", label="Revenue by Region", widget_id="chart-revenue-region"),
        ClarificationChoice(id="2", label="Revenue Trend", widget_id="chart-revenue-trend"),
    ]
    pending = PendingDashboardOp(
        plan=DashboardOperationPlan(
            dashboard_id=dashboard_id,
            operations=[
                DashboardOperationItem(
                    action="remove_widget",
                    target=WidgetRef(title="revenue"),
                )
            ],
        ),
        unresolved_index=0,
        choices=choices,
    )
    result = run_dashboard_edit_pipeline(
        instruction="2",
        dashboard=dashboard,
        dashboard_id=dashboard_id,
        semantic_catalog=SEMANTIC,
        schema=ORDERS_SCHEMA,
        db_type="postgres",
        connection_id=str(uuid4()),
        tenant_id=uuid4(),
        llm=MagicMock(),
        pending_operation=pending,
    )
    assert result.success
    ids = {w["id"] for w in result.dashboard["widgets"]}
    assert "chart-revenue-trend" not in ids
    assert "chart-revenue-region" in ids


def test_edit_never_creates_second_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    build_calls = {"count": 0}

    class FakeBuilder:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def run(self, state: dict[str, Any]) -> dict[str, Any]:
            build_calls["count"] += 1
            return {"dashboard": {"widgets": []}, "rows": []}

    monkeypatch.setattr(workflow_mod, "DashboardBuilder", FakeBuilder)
    editor = workflow_mod.DashboardEditor()
    out = editor.run(
        {
            "tenant_id": str(uuid4()),
            "user_id": str(uuid4()),
            "connection_id": str(uuid4()),
            "question": "remove kpi",
            "analysis_state": AnalysisState().model_dump(),
        }
    )
    assert build_calls["count"] == 0
    assert out.get("dashboard_clarification") or out.get("error")


def test_dashboard_update_event_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    connection_id = uuid4()
    dashboard = _sample_dashboard(dashboard_id)
    dashboard["connection_id"] = str(connection_id)

    emitted: list[Any] = []

    def capture_emit(state: dict[str, Any], event: Any) -> None:
        emitted.append(event)

    monkeypatch.setattr(workflow_mod, "_emit", capture_emit)
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda *a, **k: ORDERS_SCHEMA,
    )
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_semantic",
        lambda *a, **k: SEMANTIC,
    )
    monkeypatch.setattr(
        workflow_mod,
        "get_dashboard",
        lambda db, did, user: dashboard,
    )
    monkeypatch.setattr(
        workflow_mod,
        "execute_dashboard",
        lambda spec, **kwargs: dict(spec),
    )
    monkeypatch.setattr(
        workflow_mod,
        "run_dashboard_edit_pipeline",
        lambda **kwargs: DashboardEditResult(
            success=True,
            dashboard=dashboard,
            edit_notes=["Renamed dashboard."],
            modified_widget_ids=["kpi-revenue"],
        ),
    )
    monkeypatch.setattr(
        workflow_mod,
        "update_dashboard_from_spec",
        lambda db, dashboard_id, user, spec, save_version=True: spec,
    )
    monkeypatch.setattr(
        workflow_mod,
        "SessionLocal",
        lambda: type(
            "S",
            (),
            {
                "get": lambda self, model, pk: User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email="t@test.com",
                    role="analyst",
                    password_hash="x",
                    is_verified=True,
                )
                if model is User
                else None,
                "scalar": lambda self, stmt: type(
                    "C",
                    (),
                    {"id": connection_id, "tenant_id": tenant_id},
                )(),
                "close": lambda self: None,
            },
        )(),
    )

    editor = workflow_mod.DashboardEditor()
    editor.run(
        {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "connection_id": str(connection_id),
            "question": "rename dashboard to Q1 Sales",
            "run_id": "run-1",
            "analysis_state": AnalysisState(
                dashboard=DashboardFocus(
                    dashboard_id=dashboard_id,
                )
            ).model_dump(),
        }
    )

    assert any(isinstance(e, DashboardUpdatedEvent) for e in emitted)
    evt = next(e for e in emitted if isinstance(e, DashboardUpdatedEvent))
    assert evt.type == ProgressEventType.DASHBOARD_UPDATED
    assert evt.dashboard_id == str(dashboard_id)
    assert evt.edit_notes


def test_analysis_state_updated_after_edit() -> None:
    dashboard_id = uuid4()
    dashboard = _sample_dashboard(dashboard_id)
    dashboard["filters"] = [{"field": "Region", "operator": "eq", "value": "West"}]
    catalog = build_widget_catalog(dashboard, SEMANTIC)
    prior = AnalysisState()
    final = {
        "dashboard": dashboard,
        "dashboard_edit_applied": True,
        "dashboard_widget_catalog": [c.model_dump(mode="json") for c in catalog],
        "dashboard_edit_notes": ["Added filter on Region."],
        "dashboard_modified_widget_ids": ["kpi-revenue"],
        "answer": "Updated dashboard.",
    }
    state = merge_workflow_into_state(
        prior,
        question="add region filter West",
        final=final,
        connection_id=uuid4(),
    )
    assert state.dashboard.dashboard_id == dashboard_id
    assert state.dashboard.filters
    assert state.dashboard.widget_catalog
    assert state.dashboard.last_edit_notes
    assert state.dashboard.active_widget_id == "kpi-revenue"
    assert state.dashboard.pending_operation is None


def test_cross_tenant_dashboard_edit_denied(client) -> None:
    from sqlalchemy import select

    from app.database import get_db

    test_client, sent = client
    test_client.post(
        "/api/v1/auth/signup",
        json={
            "tenant_name": "TenantA",
            "email": "a@test.com",
            "password": "Password@123",
        },
    )
    verify_a = test_client.post(
        "/api/v1/auth/verify-otp",
        json={"email": "a@test.com", "otp_code": sent["otp"]},
    )
    token_a = verify_a.json()["access_token"]

    override = test_client.app.dependency_overrides.get(get_db)
    gen = override()
    db = next(gen)
    try:
        user_a = db.scalar(select(User).where(User.email == "a@test.com"))
        assert user_a is not None
        tenant_b = Tenant(name="TenantB")
        db.add(tenant_b)
        db.flush()
        user_b = User(
            tenant_id=tenant_b.id,
            email="b@test.com",
            role="analyst",
            password_hash="x",
            is_verified=True,
        )
        db.add(user_b)
        db.flush()
        spec = _sample_dashboard()
        persisted = create_dashboard_from_spec(
            db,
            user_a,
            uuid4(),
            spec,
        )
        dash_id = UUID(str(persisted["id"]))
        with pytest.raises(ValueError, match="Dashboard not found"):
            get_dashboard(db, dash_id, user_b)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


@pytest.mark.parametrize(
    "unsafe_sql",
    ["DROP TABLE orders;--", "DELETE FROM orders"],
)
def test_sql_safety_on_generated_widget(unsafe_sql: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.dashboard import DashboardEditExecutionError, execute_dashboard_edit

    def raise_on_unsafe(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], str]:
        sql = kwargs.get("sql") or args[3] if len(args) > 3 else ""
        if "DROP" in str(sql).upper() or "DELETE" in str(sql).upper():
            raise ValueError("Unsafe SQL blocked")
        return ([{"value": 1}], "SELECT 1 AS value")

    monkeypatch.setattr("app.agent.dashboard._validate_and_run", raise_on_unsafe)

    spec = _sample_dashboard()
    spec["widgets"].append(
        {
            "id": "kpi-evil",
            "type": "kpi",
            "title": "Evil",
            "sql": unsafe_sql,
        }
    )
    with pytest.raises(DashboardEditExecutionError):
        execute_dashboard_edit(
            spec,
            connection_id=str(uuid4()),
            schema=ORDERS_SCHEMA,
            db_type="postgres",
            tenant_id=uuid4(),
            semantic=SEMANTIC,
            modified_widget_ids={"kpi-evil"},
        )


def test_dashboard_operations_e2e_same_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-turn edits on the same dashboard_id with AnalysisState sync."""
    dashboard_id = uuid4()
    dashboard = _sample_dashboard(dashboard_id)
    tenant_id = uuid4()
    connection_id = uuid4()

    plans = [
        DashboardOperationPlan(
            dashboard_id=dashboard_id,
            operations=[
                DashboardOperationItem(
                    action="add_widget",
                    widget_spec=WidgetAddSpec(
                        metric="Profit Margin",
                        widget_type="kpi",
                    ),
                )
            ],
        ),
        DashboardOperationPlan(
            dashboard_id=dashboard_id,
            operations=[
                DashboardOperationItem(
                    action="update_widget",
                    target=WidgetRef(widget_id="chart-revenue-region"),
                    widget_patch=WidgetPatch(chart="line"),
                )
            ],
        ),
        DashboardOperationPlan(
            dashboard_id=dashboard_id,
            operations=[
                DashboardOperationItem(
                    action="remove_widget",
                    target=WidgetRef(widget_id="insight-1"),
                )
            ],
        ),
        DashboardOperationPlan(
            dashboard_id=dashboard_id,
            operations=[
                DashboardOperationItem(
                    action="add_filter",
                    filter=FilterSpec(field="Region", operator="eq", value="East"),
                )
            ],
        ),
    ]
    plan_iter = iter(plans)

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            plan = next(plan_iter)
            return json.dumps(plan.model_dump(mode="json"))

    monkeypatch.setattr(
        "app.agent.dashboard._validate_and_run",
        lambda *a, **k: ([{"value": 100}], "SELECT 1 AS value"),
    )

    analysis = AnalysisState(
        dashboard=DashboardFocus(dashboard_id=dashboard_id),
    )
    working = dashboard

    for question in [
        "add profit margin kpi",
        "change revenue by region to line chart",
        "remove ai insights",
        "filter region to East",
    ]:
        result = run_dashboard_edit_pipeline(
            instruction=question,
            dashboard=working,
            dashboard_id=dashboard_id,
            semantic_catalog=SEMANTIC,
            schema=ORDERS_SCHEMA,
            db_type="postgres",
            connection_id=str(connection_id),
            tenant_id=tenant_id,
            llm=FakeLLM(),
            active_widget_id=analysis.dashboard.active_widget_id,
        )
        assert result.success, result.error
        assert result.dashboard is not None
        assert str(result.dashboard.get("id") or dashboard_id) == str(dashboard_id)
        working = result.dashboard

        catalog = build_widget_catalog(working, SEMANTIC)
        final = {
            "dashboard": {**working, "id": str(dashboard_id)},
            "dashboard_edit_applied": True,
            "dashboard_widget_catalog": [c.model_dump(mode="json") for c in catalog],
            "dashboard_edit_notes": result.edit_notes,
            "dashboard_modified_widget_ids": result.modified_widget_ids,
            "answer": "ok",
        }
        analysis = merge_workflow_into_state(
            analysis,
            question=question,
            final=final,
            connection_id=connection_id,
        )

    assert analysis.dashboard.dashboard_id == dashboard_id
    assert analysis.dashboard.filters == [
        {"field": "Region", "operator": "eq", "value": "East"}
    ]
    assert analysis.dashboard.widget_catalog
    assert not any(w["id"] == "insight-1" for w in working["widgets"])
    chart = next(w for w in working["widgets"] if w["id"] == "chart-revenue-region")
    assert chart["chart"] == "line"
    assert any("Profit" in w.get("title", "") for w in working["widgets"])
