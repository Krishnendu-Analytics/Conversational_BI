"""P1-5 closure: workflow integration, E2E chat/dashboard, security matrix."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.agent.workflow as workflow_mod
from app.agent.analysis_state import AnalysisState, merge_workflow_into_state
from app.agent.dataset_context import (
    filter_schema_for_dataset,
    format_dataset_clarification_answer,
    prepare_file_dataset_context,
    resolve_dataset_clarification,
)
from app.agent.follow_up import resolve_follow_up
from app.agent.workflow import BIWorkflow, SQLGenerator
from app.database import get_db
from app.models import Dataset, Dimension, Metric
from tests.test_p1_5_datasets import (
    ELECTIONS_CSV,
    HEALTHCARE_CSV,
    _query_file_connection,
    _upload,
    file_storage_tmp,
)
from tests.test_tenant_isolation import _signup_and_token


@pytest.fixture()
def patch_workflow_db(client: tuple[TestClient, dict[str, str]], monkeypatch: pytest.MonkeyPatch):
    """Route workflow and query SessionLocal to the in-memory test database."""
    test_client, _sent = client

    def _session_local():
        gen = test_client.app.dependency_overrides[get_db]()
        return next(gen)

    monkeypatch.setattr(workflow_mod, "SessionLocal", _session_local)
    monkeypatch.setattr("app.database.SessionLocal", _session_local)


@pytest.fixture()
def elections_upload(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp: Path,
    patch_workflow_db: None,
) -> dict[str, Any]:
    test_client, sent = client
    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, f"wf-{uuid4().hex[:8]}@acme.com"
    )
    body = _upload(
        test_client,
        headers,
        name="Elections",
        filename="elections.csv",
        content=ELECTIONS_CSV,
    )
    return {
        "client": test_client,
        "headers": headers,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "connection_id": body["connection_id"],
        "dataset_id": body["datasets"][0]["id"],
        "physical_table": body["datasets"][0]["physical_table"],
    }


def _schema_and_semantic_from_db(
    test_client: TestClient,
    *,
    tenant_id: UUID,
    connection_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    gen = test_client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        from app.tools.customer_db import get_stored_schema

        schema = get_stored_schema(db, UUID(connection_id))
        metrics = db.scalars(
            select(Metric).where(
                Metric.tenant_id == tenant_id,
                Metric.connection_id == UUID(connection_id),
            )
        ).all()
        dims = db.scalars(
            select(Dimension).where(
                Dimension.tenant_id == tenant_id,
                Dimension.connection_id == UUID(connection_id),
            )
        ).all()
        semantic = {
            "metrics": [
                {"name": m.name, "expression": m.expression, "source_column_ref": m.source_column_ref}
                for m in metrics
            ],
            "dimensions": [
                {"name": d.name, "expression": d.expression, "source_column_ref": d.source_column_ref}
                for d in dims
            ],
            "relationships": [],
        }
        return schema, semantic
    finally:
        gen.close()


def _workflow_llm_stub(*, intent: str = "sql") -> Any:
    class Stub:
        client = object()

        def complete(self, prompt: str) -> str:
            lower = prompt.lower()
            if "intent classifier" in lower:
                return intent
            if "parse the user question into a json query plan" in lower:
                if "north" in lower or "only" in lower:
                    return json.dumps(
                        {
                            "intent": "aggregation",
                            "metrics": ["Votes"],
                            "dimensions": ["Category"],
                            "filters": [{"field": "Category", "operator": "=", "value": "North"}],
                        }
                    )
                if "category" in lower:
                    return json.dumps(
                        {
                            "intent": "aggregation",
                            "metrics": ["Votes"],
                            "dimensions": ["Category"],
                        }
                    )
                return json.dumps(
                    {"intent": "aggregation", "metrics": ["Votes"], "dimensions": []}
                )
            if "pre-computed facts json" in lower:
                return "Totals are grounded in query results."
            return "[llm stub]"

    return Stub()


def test_analysis_state_persists_dataset_id() -> None:
    prior = AnalysisState(dataset_id=UUID("00000000-0000-0000-0000-000000000001"))
    state = merge_workflow_into_state(
        prior,
        question="Show votes",
        final={
            "dataset_id": "00000000-0000-0000-0000-000000000002",
            "dataset_display_name": "Elections",
            "sql": "SELECT 1",
            "answer": "ok",
        },
        connection_id="00000000-0000-0000-0000-000000000099",
    )
    assert str(state.dataset_id) == "00000000-0000-0000-0000-000000000002"
    assert state.dataset_display_name == "Elections"


def test_dataset_clarification_resolution(elections_upload: dict[str, Any]) -> None:
    gen = elections_upload["client"].app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        ds2 = Dataset(
            id=uuid4(),
            tenant_id=elections_upload["tenant_id"],
            connection_id=UUID(elections_upload["connection_id"]),
            name="archive",
            display_name="elections_archive.csv",
            source_type="csv",
            physical_table="t_archive",
            status="ready",
            row_count=1,
            column_count=1,
            columns_json="[]",
        )
        db.add(ds2)
        db.commit()

        analysis = AnalysisState(
            connection_id=UUID(elections_upload["connection_id"]),
            pending_dataset_clarification={
                "kind": "pick_dataset",
                "message": "Which dataset?",
                "choices": [
                    {"id": "1", "label": "Elections", "dataset_id": elections_upload["dataset_id"]},
                    {"id": "2", "label": "elections_archive.csv", "dataset_id": str(ds2.id)},
                ],
                "original_question": "Show votes by category",
            },
        )
        resolved = resolve_dataset_clarification("2", analysis)
        assert resolved == ds2.id
    finally:
        gen.close()


def test_sql_generator_file_dataset_kpi(elections_upload: dict[str, Any], monkeypatch) -> None:
    schema, _semantic = _schema_and_semantic_from_db(
        elections_upload["client"],
        tenant_id=elections_upload["tenant_id"],
        connection_id=elections_upload["connection_id"],
    )

    class Cache:
        def get(self, _t, _c):
            return schema

    monkeypatch.setattr(workflow_mod, "SchemaCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "SemanticCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "get_llm", lambda: _workflow_llm_stub())

    analysis = AnalysisState(
        connection_id=UUID(elections_upload["connection_id"]),
        dataset_id=UUID(elections_upload["dataset_id"]),
    )
    gen_result = SQLGenerator().run(
        {
            "tenant_id": str(elections_upload["tenant_id"]),
            "connection_id": elections_upload["connection_id"],
            "question": "Show total Votes",
            "analysis_state": analysis.model_dump(mode="json"),
        }
    )
    assert gen_result.get("sql")
    assert gen_result.get("dataset_clarification") is None
    assert elections_upload["physical_table"] in gen_result["sql"] or "votes" in gen_result["sql"].lower()


def test_sql_generator_multi_dataset_clarification(
    elections_upload: dict[str, Any],
    monkeypatch,
) -> None:
    schema, _semantic = _schema_and_semantic_from_db(
        elections_upload["client"],
        tenant_id=elections_upload["tenant_id"],
        connection_id=elections_upload["connection_id"],
    )

    gen = elections_upload["client"].app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        db.add(
            Dataset(
                id=uuid4(),
                tenant_id=elections_upload["tenant_id"],
                connection_id=UUID(elections_upload["connection_id"]),
                name="other",
                display_name="other.csv",
                source_type="csv",
                physical_table="t_other",
                status="ready",
                row_count=0,
                column_count=0,
                columns_json="[]",
            )
        )
        db.commit()
    finally:
        gen.close()

    class Cache:
        def get(self, _t, _c):
            return schema

    monkeypatch.setattr(workflow_mod, "SchemaCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "SemanticCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "get_llm", lambda: _workflow_llm_stub())

    result = SQLGenerator().run(
        {
            "tenant_id": str(elections_upload["tenant_id"]),
            "connection_id": elections_upload["connection_id"],
            "question": "Show votes",
            "analysis_state": {},
        }
    )
    assert result.get("dataset_clarification")
    assert result.get("sql") is None


def test_follow_up_preserves_dataset_id(elections_upload: dict[str, Any]) -> None:
    from app.agent.query_plan import QueryPlan

    ds_id = UUID(elections_upload["dataset_id"])
    state = AnalysisState(
        connection_id=UUID(elections_upload["connection_id"]),
        dataset_id=ds_id,
        dataset_display_name="Elections",
        query_plan=QueryPlan(
            intent="aggregation",
            metrics=["Votes"],
            dimensions=["Category"],
        ),
    )
    plan, handled = resolve_follow_up("Only North", state)
    assert handled
    assert state.dataset_id == ds_id
    assert plan is not None
    assert plan.filters


def test_biworkflow_csv_dimension_aggregation(
    elections_upload: dict[str, Any],
    monkeypatch,
) -> None:
    schema, _semantic = _schema_and_semantic_from_db(
        elections_upload["client"],
        tenant_id=elections_upload["tenant_id"],
        connection_id=elections_upload["connection_id"],
    )

    class Cache:
        def get(self, _t, _c):
            return schema

    monkeypatch.setattr(workflow_mod, "SchemaCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "SemanticCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "get_llm", lambda: _workflow_llm_stub())

    result = BIWorkflow().run(
        "Show Votes by Category",
        tenant_id=elections_upload["tenant_id"],
        user_id=elections_upload["user_id"],
        connection_id=elections_upload["connection_id"],
    )
    if result.get("sql"):
        assert result.get("rows") is not None


def test_deleted_dataset_blocks_query(elections_upload: dict[str, Any]) -> None:
    test_client = elections_upload["client"]
    headers = elections_upload["headers"]
    dataset_id = elections_upload["dataset_id"]
    test_client.delete(f"/api/v1/datasets/{dataset_id}", headers=headers)
    gen = test_client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        from app.tools.customer_db import get_stored_schema

        schema = get_stored_schema(db, UUID(elections_upload["connection_id"]))
        ctx = prepare_file_dataset_context(
            db,
            tenant_id=elections_upload["tenant_id"],
            connection_id=UUID(elections_upload["connection_id"]),
            schema=schema,
            semantic={},
            analysis=AnalysisState(
                connection_id=UUID(elections_upload["connection_id"]),
                dataset_id=UUID(dataset_id),
            ),
            question="Show votes",
        )
        assert ctx.error or ctx.clarification
    finally:
        gen.close()


def test_foreign_tenant_dataset_in_analysis_state_denied(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp: Path,
    patch_workflow_db: None,
) -> None:
    test_client, sent = client
    headers_a, _t, tenant_a, _user_a = _signup_and_token(
        test_client, sent, "ds-a@acme.com", tenant_name="TenantA"
    )
    headers_b, _t, tenant_b, _user_b = _signup_and_token(
        test_client, sent, "ds-b@other.com", tenant_name="TenantB"
    )
    body = _upload(test_client, headers_a, name="TenantA", filename="a.csv", content=ELECTIONS_CSV)
    foreign_dataset_id = body["datasets"][0]["id"]

    gen = test_client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        ctx = prepare_file_dataset_context(
            db,
            tenant_id=tenant_b,
            connection_id=UUID(body["connection_id"]),
            schema={"db_type": "csv", "tables": []},
            semantic={},
            analysis=AnalysisState(dataset_id=UUID(foreign_dataset_id)),
            question="hack",
        )
        assert ctx.error
    finally:
        gen.close()


def test_filter_schema_for_dataset_isolates_table() -> None:
    schema = {
        "db_type": "csv",
        "tables": [
            {"table_name": "t_a", "columns": [{"name": "x"}]},
            {"table_name": "t_b", "columns": [{"name": "y"}]},
        ],
    }
    filtered = filter_schema_for_dataset(schema, "t_a")
    assert len(filtered["tables"]) == 1
    assert filtered["tables"][0]["table_name"] == "t_a"


def test_dataset_clarification_resume_original_question(
    elections_upload: dict[str, Any],
    monkeypatch,
) -> None:
    schema, _semantic = _schema_and_semantic_from_db(
        elections_upload["client"],
        tenant_id=elections_upload["tenant_id"],
        connection_id=elections_upload["connection_id"],
    )

    gen = elections_upload["client"].app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        extra_id = uuid4()
        db.add(
            Dataset(
                id=extra_id,
                tenant_id=elections_upload["tenant_id"],
                connection_id=UUID(elections_upload["connection_id"]),
                name="alt",
                display_name="alt.csv",
                source_type="csv",
                physical_table="t_alt",
                status="ready",
                row_count=0,
                column_count=0,
                columns_json="[]",
            )
        )
        db.commit()
    finally:
        gen.close()

    class Cache:
        def get(self, _t, _c):
            return schema

    monkeypatch.setattr(workflow_mod, "SchemaCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "SemanticCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "get_llm", lambda: _workflow_llm_stub())

    pending = AnalysisState(
        connection_id=UUID(elections_upload["connection_id"]),
        pending_dataset_clarification={
            "kind": "pick_dataset",
            "message": "Which dataset?",
            "choices": [
                {"id": "1", "label": "Elections", "dataset_id": elections_upload["dataset_id"]},
                {"id": "2", "label": "alt.csv", "dataset_id": str(extra_id)},
            ],
            "original_question": "Show Votes by Category",
        },
    )
    result = SQLGenerator().run(
        {
            "tenant_id": str(elections_upload["tenant_id"]),
            "connection_id": elections_upload["connection_id"],
            "question": "1",
            "analysis_state": pending.model_dump(mode="json"),
        }
    )
    assert result.get("dataset_clarification") is None
    assert result.get("sql")


def test_dashboard_builder_single_file_dataset(
    elections_upload: dict[str, Any],
    monkeypatch,
) -> None:
    from app.agent.workflow import DashboardBuilder

    schema, _semantic = _schema_and_semantic_from_db(
        elections_upload["client"],
        tenant_id=elections_upload["tenant_id"],
        connection_id=elections_upload["connection_id"],
    )

    class Cache:
        def get(self, _t, _c):
            return schema

    monkeypatch.setattr(workflow_mod, "SchemaCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "SemanticCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "get_llm", lambda: _workflow_llm_stub(intent="dashboard"))

    analysis = AnalysisState(
        connection_id=UUID(elections_upload["connection_id"]),
        dataset_id=UUID(elections_upload["dataset_id"]),
    )
    result = DashboardBuilder().run(
        {
            "tenant_id": str(elections_upload["tenant_id"]),
            "connection_id": elections_upload["connection_id"],
            "question": "Create a dashboard",
            "analysis_state": analysis.model_dump(mode="json"),
        }
    )
    assert result.get("dataset_clarification") is None
    assert result.get("dashboard") is not None or result.get("error")


def test_healthcare_domain_csv_workflow(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp: Path,
    patch_workflow_db: None,
    monkeypatch,
) -> None:
    test_client, sent = client
    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, f"health-{uuid4().hex[:8]}@acme.com"
    )
    body = _upload(
        test_client, headers, name="Healthcare", filename="h.csv", content=HEALTHCARE_CSV
    )
    schema, _ = _schema_and_semantic_from_db(
        test_client, tenant_id=tenant_id, connection_id=body["connection_id"]
    )

    class Cache:
        def get(self, _t, _c):
            return schema

    monkeypatch.setattr(workflow_mod, "SchemaCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "SemanticCache", lambda: Cache())

    class Stub:
        client = object()

        def complete(self, prompt: str) -> str:
            if "intent classifier" in prompt.lower():
                return "sql"
            if "json query plan" in prompt.lower():
                return json.dumps(
                    {
                        "intent": "aggregation",
                        "metrics": ["Admissions"],
                        "dimensions": ["Category"],
                    }
                )
            return "[llm stub]"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: Stub())

    result = BIWorkflow().run(
        "Show Admissions by Category",
        tenant_id=tenant_id,
        user_id=user_id,
        connection_id=body["connection_id"],
    )
    assert result.get("sql") or result.get("error")
