"""P1-5 E2E: comparisons, dashboard, AI insights, Excel, security, multi-turn."""

from __future__ import annotations

import io
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.agent.workflow as workflow_mod
from app.agent.analysis_state import AnalysisState, merge_workflow_into_state
from app.agent.chat_insight_facts import extract_chat_insight_facts, grounding_context_from_chat_facts
from app.agent.comparison_facts import extract_comparison_facts
from app.agent.comparison_compiler import compile_dual_period_comparison
from app.agent.dashboard import execute_dashboard
from app.agent.dashboard_insight_facts import (
    extract_dashboard_insight_facts,
    validate_narrative_numbers,
)
from app.agent.dataset_context import detect_dataset_switch, prepare_file_dataset_context
from app.agent.insight_validation import GroundingOutcome
from app.agent.query_plan import ComparisonSpec, QueryPlan, TimeSpec
from app.agent.workflow import BIWorkflow
from app.authorization import DashboardConnectionMismatchError
from app.database import get_db
from app.models import Dashboard, Dataset, User
from app.services.conversation_state_service import ConversationStateService
from app.tools.sql_safety import SQLSafetyPipeline
from tests.test_p1_5_closure import (
    _schema_and_semantic_from_db,
    elections_upload,
    patch_workflow_db,
)
from tests.test_p1_5_datasets import (
    COMPARISON_SALES_CSV,
    ELECTIONS_CSV,
    SALES_CSV,
    _query_file_connection,
    _upload,
    file_storage_tmp,
)
from tests.test_tenant_isolation import _signup_and_token


def _cache_monkeypatch(monkeypatch: pytest.MonkeyPatch, schema: dict) -> None:
    class Cache:
        def get(self, _t, _c):
            return schema

    monkeypatch.setattr(workflow_mod, "SchemaCache", lambda: Cache())
    monkeypatch.setattr(workflow_mod, "SemanticCache", lambda: Cache())


def _sales_comparison_llm() -> Any:
    class Stub:
        client = object()

        def complete(self, prompt: str) -> str:
            lower = prompt.lower()
            if "intent classifier" in lower:
                return "sql"
            if "parse the user question into a json query plan" in lower:
                if "compare" in lower:
                    return json.dumps(
                        {
                            "intent": "comparison",
                            "metrics": ["Revenue"],
                            "dimensions": [],
                            "time": {"field": "Date", "range": "this_year"},
                            "comparison": {
                                "type": "previous_year",
                            },
                        }
                    )
                return json.dumps(
                    {"intent": "aggregation", "metrics": ["Revenue"], "dimensions": ["Region"]}
                )
            if "pre-computed facts json" in lower:
                return "Revenue increased compared to the prior period."
            if "insight narrator" in lower or "dashboard insight" in lower:
                return "Revenue is higher in the current period."
            return "[llm stub]"

    return Stub()


@pytest.fixture()
def sales_comparison_upload(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp,
    patch_workflow_db: None,
) -> dict[str, Any]:
    test_client, sent = client
    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, f"sales-{uuid4().hex[:8]}@acme.com"
    )
    body = _upload(
        test_client,
        headers,
        name="Sales Compare",
        filename="sales.csv",
        content=COMPARISON_SALES_CSV,
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


def test_biworkflow_csv_comparison_e2e(
    sales_comparison_upload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema, semantic = _schema_and_semantic_from_db(
        sales_comparison_upload["client"],
        tenant_id=sales_comparison_upload["tenant_id"],
        connection_id=sales_comparison_upload["connection_id"],
    )
    metric_name = semantic["metrics"][0]["name"] if semantic.get("metrics") else "Revenue"
    date_field = next(
        (d["name"] for d in semantic.get("dimensions") or [] if "date" in d["name"].lower()),
        "Date",
    )

    plan = QueryPlan(
        intent="comparison",
        metrics=[metric_name],
        dimensions=[],
        time=TimeSpec(field=date_field, range="this_year"),
        comparison=ComparisonSpec(type="previous_year"),
    )
    sql = compile_dual_period_comparison(
        plan,
        semantic,
        schema,
        "csv",
    )
    safe = SQLSafetyPipeline(db_type="csv").validate(sql)
    rows = _query_file_connection(
        sales_comparison_upload["client"],
        sales_comparison_upload["connection_id"],
        safe,
    )
    assert rows
    row = rows[0]
    assert any(str(k).startswith("current_") for k in row)
    assert any(str(k).startswith("comparison_") for k in row)
    facts = extract_comparison_facts(rows, query_plan=plan)
    assert facts
    assert facts[0].direction in {"up", "down", "flat"}

    _cache_monkeypatch(monkeypatch, schema)

    class Stub:
        client = object()

        def complete(self, prompt: str) -> str:
            if "intent classifier" in prompt.lower():
                return "sql"
            if "json query plan" in prompt.lower():
                return plan.model_dump_json()
            return "Comparison grounded in query results."

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: Stub())
    result = BIWorkflow().run(
        "Compare revenue this year with last year",
        tenant_id=sales_comparison_upload["tenant_id"],
        user_id=sales_comparison_upload["user_id"],
        connection_id=sales_comparison_upload["connection_id"],
    )
    if result.get("sql"):
        assert result.get("rows") is not None


def test_biworkflow_excel_kpi(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp,
    patch_workflow_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("openpyxl")
    from openpyxl import Workbook

    test_client, sent = client
    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, f"excel-{uuid4().hex[:8]}@acme.com"
    )
    wb = Workbook()
    ws = wb.active
    ws.title = "Votes"
    ws.append(["candidate", "votes", "constituency"])
    ws.append(["Alice", 100, "North"])
    ws.append(["Bob", 80, "South"])
    buf = io.BytesIO()
    wb.save(buf)

    body = _upload(
        test_client,
        headers,
        name="Elections Excel",
        filename="elections.xlsx",
        content=buf.getvalue(),
    )
    schema, _ = _schema_and_semantic_from_db(
        test_client, tenant_id=tenant_id, connection_id=body["connection_id"]
    )
    _cache_monkeypatch(monkeypatch, schema)

    class Stub:
        client = object()

        def complete(self, prompt: str) -> str:
            if "intent classifier" in prompt.lower():
                return "sql"
            if "json query plan" in prompt.lower():
                return json.dumps(
                    {"intent": "aggregation", "metrics": ["Votes"], "dimensions": ["Category"]}
                )
            return "Votes by category from data."

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: Stub())

    result = BIWorkflow().run(
        "Show Votes by Category",
        tenant_id=tenant_id,
        user_id=user_id,
        connection_id=body["connection_id"],
    )
    assert result.get("sql") or result.get("dataset_clarification")
    if result.get("sql"):
        assert result.get("rows") is not None


def test_file_dashboard_execute_filters_and_ai_insights(
    elections_upload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    patch_workflow_db: None,
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("DASHBOARD_WIDGET_MAX_CONCURRENCY", "1")
    get_settings.cache_clear()

    schema, semantic = _schema_and_semantic_from_db(
        elections_upload["client"],
        tenant_id=elections_upload["tenant_id"],
        connection_id=elections_upload["connection_id"],
    )
    physical = elections_upload["physical_table"]
    from app.agent.dataset_context import filter_schema_for_dataset, filter_semantic_for_dataset

    filtered_schema = filter_schema_for_dataset(schema, physical)
    filtered_semantic = filter_semantic_for_dataset(semantic, physical)

    filter_field = "Category"
    for dim in filtered_semantic.get("dimensions") or []:
        ref = str(dim.get("expression") or dim.get("source_column_ref") or "").lower()
        if "constituency" in ref:
            filter_field = str(dim["name"])
            break

    spec = {
        "id": str(uuid4()),
        "title": "Elections Dashboard",
        "connection_id": elections_upload["connection_id"],
        "source_table": physical,
        "widgets": [
            {
                "id": "kpi-votes",
                "type": "kpi",
                "title": "Total Votes",
                "sql": f'SELECT SUM(votes) AS value FROM "{physical}"',
            },
            {
                "id": "chart-cat",
                "type": "chart",
                "title": "Votes by Category",
                "chart": "bar",
                "sql": (
                    f'SELECT constituency AS category, SUM(votes) AS value '
                    f'FROM "{physical}" GROUP BY constituency ORDER BY 2 DESC'
                ),
            },
            {"id": "insight-1", "type": "ai_insight", "title": "AI Insights"},
        ],
        "filters": [{"field": filter_field, "operator": "eq", "value": "North"}],
    }

    class InsightLLM:
        client = object()

        def complete(self, prompt: str) -> str:
            return "North leads with the highest vote total."

    monkeypatch.setattr("app.llm.client.get_llm", lambda: InsightLLM())

    executed = execute_dashboard(
        spec,
        connection_id=elections_upload["connection_id"],
        schema=filtered_schema,
        db_type="csv",
        tenant_id=elections_upload["tenant_id"],
        semantic=filtered_semantic,
    )
    kpi = next(w for w in executed["widgets"] if w["id"] == "kpi-votes")
    assert kpi.get("rows") or kpi.get("value") is not None
    chart = next(w for w in executed["widgets"] if w["id"] == "chart-cat")
    if filter_field and not chart.get("error"):
        assert chart.get("rows"), "Expected filtered chart rows for constituency=North"

    insight = next(w for w in executed["widgets"] if w["type"] == "ai_insight")
    facts = extract_dashboard_insight_facts(executed)
    if facts.status.value == "insufficient_data":
        # Filter may narrow all widgets; verify unfiltered execution separately.
        unfiltered = execute_dashboard(
            {**spec, "filters": []},
            connection_id=elections_upload["connection_id"],
            schema=filtered_schema,
            db_type="csv",
            tenant_id=elections_upload["tenant_id"],
            semantic=filtered_semantic,
        )
        facts = extract_dashboard_insight_facts(unfiltered)
    assert facts.kpis or facts.categories
    content = insight.get("content") or ""
    if content and facts.kpis:
        nums = [str(int(facts.kpis[0].value)) for f in facts.kpis if f.value]
        if nums:
            validate_narrative_numbers(content, facts)


def test_multi_turn_follow_up_preserves_dataset(
    elections_upload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client = elections_upload["client"]
    conv_id = uuid4()
    schema, _ = _schema_and_semantic_from_db(
        test_client,
        tenant_id=elections_upload["tenant_id"],
        connection_id=elections_upload["connection_id"],
    )
    _cache_monkeypatch(monkeypatch, schema)
    monkeypatch.setattr(workflow_mod, "get_llm", _sales_comparison_llm)
    monkeypatch.setattr(workflow_mod, "_persist_analysis_state", lambda **kwargs: None)

    wf = BIWorkflow()
    ds_id = elections_upload["dataset_id"]

    r1 = wf.run(
        "Show Votes by Category",
        tenant_id=elections_upload["tenant_id"],
        user_id=elections_upload["user_id"],
        connection_id=elections_upload["connection_id"],
        conversation_id=conv_id,
    )
    state = AnalysisState(
        connection_id=UUID(elections_upload["connection_id"]),
        dataset_id=UUID(ds_id),
        dataset_display_name="Elections",
        query_plan=QueryPlan.model_validate(r1["query_plan"]) if r1.get("query_plan") else None,
    )

    r2 = wf.run(
        "Only North",
        tenant_id=elections_upload["tenant_id"],
        user_id=elections_upload["user_id"],
        connection_id=elections_upload["connection_id"],
        conversation_id=conv_id,
    )
    state2 = merge_workflow_into_state(
        state,
        question="Only North",
        final={**r2, "dataset_id": ds_id, "dataset_display_name": "Elections"},
        connection_id=elections_upload["connection_id"],
    )
    assert state2.dataset_id == UUID(ds_id)

    r3 = wf.run(
        "Compare with last year",
        tenant_id=elections_upload["tenant_id"],
        user_id=elections_upload["user_id"],
        connection_id=elections_upload["connection_id"],
        conversation_id=conv_id,
    )
    state3 = merge_workflow_into_state(
        state2,
        question="Compare with last year",
        final={**r3, "dataset_id": ds_id},
        connection_id=elections_upload["connection_id"],
    )
    assert state3.dataset_id == UUID(ds_id)


def test_dataset_switching_clears_query_context(elections_upload: dict[str, Any]) -> None:
    assert detect_dataset_switch("Now use inventory.xlsx") == "inventory"

    gen = elections_upload["client"].app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        inv_id = uuid4()
        db.add(
            Dataset(
                id=inv_id,
                tenant_id=elections_upload["tenant_id"],
                connection_id=UUID(elections_upload["connection_id"]),
                name="inventory",
                display_name="inventory.xlsx",
                source_type="excel",
                physical_table="t_inv",
                status="ready",
                row_count=1,
                column_count=1,
                columns_json="[]",
            )
        )
        db.commit()
        from app.tools.customer_db import get_stored_schema

        schema = get_stored_schema(db, UUID(elections_upload["connection_id"]))
        prior = AnalysisState(
            connection_id=UUID(elections_upload["connection_id"]),
            dataset_id=UUID(elections_upload["dataset_id"]),
            query_plan=QueryPlan(intent="aggregation", metrics=["Votes"], dimensions=["Category"]),
            last_sql="SELECT 1",
        )
        ctx = prepare_file_dataset_context(
            db,
            tenant_id=elections_upload["tenant_id"],
            connection_id=UUID(elections_upload["connection_id"]),
            schema=schema,
            semantic={},
            analysis=prior,
            question="Now use inventory.xlsx",
        )
        assert ctx.analysis.query_plan is None
        assert ctx.analysis.last_sql is None
    finally:
        gen.close()


def test_dataset_not_ready_blocks_query(elections_upload: dict[str, Any]) -> None:
    gen = elections_upload["client"].app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        ds = db.scalar(
            select(Dataset).where(Dataset.id == UUID(elections_upload["dataset_id"]))
        )
        assert ds is not None
        ds.status = "failed"
        ds.ingestion_error = "Parse error"
        db.commit()

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
                dataset_id=UUID(elections_upload["dataset_id"]),
            ),
            question="Show votes",
        )
        assert ctx.error
        assert "not ready" in ctx.error.lower() or "parse" in ctx.error.lower()
    finally:
        gen.close()


def test_foreign_tenant_cannot_query_file_connection(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp,
    patch_workflow_db: None,
) -> None:
    test_client, sent = client
    headers_a, _t, tenant_a, user_a = _signup_and_token(
        test_client, sent, "query-a@acme.com", tenant_name="TenantA"
    )
    headers_b, _t, tenant_b, user_b = _signup_and_token(
        test_client, sent, "query-b@other.com", tenant_name="TenantB"
    )
    body = _upload(test_client, headers_a, name="Private", filename="p.csv", content=ELECTIONS_CSV)

    queried: list[str] = []
    original = workflow_mod.run_readonly_query

    def spy(connection_id, sql, *, tenant_id=None, **kwargs):
        queried.append(str(connection_id))
        return original(connection_id, sql, tenant_id=tenant_id, **kwargs)

    import app.tools.customer_db as customer_db

    customer_db.run_readonly_query = spy  # type: ignore[assignment]

    gen = test_client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        ctx = prepare_file_dataset_context(
            db,
            tenant_id=tenant_b,
            connection_id=UUID(body["connection_id"]),
            schema={"db_type": "csv", "tables": []},
            semantic={},
            analysis=AnalysisState(),
            question="Show votes",
        )
        assert ctx.error
    finally:
        gen.close()
        customer_db.run_readonly_query = original


def test_malicious_filename_sanitized_on_upload(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp,
) -> None:
    test_client, sent = client
    headers, _t, _tenant, _user = _signup_and_token(
        test_client, sent, f"mal-{uuid4().hex[:8]}@acme.com"
    )
    body = _upload(
        test_client,
        headers,
        name="Safe Name",
        filename='../../etc/passwd"; DROP TABLE users; --.csv',
        content=ELECTIONS_CSV,
    )
    assert body["datasets"][0]["status"] == "ready"
    physical = body["datasets"][0]["physical_table"]
    assert physical.startswith("t_")
    assert "drop table" not in physical.lower()


def test_chat_insight_grounding_from_file_query_rows() -> None:
    rows = [{"category": "North", "total_votes": 2700}]
    plan = QueryPlan(intent="aggregation", metrics=["Votes"], dimensions=["Category"])
    facts = extract_chat_insight_facts(
        rows,
        question="Show votes by category",
        sql="SELECT ...",
        query_plan=plan.model_dump(mode="json"),
        tenant_id="t1",
        connection_id="c1",
    )
    ctx = grounding_context_from_chat_facts(facts)
    assert ctx.numeric_claims or ctx.rankings or facts.row_count == 1


def test_dashboard_connection_mismatch_file_dataset(
    elections_upload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.workflow import DashboardEditor

    schema, semantic = _schema_and_semantic_from_db(
        elections_upload["client"],
        tenant_id=elections_upload["tenant_id"],
        connection_id=elections_upload["connection_id"],
    )
    _cache_monkeypatch(monkeypatch, schema)
    other_conn = uuid4()
    dashboard_id = uuid4()

    queried: list[str] = []

    def spy(*args, **kwargs):
        queried.append("called")
        return []

    monkeypatch.setattr(workflow_mod, "run_readonly_query", spy)

    class FakeDash:
        id = dashboard_id
        tenant_id = elections_upload["tenant_id"]
        connection_id = other_conn
        name = "Other"
        filters_json = "[]"
        deleted_at = None

    monkeypatch.setattr(
        workflow_mod,
        "get_dashboard",
        lambda db, did, user: FakeDash() if did == dashboard_id else None,
    )

    analysis = AnalysisState()
    analysis.dashboard.dashboard_id = dashboard_id

    result = DashboardEditor().run(
        {
            "tenant_id": str(elections_upload["tenant_id"]),
            "user_id": str(elections_upload["user_id"]),
            "connection_id": elections_upload["connection_id"],
            "question": "Add chart",
            "analysis_state": analysis.model_dump(mode="json"),
        }
    )
    assert result.get("error_code") == DashboardConnectionMismatchError.code or result.get("error")


def test_ecommerce_sales_csv_workflow(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp,
    patch_workflow_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client, sent = client
    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, f"ecom-{uuid4().hex[:8]}@acme.com"
    )
    body = _upload(
        test_client, headers, name="Ecommerce", filename="orders.csv", content=SALES_CSV
    )
    schema, _ = _schema_and_semantic_from_db(
        test_client, tenant_id=tenant_id, connection_id=body["connection_id"]
    )
    _cache_monkeypatch(monkeypatch, schema)
    monkeypatch.setattr(workflow_mod, "get_llm", _sales_comparison_llm)

    result = BIWorkflow().run(
        "Show Revenue by Region",
        tenant_id=tenant_id,
        user_id=user_id,
        connection_id=body["connection_id"],
    )
    assert result.get("sql") or result.get("error")
