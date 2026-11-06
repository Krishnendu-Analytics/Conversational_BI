"""P0-2: Complete AnalysisState persistence through BIWorkflow.run."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

import app.agent.workflow as workflow_mod
from app.agent.analysis_state import (
    AnalysisState,
    DashboardFocus,
    analysis_state_to_json,
    merge_workflow_into_state,
)
from app.agent.dashboard_operation_schema import (
    ClarificationChoice,
    DashboardOperationItem,
    DashboardOperationPlan,
    PendingDashboardOp,
    WidgetRef,
)
from app.agent.query_plan import QueryPlan
from app.database import get_db
from app.main import app
from app.models import Conversation, User
from app.services.conversation_state_service import ConversationStateService
from app.redis_client import ConversationMemory
from tests.test_conversation_memory import FakeRedis
from tests.test_p0_dashboard_connection_guard import (
    _fake_session,
    _sample_dashboard,
)
from tests.test_tenant_isolation import (
    _create_connection,
    _create_conversation,
    _signup_and_token,
)


def test_biworkflow_result_includes_dashboard_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    pending = {"plan": {"dashboard_id": str(uuid4()), "operations": []}}
    catalog = [{"id": "kpi-revenue", "label": "Revenue"}]

    class FakeGraph:
        def invoke(self, initial: dict[str, Any]) -> dict[str, Any]:
            return {
                **initial,
                "answer": "Updated.",
                "intent": "edit_dashboard",
                "dashboard_pending_operation": pending,
                "dashboard_widget_catalog": catalog,
                "dashboard_edit_applied": False,
                "dashboard_modified_widget_ids": ["kpi-revenue"],
                "dashboard_edit_notes": ["Pick a widget."],
                "dashboard_clarification": "Which revenue widget?",
                "query_plan": QueryPlan(intent="aggregation", metrics=["Revenue"]),
            }

    monkeypatch.setattr(workflow_mod, "_persist_analysis_state", lambda **kwargs: None)

    wf = workflow_mod.BIWorkflow()
    wf._graph = FakeGraph()  # type: ignore[assignment]
    result = wf.run("add chart", tenant_id=uuid4(), user_id=uuid4())

    assert result["dashboard_pending_operation"] == pending
    assert result["dashboard_widget_catalog"] == catalog
    assert result["dashboard_edit_notes"] == ["Pick a widget."]
    assert result["dashboard_clarification"] == "Which revenue widget?"
    assert result["query_plan"]["intent"] == "aggregation"


def test_merge_persists_dashboard_clarification_state() -> None:
    dashboard_id = uuid4()
    dashboard = _sample_dashboard(dashboard_id)
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
        choices=[
            ClarificationChoice(id="1", label="Revenue by Region", widget_id="chart-revenue-region"),
        ],
    ).model_dump(mode="json")
    catalog = [{"id": "chart-revenue-region", "label": "Revenue by Region"}]

    prior = AnalysisState()
    state = merge_workflow_into_state(
        prior,
        question="remove revenue chart",
        final={
            "answer": "Which one?",
            "dashboard": dashboard,
            "dashboard_pending_operation": pending,
            "dashboard_widget_catalog": catalog,
            "dashboard_edit_notes": ["Which revenue widget?"],
        },
        connection_id=uuid4(),
    )

    assert state.dashboard.pending_operation == pending
    assert state.dashboard.widget_catalog == catalog
    assert state.dashboard.last_edit_notes == ["Which revenue widget?"]
    assert state.dashboard.dashboard_id == dashboard_id


def test_merge_persists_investigation_evidence() -> None:
    """Investigator's evidence must survive into durable AnalysisState, not
    just the narrated answer — otherwise a follow-up turn (or anyone
    inspecting the conversation later) has no record of what was checked."""
    from app.agent.analysis_state import format_analysis_state_context

    prior = AnalysisState()
    evidence = [
        {"domain": "Sales", "error": None, "facts": {"headline": "ok"}},
        {"domain": "Procurement", "error": "No discovered schema", "facts": None},
    ]
    state = merge_workflow_into_state(
        prior,
        question="why did gross profit decline",
        final={
            "answer": "## Investigation ...",
            "run_id": "r1",
            "investigation_evidence": evidence,
            "investigation_domains_checked": ["Procurement", "Sales"],
        },
    )

    assert state.investigation.run_id == "r1"
    assert state.investigation.domains_checked == ["Procurement", "Sales"]
    assert state.investigation.evidence_summary == [
        {"domain": "Sales", "ok": True, "error": None},
        {"domain": "Procurement", "ok": False, "error": "No discovered schema"},
    ]

    context = format_analysis_state_context(state)
    assert "checked Procurement, Sales" in context
    assert "no evidence from Procurement" in context


def test_merge_without_investigation_evidence_leaves_it_untouched() -> None:
    """A normal (non-investigation) turn must not clobber an earlier
    investigation's trace still worth remembering for this conversation."""
    prior = AnalysisState()
    prior.investigation.domains_checked = ["Sales"]

    state = merge_workflow_into_state(
        prior,
        question="show me top 10 customers",
        final={"answer": "Here you go.", "sql": "SELECT 1"},
    )

    assert state.investigation.domains_checked == ["Sales"]


def test_second_turn_loads_pending_operation_from_pg(client, monkeypatch) -> None:
    test_client, sent = client
    monkeypatch.setattr("app.redis_client.get_redis", lambda: FakeRedis())

    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, "persist-user@test.com"
    )
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    dashboard_id = uuid4()
    pending = {"plan": {"dashboard_id": str(dashboard_id), "operations": []}}

    override = app.dependency_overrides.get(get_db)
    gen = override()
    db: Session = next(gen)
    try:
        conv = db.get(Conversation, conv_id)
        user = db.get(User, user_id)
        assert conv is not None and user is not None

        state = AnalysisState(
            turn_count=1,
            dashboard=DashboardFocus(),
        )
        state.dashboard.pending_operation = pending
        state.dashboard.widget_catalog = [{"id": "w1", "label": "Widget 1"}]
        conv.analysis_state_json = analysis_state_to_json(state)
        db.commit()

        loaded = ConversationStateService().load(db, conversation_id=conv_id, user=user)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    assert loaded.dashboard.pending_operation == pending
    assert loaded.dashboard.widget_catalog == [{"id": "w1", "label": "Widget 1"}]


def test_stale_redis_does_not_override_pg_pending_operation(client, monkeypatch) -> None:
    """PG has pending_operation; Redis blob lacks it — PG must win after P0-3 fix."""
    test_client, sent = client
    fake_redis = FakeRedis()
    monkeypatch.setattr("app.redis_client.get_redis", lambda: fake_redis)

    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, "stale-redis@test.com"
    )
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    pending_pg = {"plan": {"dashboard_id": str(uuid4()), "operations": []}}

    override = app.dependency_overrides.get(get_db)
    gen = override()
    db: Session = next(gen)
    try:
        conv = db.get(Conversation, conv_id)
        user = db.get(User, user_id)
        assert conv is not None and user is not None

        pg_state = AnalysisState(
            turn_count=2,
            last_sql="SELECT 1",
        )
        pg_state.dashboard.pending_operation = pending_pg
        conv.analysis_state_json = analysis_state_to_json(pg_state)
        db.commit()

        stale_state = AnalysisState(turn_count=2, last_sql="SELECT 1")
        stale_state.dashboard.pending_operation = None
        from app.agent.analysis_state import analysis_state_to_memory_blob

        memory = ConversationMemory()
        memory.set_state(tenant_id, conv_id, analysis_state_to_memory_blob(stale_state))

        loaded = ConversationStateService().load(db, conversation_id=conv_id, user=user)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    assert loaded.dashboard.pending_operation == pending_pg


def test_last_sql_not_executed_without_safety_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persisted last_sql in state must not bypass SQLSafetyPipeline."""
    executed_sql: list[str] = []

    def spy_run_readonly_query(connection_id: str, sql: str, *, tenant_id=None, **kwargs):  # noqa: ANN001
        executed_sql.append(sql)
        return [{"region": "West"}]

    real_validate = workflow_mod.SQLSafetyPipeline.validate

    def tracking_validate(self, sql: str) -> str:  # noqa: ANN001
        validated = real_validate(self, sql)
        executed_sql.append(f"validated:{validated}")
        return validated

    monkeypatch.setattr(workflow_mod, "run_readonly_query", spy_run_readonly_query)
    monkeypatch.setattr(workflow_mod.SQLSafetyPipeline, "validate", tracking_validate)
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda *a, **k: {
            "db_type": "postgres",
            "tables": [
                {
                    "table_name": "sales",
                    "schema_name": "public",
                    "columns": [
                        {"name": "region", "data_type": "varchar"},
                    ],
                }
            ],
        },
    )
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_semantic",
        lambda *a, **k: {"metrics": [], "dimensions": []},
    )

    prior_sql = "DROP TABLE sales"
    state = {
        "tenant_id": str(uuid4()),
        "connection_id": str(uuid4()),
        "sql": "SELECT region FROM public.sales",
        "analysis_state": AnalysisState(
            last_sql=prior_sql,
            turn_count=3,
        ).model_dump(mode="json"),
    }

    workflow_mod.QueryExecutor().run(state)

    assert prior_sql not in executed_sql
    assert any("validated:SELECT" in s for s in executed_sql)


def test_dashboard_create_persists_dashboard_id_via_save(client, monkeypatch) -> None:
    test_client, sent = client
    monkeypatch.setattr("app.redis_client.get_redis", lambda: FakeRedis())

    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, "dash-create@test.com"
    )
    conn_id = _create_connection(test_client, headers, monkeypatch)
    conv_id = _create_conversation(test_client, headers, connection_id=conn_id)

    dashboard_id = uuid4()
    dashboard = _sample_dashboard(dashboard_id, connection_id=conn_id)

    override = app.dependency_overrides.get(get_db)
    gen = override()
    db: Session = next(gen)
    try:
        user = db.get(User, user_id)
        assert user is not None
        svc = ConversationStateService()
        svc.save(
            db,
            conversation_id=conv_id,
            user=user,
            question="create dashboard",
            final={
                "answer": "Created dashboard.",
                "intent": "create_dashboard",
                "dashboard": dashboard,
            },
            connection_id=conn_id,
        )
        loaded = svc.load(db, conversation_id=conv_id, user=user)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass

    assert loaded.dashboard.dashboard_id == dashboard_id
    assert loaded.turn_count == 1
