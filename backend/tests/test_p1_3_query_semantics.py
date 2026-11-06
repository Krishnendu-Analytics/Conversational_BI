"""P1-3: Semantic query correctness and analytical intent."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.agent.analysis_state import AnalysisState, merge_workflow_into_state
from app.agent.follow_up import resolve_follow_up, resolve_query_clarification
from app.agent.query_plan import FilterSpec, QueryPlan, SortSpec, TimeSpec
from app.agent.query_plan_validation import (
    QueryClarification,
    format_clarification_answer,
    normalize_query_plan,
    resolve_time_phrase,
    infer_comparison_type,
    validate_query_plan,
    verify_sql_reflects_plan,
)
from app.agent.sql_compiler import compile_query_plan
from app.agent.workflow import SQLGenerator
from tests.test_sql_generator import FakeLLM, FakeSchemaCache, FakeSemanticCache


def _sales_catalog() -> dict[str, Any]:
    return {
        "metrics": [
            {"name": "Revenue", "expression": "SUM(sales.amount)"},
            {"name": "Gross Sales", "expression": "SUM(sales.gross_amount)"},
            {"name": "Net Sales", "expression": "SUM(sales.net_amount)"},
            {"name": "Order Count", "expression": "COUNT(DISTINCT sales.order_id)"},
        ],
        "dimensions": [
            {"name": "Region", "expression": "sales.region"},
            {"name": "Product", "expression": "sales.product_name"},
            {"name": "Order Date", "expression": "sales.order_date"},
            {"name": "Shipment Date", "expression": "sales.shipment_date"},
        ],
        "relationships": [],
    }


def _sales_schema() -> dict[str, Any]:
    return {
        "connection_id": "connection-123",
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "sales",
                "columns": [
                    {"name": "amount", "data_type": "numeric"},
                    {"name": "gross_amount", "data_type": "numeric"},
                    {"name": "net_amount", "data_type": "numeric"},
                    {"name": "order_id", "data_type": "integer"},
                    {"name": "region", "data_type": "text"},
                    {"name": "product_name", "data_type": "text"},
                    {"name": "order_date", "data_type": "date"},
                    {"name": "shipment_date", "data_type": "date"},
                ],
                "foreign_keys": [],
            }
        ],
    }


def _region_revenue_plan() -> QueryPlan:
    return QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        dimensions=["Region"],
    )


# --- Time semantics ---


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("compare with last year", "last_year"),
        ("previous year revenue", "last_year"),
        ("year to date", "ytd"),
        ("ytd sales", "ytd"),
        ("last month totals", "last_month"),
        ("this month only", "this_month"),
        ("last 7 days", "last_7_days"),
        ("last quarter", "last_quarter"),
    ],
)
def test_resolve_time_phrase(phrase: str, expected: str) -> None:
    spec = resolve_time_phrase(phrase)
    assert spec is not None
    assert spec.range == expected


def test_last_year_is_not_ytd() -> None:
    assert resolve_time_phrase("last year").range == "last_year"
    assert resolve_time_phrase("last year").range != "ytd"


def test_follow_up_last_year_sets_comparison_intent() -> None:
    prior = AnalysisState(query_plan=_region_revenue_plan(), turn_count=1)
    plan, handled = resolve_follow_up("Compare with last year", prior)
    assert handled is True
    assert plan is not None
    assert plan.intent == "comparison"
    assert plan.time is not None
    assert plan.time.range == "ytd"
    assert plan.comparison is not None
    assert plan.comparison.type == "previous_year"


def test_follow_up_last_month_not_rolling_30_days() -> None:
    prior = AnalysisState(query_plan=_region_revenue_plan(), turn_count=1)
    plan, handled = resolve_follow_up("last month", prior)
    assert handled is True
    assert plan is not None
    assert plan.time is not None
    assert plan.time.range == "last_month"


def test_compile_last_year_predicate_postgres() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        time=TimeSpec(field="Order Date", range="last_year"),
    )
    sql = compile_query_plan(plan, _sales_catalog(), _sales_schema(), "postgres")
    assert "DATE_TRUNC('year'" in sql
    assert "INTERVAL '1 year'" in sql
    assert "ytd" not in sql.lower()


def test_compile_ytd_predicate_postgres() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        time=TimeSpec(field="Order Date", range="ytd"),
    )
    sql = compile_query_plan(plan, _sales_catalog(), _sales_schema(), "postgres")
    assert "DATE_TRUNC('year', CURRENT_DATE)" in sql
    assert "INTERVAL '1 year'" not in sql


# --- Metric / dimension validation ---


def test_validate_accepts_known_metric_and_dimension() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        dimensions=["Region"],
    )
    result = validate_query_plan(
        plan,
        question="Show revenue by region",
        semantic_catalog=_sales_catalog(),
    )
    assert result.ok is True
    assert result.clarification is None


def test_validate_rejects_unknown_metric() -> None:
    plan = QueryPlan(intent="aggregation", metrics=["Profit"], dimensions=["Region"])
    result = validate_query_plan(
        plan,
        question="Show profit by region",
        semantic_catalog=_sales_catalog(),
    )
    assert result.ok is False
    assert result.clarification is not None
    # A friendly "which metric did you mean?" clarification, not a raw
    # "Unknown metric: Profit" string surfaced as the chat answer.
    assert result.clarification.kind == "pick_metric"
    assert "Profit" in result.clarification.message
    assert result.clarification.choices


def test_validate_rejects_unknown_dimension_with_friendly_choices() -> None:
    plan = QueryPlan(intent="detail", metrics=["Revenue"], dimensions=["Customer"])
    result = validate_query_plan(
        plan,
        question="top 5 customer with sales amount",
        semantic_catalog=_sales_catalog(),
    )
    assert result.ok is False
    assert result.clarification is not None
    # Same fix as the metric case: a friendly "which field?" clarification
    # naming what was asked for, not a raw "Unknown dimension: Customer"
    # string surfaced as the chat answer.
    assert result.clarification.kind == "pick_dimension"
    assert "Customer" in result.clarification.message
    assert result.clarification.choices
    labels = {c.label for c in result.clarification.choices}
    assert "Region" in labels


def test_validate_unknown_dimension_with_no_confirmed_dimensions_at_all() -> None:
    """No dimensions confirmed for this connection at all (a real, common
    state — see the "cff" connection, which had 52 suggested dimensions
    and zero confirmed) — the message should say so plainly instead of
    showing an empty choice list."""
    catalog = {
        "metrics": [{"name": "Revenue", "expression": "SUM(sales.amount)"}],
        "dimensions": [],
        "relationships": [],
    }
    plan = QueryPlan(intent="detail", metrics=["Revenue"], dimensions=["Customer"])
    result = validate_query_plan(plan, question="top 5 customer", semantic_catalog=catalog)

    assert result.ok is False
    assert result.clarification is not None
    assert result.clarification.kind == "pick_dimension"
    assert not result.clarification.choices
    assert "no" in result.clarification.message.lower()
    assert "Semantic settings" in result.clarification.message


def test_validate_rejects_unknown_filter_field_with_friendly_choices() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        filters=[FilterSpec(field="Salesperson", operator="eq", value="Alice")],
    )
    result = validate_query_plan(
        plan, question="revenue for Alice", semantic_catalog=_sales_catalog()
    )
    assert result.ok is False
    assert result.clarification is not None
    assert result.clarification.kind == "pick_dimension"
    assert "Salesperson" in result.clarification.message
    assert result.clarification.choices


def test_validate_rejects_unknown_time_field_with_friendly_choices() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        time=TimeSpec(field="Delivery Date", range="last_quarter"),
    )
    result = validate_query_plan(
        plan, question="revenue last quarter by delivery date", semantic_catalog=_sales_catalog()
    )
    assert result.ok is False
    assert result.clarification is not None
    assert result.clarification.kind == "pick_date_field"
    assert "Delivery Date" in result.clarification.message


def test_validate_no_metric_candidates_ranks_choices_by_relevance() -> None:
    """Regression test for an observed live bug: "top 5 customer with
    sales amount" doesn't literally substring-match any confirmed metric
    name ("Net Sales Value" isn't a substring of "sales amount" or vice
    versa), so _question_metric_candidates finds zero matches. Before this
    fix, the fallback then showed metric_map.values()[:6] in arbitrary
    dict order — real observed output offered "Available Hours",
    "Downtime Hours" for a question about sales, which looks like a
    considered suggestion but is random. Ranking by keyword overlap must
    put anything with "sales" in its name ahead of unrelated metrics.
    """
    catalog = {
        "metrics": [
            {"name": "Available Hours", "expression": "SUM(ops.available_hours)"},
            {"name": "Downtime Hours", "expression": "SUM(ops.downtime_hours)"},
            {"name": "Cogs Value", "expression": "SUM(sales.cogs_value)"},
            {"name": "Net Sales Value", "expression": "SUM(sales.net_sales_value)"},
            {"name": "Freight Cost", "expression": "SUM(logistics.freight_cost)"},
        ],
        "dimensions": [],
        "relationships": [],
    }
    plan = QueryPlan(intent="aggregation", metrics=[], dimensions=["Customer"])
    result = validate_query_plan(
        plan, question="top 5 customer with sales amount", semantic_catalog=catalog
    )

    assert result.ok is False
    assert result.clarification is not None
    assert result.clarification.kind == "pick_metric"
    labels = [c.label for c in result.clarification.choices]
    assert labels[0] == "Net Sales Value"


def test_validate_ambiguous_metric_clarification() -> None:
    plan = QueryPlan(intent="aggregation", metrics=[], dimensions=["Region"])
    result = validate_query_plan(
        plan,
        question="Show gross sales and net sales by region",
        semantic_catalog=_sales_catalog(),
    )
    assert result.ok is False
    assert result.clarification is not None
    assert result.clarification.kind == "pick_metric"
    labels = {c.label for c in result.clarification.choices}
    assert "Gross Sales" in labels
    assert "Net Sales" in labels


def test_validate_ambiguous_date_field_clarification() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        time=TimeSpec(range="last_month"),
    )
    result = validate_query_plan(
        plan,
        question="Revenue last month",
        semantic_catalog=_sales_catalog(),
    )
    assert result.ok is False
    assert result.clarification is not None
    assert result.clarification.kind == "pick_date_field"
    labels = {c.label for c in result.clarification.choices}
    assert "Order Date" in labels
    assert "Shipment Date" in labels


def test_normalize_single_metric_from_question() -> None:
    plan = QueryPlan(intent="aggregation", metrics=[], dimensions=["Region"])
    normalized = normalize_query_plan(
        plan,
        question="Show order count by region",
        semantic_catalog=_sales_catalog(),
    )
    assert normalized.metrics == ["Order Count"]


def test_normalize_top_n_adds_desc_sort_on_metric() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        dimensions=["Product"],
        limit=5,
    )
    normalized = normalize_query_plan(plan, semantic_catalog=_sales_catalog())
    assert normalized.sort
    assert normalized.sort[0].field == "Revenue"
    assert normalized.sort[0].direction == "desc"


def test_normalize_bottom_n_adds_asc_sort() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["bottom Revenue"],
        dimensions=["Product"],
        limit=3,
    )
    normalized = normalize_query_plan(plan, semantic_catalog=_sales_catalog())
    assert normalized.sort[0].direction == "asc"


# --- Aggregation / SQL compilation ---


def test_compile_uses_sum_for_revenue_metric() -> None:
    plan = QueryPlan(intent="aggregation", metrics=["Revenue"], dimensions=["Region"])
    sql = compile_query_plan(plan, _sales_catalog(), _sales_schema(), "postgres")
    assert "SUM(sales.amount)" in sql
    assert "GROUP BY sales.region" in sql


def test_compile_uses_count_distinct_for_order_count() -> None:
    plan = QueryPlan(intent="aggregation", metrics=["Order Count"], dimensions=["Region"])
    sql = compile_query_plan(plan, _sales_catalog(), _sales_schema(), "postgres")
    assert "COUNT(DISTINCT sales.order_id)" in sql


def test_compile_top_n_limit_and_order() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        dimensions=["Product"],
        limit=5,
        sort=[SortSpec(field="Revenue", direction="desc")],
    )
    sql = compile_query_plan(plan, _sales_catalog(), _sales_schema(), "postgres")
    assert "LIMIT 5" in sql
    assert "DESC" in sql


def test_compile_filter_applied_to_intended_field() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        dimensions=["Region"],
        filters=[FilterSpec(field="Region", operator="eq", value="West")],
    )
    sql = compile_query_plan(plan, _sales_catalog(), _sales_schema(), "postgres")
    assert "sales.region = 'West'" in sql


def test_verify_sql_reflects_plan_accepts_matching_sql() -> None:
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        dimensions=["Region"],
        limit=5,
        sort=[SortSpec(field="Revenue", direction="desc")],
    )
    sql = compile_query_plan(plan, _sales_catalog(), _sales_schema(), "postgres")
    result = verify_sql_reflects_plan(sql, plan, _sales_catalog())
    assert result.ok is True


def test_verify_sql_reflects_plan_rejects_wrong_metric() -> None:
    plan = QueryPlan(intent="aggregation", metrics=["Revenue"], dimensions=["Region"])
    sql = "SELECT sales.region, COUNT(*) AS row_count FROM public.sales GROUP BY sales.region"
    result = verify_sql_reflects_plan(sql, plan, _sales_catalog())
    assert result.ok is False


# --- Follow-up semantics ---


def test_follow_up_filter_preservation() -> None:
    prior = AnalysisState(
        query_plan=_region_revenue_plan(),
        turn_count=1,
        last_sql="SELECT region, SUM(revenue) FROM sales GROUP BY region",
    )
    plan, handled = resolve_follow_up("Only West", prior)
    assert handled is True
    assert plan is not None
    assert plan.metrics == ["Revenue"]
    assert plan.dimensions == ["Region"]
    assert len(plan.filters) == 1
    assert plan.filters[0].value == "West"


def test_follow_up_top_n_preserves_prior_semantics() -> None:
    prior = AnalysisState(query_plan=_region_revenue_plan(), turn_count=1)
    plan, handled = resolve_follow_up("top 5", prior)
    assert handled is True
    assert plan is not None
    assert plan.limit == 5
    assert plan.metrics == ["Revenue"]
    assert plan.sort[0].direction == "desc"


def test_follow_up_bottom_n_sort_asc() -> None:
    prior = AnalysisState(query_plan=_region_revenue_plan(), turn_count=1)
    plan, handled = resolve_follow_up("bottom 3", prior)
    assert handled is True
    assert plan is not None
    assert plan.limit == 3
    assert plan.sort[0].direction == "asc"


# --- Clarification flow / no execution ---


def test_sql_generator_returns_clarification_without_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.query_plan import QueryPlan as QP

    monkeypatch.setattr(
        "app.agent.workflow.parse_query_plan",
        lambda *args, **kwargs: QP(
            intent="aggregation",
            metrics=[],
            dimensions=["Region"],
            time=TimeSpec(range="last_month"),
        ),
    )

    generator = SQLGenerator(
        schema_cache=FakeSchemaCache(_sales_schema()),
        semantic_cache=FakeSemanticCache(_sales_catalog()),
        llm=FakeLLM("SELECT 1"),
    )
    result = generator.run(
        {
            "tenant_id": "tenant-123",
            "connection_id": "connection-123",
            "question": "Revenue last month by region",
        }
    )

    assert result["sql"] is None
    assert result.get("query_clarification") is not None
    assert result["query_clarification"]["kind"] == "pick_date_field"
    assert result.get("error") is None


def test_sql_generator_skips_llm_when_catalog_exists_and_compile_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.query_plan import QueryPlan as QP

    monkeypatch.setattr(
        "app.agent.workflow.parse_query_plan",
        lambda *args, **kwargs: QP(
            intent="aggregation",
            metrics=["UnknownMetric"],
            dimensions=["Region"],
        ),
    )

    llm = FakeLLM("SELECT evil FROM nowhere")
    generator = SQLGenerator(
        schema_cache=FakeSchemaCache(_sales_schema()),
        semantic_cache=FakeSemanticCache(_sales_catalog()),
        llm=llm,
    )
    result = generator.run(
        {
            "tenant_id": "tenant-123",
            "connection_id": "connection-123",
            "question": "Show unknown metric",
        }
    )

    assert result["sql"] is None
    assert result.get("query_clarification") is not None
    assert llm.prompts == []


def test_merge_workflow_persists_clarification_without_last_sql() -> None:
    clarification = {
        "kind": "pick_metric",
        "message": "Which metric should I use?",
        "choices": [{"id": "1", "label": "Gross Sales"}],
    }
    prior = AnalysisState(last_sql="SELECT 1", turn_count=2)
    updated = merge_workflow_into_state(
        prior,
        question="gross or net sales?",
        final={
            "answer": format_clarification_answer(
                QueryClarification.model_validate(clarification)
            ),
            "intent": "sql",
            "sql": None,
            "query_clarification": clarification,
            "query_plan": _region_revenue_plan().model_dump(mode="json"),
        },
    )
    assert updated.pending_query_clarification == clarification
    assert updated.last_sql == "SELECT 1"


def test_merge_workflow_clears_clarification_on_successful_sql() -> None:
    prior = AnalysisState(
        pending_query_clarification={"kind": "pick_metric", "message": "pick"},
        turn_count=1,
    )
    updated = merge_workflow_into_state(
        prior,
        question="use revenue",
        final={
            "answer": "Here are the numbers.",
            "intent": "sql",
            "sql": "SELECT 1",
            "rows": [{"v": 1}],
        },
    )
    assert updated.pending_query_clarification is None
    assert updated.last_sql == "SELECT 1"


def test_format_clarification_answer_lists_choices() -> None:
    text = format_clarification_answer(
        QueryClarification(
            kind="pick_metric",
            message="Which metric?",
            choices=[{"id": "1", "label": "Revenue"}, {"id": "2", "label": "Order Count"}],
        )
    )
    assert "Which metric?" in text
    assert "1. Revenue" in text
    assert "2. Order Count" in text


def test_heuristic_plan_does_not_default_to_first_catalog_metric() -> None:
    from app.agent.query_plan import _heuristic_plan

    plan = _heuristic_plan(
        "Show totals by region",
        _sales_catalog(),
    )
    assert plan is not None
    assert plan.metrics == []


def test_comparison_intent_requires_comparison_spec() -> None:
    plan = QueryPlan(intent="comparison", metrics=["Revenue"], dimensions=["Region"])
    result = validate_query_plan(
        plan,
        question="Compare revenue by region",
        semantic_catalog=_sales_catalog(),
    )
    assert result.ok is False
    assert any("comparison" in err.lower() for err in result.errors)


def test_query_executor_skips_when_sql_missing() -> None:
    from app.agent.workflow import QueryExecutor

    executor = QueryExecutor(schema_cache=FakeSchemaCache(_sales_schema()))
    result = executor.run(
        {
            "tenant_id": str(uuid4()),
            "connection_id": str(uuid4()),
            "sql": None,
        }
    )
    assert result["rows"] == []
    assert result["steps"] == ["execute_query"]


def test_resolve_query_clarification_metric_choice() -> None:
    state = AnalysisState(
        query_plan=QueryPlan(
            intent="aggregation",
            metrics=[],
            dimensions=["Region"],
            time=TimeSpec(range="last_month"),
        ),
        pending_query_clarification={
            "kind": "pick_metric",
            "message": "Which metric?",
            "choices": [
                {"id": "1", "label": "Gross Sales"},
                {"id": "2", "label": "Net Sales"},
            ],
        },
    )
    plan = resolve_query_clarification("2", state)
    assert plan is not None
    assert plan.metrics == ["Net Sales"]


def test_resolve_query_clarification_date_field_by_label() -> None:
    state = AnalysisState(
        query_plan=QueryPlan(
            intent="aggregation",
            metrics=["Revenue"],
            time=TimeSpec(range="last_month"),
        ),
        pending_query_clarification={
            "kind": "pick_date_field",
            "message": "Which date?",
            "choices": [
                {"id": "1", "label": "Order Date"},
                {"id": "2", "label": "Shipment Date"},
            ],
        },
    )
    plan = resolve_query_clarification("Order Date", state)
    assert plan is not None
    assert plan.time is not None
    assert plan.time.field == "Order Date"


def test_sql_generator_applies_clarification_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.query_plan import QueryPlan as QP

    monkeypatch.setattr(
        "app.agent.workflow.parse_query_plan",
        lambda *args, **kwargs: QP(intent="aggregation", metrics=[], dimensions=["Region"]),
    )

    generator = SQLGenerator(
        schema_cache=FakeSchemaCache(_sales_schema()),
        semantic_cache=FakeSemanticCache(_sales_catalog()),
        llm=FakeLLM("SELECT 1"),
    )
    result = generator.run(
        {
            "tenant_id": "tenant-123",
            "connection_id": "connection-123",
            "question": "Gross Sales",
            "analysis_state": AnalysisState(
                query_plan=QP(
                    intent="aggregation",
                    metrics=[],
                    dimensions=["Region"],
                    time=TimeSpec(range="last_month", field="Order Date"),
                ),
                pending_query_clarification={
                    "kind": "pick_metric",
                    "message": "Which metric?",
                    "choices": [
                        {"id": "1", "label": "Gross Sales"},
                        {"id": "2", "label": "Net Sales"},
                    ],
                },
            ).model_dump(mode="json"),
        }
    )

    assert result.get("query_clarification") is None
    assert result["sql"] is not None
    assert "SUM(sales.gross_amount)" in result["sql"]


def test_last_quarter_question_is_aggregation_not_comparison() -> None:
    from app.agent.time_resolution import infer_comparison_type

    question = "What were total sales last quarter?"
    assert infer_comparison_type(question, TimeSpec(range="last_quarter")) is None
    plan = normalize_query_plan(
        QueryPlan(
            intent="aggregation",
            metrics=["Revenue"],
            time=TimeSpec(range="last_quarter"),
        ),
        question=question,
        semantic_catalog=_sales_catalog(),
    )
    assert plan.intent == "aggregation"
    assert plan.comparison is None
    assert plan.time is not None
    assert plan.time.range == "last_quarter"


def test_validate_empty_catalog_still_allows_file_dataset_compile() -> None:
    """Live DB empty catalogs are blocked in SQLGenerator, not validate_query_plan."""
    plan = QueryPlan(
        intent="aggregation",
        metrics=[],
        time=TimeSpec(range="last_quarter"),
    )
    result = validate_query_plan(
        plan,
        question="What were total sales last quarter?",
        semantic_catalog={"metrics": [], "dimensions": []},
    )
    assert result.ok is True


def test_compile_last_quarter_mysql_is_half_open_and_sqlglot_safe() -> None:
    from app.tools.sql_safety import SQLSafetyPipeline
    import sqlglot
    from sqlglot import exp

    catalog = {
        "metrics": [{"name": "Revenue", "expression": "SUM(sales.amount)"}],
        "dimensions": [{"name": "Order Date", "expression": "sales.order_date"}],
        "relationships": [],
    }
    schema = {
        "db_type": "mysql",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "sales",
                "columns": [
                    {"name": "amount", "data_type": "numeric"},
                    {"name": "order_date", "data_type": "date"},
                ],
                "foreign_keys": [],
            }
        ],
    }
    plan = QueryPlan(
        intent="aggregation",
        metrics=["Revenue"],
        time=TimeSpec(field="Order Date", range="last_quarter"),
    )
    sql = compile_query_plan(plan, catalog, schema, "mysql")
    assert "%" not in sql
    assert "INTERVAL 3 MONTH" in sql.upper()
    assert " < " in sql
    assert "ORDER BY 2" not in sql.upper()
    parsed = sqlglot.parse(sql, read="mysql")
    stmts = [s for s in parsed if s is not None]
    assert len(stmts) == 1
    assert isinstance(stmts[0], exp.Select)
    validated = SQLSafetyPipeline(schema=schema, db_type="mysql").validate(sql)
    assert validated.upper().lstrip().startswith("SELECT")
