"""P0-E: Real BIWorkflow → DashboardEditor connection guard integration."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

import app.agent.workflow as workflow_mod
from app.agent.analysis_state import AnalysisState, DashboardFocus
from app.authorization import DashboardConnectionMismatchError
from tests.test_p0_dashboard_connection_guard import (
    ORDERS_SCHEMA,
    _editor_state,
    _fake_session,
    _sample_dashboard,
)


def test_biworkflow_dashboard_mismatch_rejects_before_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conversation DB-A, dashboard DB-B — real workflow chain, zero customer SQL."""
    tenant_id = uuid4()
    user_id = uuid4()
    conn_a = uuid4()
    conn_b = uuid4()
    dashboard_id = uuid4()
    dashboard_spec = _sample_dashboard(dashboard_id, connection_id=conn_b)

    queried: list[str] = []

    def spy_run_readonly_query(connection_id: str, sql: str, *, tenant_id=None, **kwargs):  # noqa: ANN001
        queried.append(str(connection_id))
        return [], sql

    monkeypatch.setattr(
        "app.tools.customer_db.run_readonly_query",
        spy_run_readonly_query,
    )
    monkeypatch.setattr(
        workflow_mod.IntentClassifier,
        "run",
        lambda self, state: {**state, "intent": "edit_dashboard"},
    )
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda *a, **k: ORDERS_SCHEMA,
    )
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_semantic",
        lambda *a, **k: {"metrics": [], "dimensions": []},
    )
    monkeypatch.setattr(
        workflow_mod,
        "get_dashboard",
        lambda db, did, user: dashboard_spec if did == dashboard_id else None,
    )
    monkeypatch.setattr(
        workflow_mod,
        "SessionLocal",
        lambda: _fake_session(
            user_id=user_id,
            tenant_id=tenant_id,
            connection_id=conn_b,
        ),
    )
    monkeypatch.setattr(workflow_mod, "_persist_analysis_state", lambda **kwargs: None)

    analysis = AnalysisState(
        dashboard=DashboardFocus(dashboard_id=dashboard_id),
    ).model_dump(mode="json")

    class FakeGraph:
        def invoke(self, initial: dict[str, Any]) -> dict[str, Any]:
            editor = workflow_mod.DashboardEditor()
            merged = {**initial, "analysis_state": analysis}
            return editor.run(merged)

    wf = workflow_mod.BIWorkflow()
    wf._graph = FakeGraph()  # type: ignore[assignment]

    result = wf.run(
        "Add a revenue chart",
        tenant_id=tenant_id,
        user_id=user_id,
        connection_id=str(conn_a),
    )

    assert result.get("error") is not None
    assert result.get("error_code") == DashboardConnectionMismatchError.code
    assert queried == []
    assert str(conn_a) not in queried
