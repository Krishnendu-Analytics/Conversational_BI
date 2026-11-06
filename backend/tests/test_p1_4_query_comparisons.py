"""P1-4: Advanced time comparisons, share-of-total, dashboard clarification."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.agent.analysis_state import AnalysisState
from app.agent.comparison_compiler import compile_dual_period_comparison, compile_share_of_total
from app.agent.comparison_facts import extract_comparison_facts
from app.agent.dashboard_operation_schema import DashboardOperationItem, DashboardOperationPlan, WidgetAddSpec
from app.agent.dashboard_operation_validator import validate_operation_plan
from app.agent.follow_up import resolve_follow_up
from app.agent.query_plan import ComparisonSpec, FilterSpec, QueryPlan, TimeSpec
from app.agent.query_plan_validation import normalize_query_plan, validate_query_plan, verify_sql_reflects_plan
from app.agent.sql_compiler import compile_query_plan
from app.agent.time_resolution import derive_comparison_period, resolve_time_phrase
from app.agent.sql_plan_verification import verify_sql_plan_ast


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
            {"name": "Hospital", "expression": "sales.hospital_name"},
            {"name": "Order Date", "expression": "sales.order_date"},
            {"name": "Shipment Date", "expression": "sales.shipment_date"},
        ],
        "relationships": [],
    }


def _sales_schema() -> dict[str, Any]:
    return {
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "sales",
                "columns": [
                    {"name": "amount", "data_type": "numeric"},
                    {"name": "gross_amount", "data_type": "numeric"},
                    {"name": "order_id", "data_type": "integer"},
                    {"name": "region", "data_type": "text"},
                    {"name": "product_name", "data_type": "text"},
                    {"name": "hospital_name", "data_type": "text"},
                    {"name": "order_date", "data_type": "date"},
                    {"name": "shipment_date", "data_type": "date"},
                ],
                "foreign_keys": [],
            }
        ],
    }


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("today", "today"),
        ("yesterday", "yesterday"),
        ("this week", "this_week"),
        ("last week", "last_week"),
        ("this month", "this_month"),
        ("last month", "last_month"),
        ("this quarter", "this_quarter"),
        ("last quarter", "last_quarter"),
        ("this year", "this_year"),
        ("last year", "last_year"),
        ("month to date", "mtd"),
        ("year to date", "ytd"),
        ("previous period", "previous_period"),
    ],
)
def test_resolve_time_phrases(phrase: str, expected: str) -> None:
    spec = resolve_time_phrase(phrase)
    assert spec is not None
    assert spec.range == expected


def test_mtd_is_not_this_month() -> None:
    assert resolve_time_phrase("month to date").range == "mtd"
    assert resolve_time_phrase("this month").range == "this_month"


def test_derive_previous_period_from_this_month() -> None:
    derived = derive_comparison_period(TimeSpec(range="this_month"))
    assert derived is not None
    assert derived.range == "last_month"


def test_derive_previous_period_from_ytd() -> None:
    derived = derive_comparison_period(TimeSpec(range="ytd"))
    assert derived is not None
    assert derived.range == "last_year"


def test_dual_period_yoy_sql() -> None:
    plan = QueryPlan(
        intent="comparison",
        metrics=["Revenue"],
        dimensions=["Region"],
        time=TimeSpec(field="Order Date", range="ytd"),
        comparison=ComparisonSpec(type="previous_year"),
        filters=[FilterSpec(field="Region", operator="eq", value="Bihar")],
    )
    sql = compile_dual_period_comparison(plan, _sales_catalog(), _sales_schema(), "postgres")
    assert "current_Revenue" in sql
    assert "comparison_Revenue" in sql
    assert "absolute_change_Revenue" in sql
    assert "percent_change_Revenue" in sql
    assert "sales.region = 'Bihar'" in sql
    assert "DATE_TRUNC('year'" in sql
    assert "INTERVAL '1 year'" in sql


def test_share_of_total_sql() -> None:
    plan = QueryPlan(
        intent="share_of_total",
        metrics=["Order Count"],
        dimensions=["Product"],
        comparison=ComparisonSpec(type="share_of_total"),
        filters=[FilterSpec(field="Region", operator="eq", value="North")],
    )
    sql = compile_share_of_total(plan, _sales_catalog(), _sales_schema(), "postgres")
    assert "share_pct" in sql
    assert "OVER ()" in sql
    assert "sales.region = 'North'" in sql


def test_entity_vs_entity_sql() -> None:
    plan = QueryPlan(
        intent="comparison",
        metrics=["Revenue"],
        time=TimeSpec(field="Order Date", range="ytd"),
        comparison=ComparisonSpec(
            type="entity_vs_entity",
            entity_dimension="Hospital",
            entities=["Hospital A", "Hospital B"],
        ),
    )
    sql = compile_query_plan(plan, _sales_catalog(), _sales_schema(), "postgres")
    assert "entity_a_Revenue" in sql
    assert "entity_b_Revenue" in sql


def test_normalize_comparison_from_question() -> None:
    plan = normalize_query_plan(
        QueryPlan(intent="aggregation", metrics=["Revenue"], dimensions=["Region"]),
        question="Compare revenue this year with last year",
        semantic_catalog=_sales_catalog(),
    )
    assert plan.intent == "comparison"
    assert plan.comparison.type == "previous_year"
    assert plan.time.range == "ytd"


def test_follow_up_compare_last_year_dual_period() -> None:
    prior = AnalysisState(
        query_plan=QueryPlan(intent="aggregation", metrics=["Revenue"], dimensions=["Region"]),
        turn_count=1,
    )
    plan, handled = resolve_follow_up("Compare with last year", prior)
    assert handled
    assert plan.comparison.type == "previous_year"
    assert plan.time.range == "ytd"


def test_comparison_facts_from_dual_period_rows() -> None:
    plan = QueryPlan(
        intent="comparison",
        metrics=["Revenue"],
        comparison=ComparisonSpec(type="previous_year"),
    )
    rows = [{"current_Revenue": 120.0, "comparison_Revenue": 100.0, "percent_change_Revenue": 20.0}]
    facts = extract_comparison_facts(rows, query_plan=plan)
    assert facts[0].direction == "up"
    assert facts[0].change_pct == 20.0


def test_ast_verification_dual_period() -> None:
    plan = QueryPlan(
        intent="comparison",
        metrics=["Revenue"],
        time=TimeSpec(field="Order Date", range="ytd"),
        comparison=ComparisonSpec(type="previous_year"),
    )
    sql = compile_dual_period_comparison(plan, _sales_catalog(), _sales_schema(), "postgres")
    assert verify_sql_plan_ast(sql, plan, _sales_catalog(), db_type="postgres").ok


def test_dashboard_ambiguous_metric_clarification() -> None:
    plan = DashboardOperationPlan(
        dashboard_id=uuid4(),
        operations=[
            DashboardOperationItem(
                action="add_widget",
                widget_spec=WidgetAddSpec(widget_type="kpi", title="Show sales"),
            )
        ],
    )
    result = validate_operation_plan(
        plan,
        loaded_dashboard_id=plan.dashboard_id,
        catalog=[],
        semantic_catalog=_sales_catalog(),
    )
    assert result.ok is False
    assert result.clarification.kind == "pick_metric"


def test_dashboard_ambiguous_date_clarification() -> None:
    plan = DashboardOperationPlan(
        dashboard_id=uuid4(),
        operations=[
            DashboardOperationItem(
                action="add_widget",
                widget_spec=WidgetAddSpec(
                    widget_type="chart",
                    metric="Revenue",
                    role="trend",
                    chart="line",
                ),
            )
        ],
    )
    result = validate_operation_plan(
        plan,
        loaded_dashboard_id=plan.dashboard_id,
        catalog=[],
        semantic_catalog=_sales_catalog(),
    )
    assert result.ok is False
    assert result.clarification.kind == "pick_date_field"


def test_compare_this_quarter_with_last_quarter() -> None:
    plan = normalize_query_plan(
        QueryPlan(intent="aggregation", metrics=["Revenue"], dimensions=["Region"]),
        question="Compare sales this quarter with last quarter",
        semantic_catalog=_sales_catalog(),
    )
    assert plan.intent == "comparison"
    assert plan.comparison is not None
    assert plan.comparison.type == "previous_quarter"
