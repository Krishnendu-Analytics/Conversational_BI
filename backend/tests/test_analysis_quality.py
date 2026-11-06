"""Generic analytical quality gate — no domain-specific column names."""

from app.agent.analysis_quality import (
    evaluate_executed_widgets,
    evaluate_plan_analyses,
)
from app.agent.analytical_plan import (
    AnalysisSpec,
    heuristic_analytical_plan,
    parse_llm_analytical_plan,
)
from app.agent.dataset_profiler import profile_dataset

SALES_SCHEMA = {
    "db_type": "postgres",
    "tables": [
        {
            "table_name": "orders",
            "schema_name": "public",
            "estimated_row_count": 1000,
            "columns": [
                {"name": "id", "data_type": "integer", "is_pk": True},
                {"name": "region", "data_type": "varchar", "distinct_count": 4},
                {"name": "customer_name", "data_type": "varchar", "distinct_count": 950},
                {"name": "revenue", "data_type": "numeric"},
                {"name": "order_date", "data_type": "date"},
            ],
        }
    ],
}


def test_count_distinct_grouped_by_itself_rejected() -> None:
    profile = profile_dataset(SALES_SCHEMA, db_type="postgres")
    analyses = [
        AnalysisSpec(
            purpose="entity_count",
            metric="Active Customers",
            metric_expression="COUNT(DISTINCT customer_name)",
            dimension="Customer Name",
            aggregation="count_distinct",
            visualization="bar",
            title="Active Customers by Customer Name",
            id="chart-customer",
        )
    ]
    kept, decisions = evaluate_plan_analyses(analyses, profile, source="openai")
    assert kept == []
    reasons = " ".join(d.reason for d in decisions)
    assert "tautological" in reasons.lower() or "COUNT(DISTINCT D)" in reasons


def test_high_uniqueness_dimension_rejected() -> None:
    profile = profile_dataset(SALES_SCHEMA, db_type="postgres")
    customer = profile.dimension_by_name("Customer Name")
    if customer is None:
        customer = next(
            (item for item in profile.identifiers if item.column.lower() == "customer_name"),
            None,
        )
    assert customer is not None
    assert customer.uniqueness_ratio is not None
    assert customer.uniqueness_ratio >= 0.8

    analyses = [
        AnalysisSpec(
            purpose="breakdown",
            metric="Revenue",
            metric_expression="SUM(revenue)",
            dimension="Customer Name",
            aggregation="sum",
            visualization="bar",
            title="Revenue by Customer Name",
            id="chart-customer",
        )
    ]
    kept, decisions = evaluate_plan_analyses(analyses, profile, source="heuristic")
    assert kept == []
    assert any(d.validation_result == "rejected" and d.uniqueness_ratio is not None for d in decisions)
    assert any("uniqueness" in d.reason.lower() for d in decisions)


def test_two_point_trend_rejected_after_execute() -> None:
    dashboard = {
        "build_mode": "analytical_llm",
        "widgets": [
            {
                "id": "trend-date",
                "type": "chart",
                "chart": "line",
                "title": "Revenue Over Time",
                "purpose": "trend",
                "metric": "Revenue",
                "rows": [
                    {"period": "2024-01", "value": 10},
                    {"period": "2024-02", "value": 12},
                ],
            },
            {"id": "insight", "type": "ai_insight", "title": "AI Insights"},
        ],
    }
    gated = evaluate_executed_widgets(dashboard, source="openai")
    ids = {w["id"] for w in gated["widgets"]}
    assert "trend-date" not in ids
    assert "insight" in ids
    assert any(
        d["validation_result"] == "rejected" and "time bucket" in d["reason"]
        for d in gated["analysis_decisions"]
    )


def test_flat_series_rejected() -> None:
    dashboard = {
        "build_mode": "analytical_heuristic",
        "widgets": [
            {
                "id": "chart-flat",
                "type": "chart",
                "chart": "bar",
                "title": "Measure by Entity",
                "metric": "Revenue",
                "dimension": "Entity",
                "rows": [{"category": f"e{i}", "value": 1} for i in range(15)],
            }
        ],
    }
    gated = evaluate_executed_widgets(dashboard, source="heuristic")
    assert all(w["id"] != "chart-flat" for w in gated["widgets"])
    assert any("variance" in d["reason"].lower() or "max" in d["reason"].lower() for d in gated["analysis_decisions"])


def test_useful_metric_moderate_cardinality_accepted() -> None:
    profile = profile_dataset(SALES_SCHEMA, db_type="postgres")
    region = profile.dimension_by_name("Region")
    assert region is not None
    assert region.uniqueness_ratio is not None
    assert region.uniqueness_ratio < 0.8

    analyses = [
        AnalysisSpec(
            purpose="breakdown",
            metric="Revenue",
            metric_expression="SUM(revenue)",
            dimension="Region",
            aggregation="sum",
            visualization="bar",
            title="Revenue by Region",
            id="chart-region",
        )
    ]
    kept, decisions = evaluate_plan_analyses(analyses, profile, source="openai")
    assert len(kept) == 1
    assert kept[0].dimension == "Region"
    accepted = [d for d in decisions if d.validation_result == "accepted"]
    assert accepted
    assert accepted[0].useful is True
    assert accepted[0].cardinality == 4


def test_decision_log_populated() -> None:
    profile = profile_dataset(SALES_SCHEMA, db_type="postgres")
    plan = heuristic_analytical_plan(profile)
    assert plan.decisions
    for row in plan.decisions:
        assert "analysis_id" in row
        assert "purpose" in row
        assert "metric" in row
        assert "planner_source" in row
        assert row["planner_source"] in {"openai", "heuristic"}
        assert row["validation_result"] in {"proposed", "accepted", "rejected", "demoted"}
        assert "useful" in row
        assert "reason" in row
        assert "unavailable_stats" in row


def test_record_count_breakdown_rejected_when_measure_exists() -> None:
    profile = profile_dataset(SALES_SCHEMA, db_type="postgres")
    analyses = [
        AnalysisSpec(
            purpose="breakdown",
            metric="Record Count",
            metric_expression="COUNT(*)",
            dimension="Region",
            aggregation="count",
            visualization="bar",
            title="Record Count by Region",
            id="chart-records",
        )
    ]
    kept, decisions = evaluate_plan_analyses(analyses, profile, source="openai")
    assert kept == []
    assert any("COUNT(*)" in d.reason for d in decisions)


def test_flexi_like_llm_plan_quality_transcript() -> None:
    """Generic high-uniqueness / tautology / 2-point trend shape (not domain rules)."""
    schema = {
        "db_type": "excel",
        "tables": [
            {
                "table_name": "dataset",
                "estimated_row_count": 20,
                "columns": [
                    {"name": "group_name", "data_type": "varchar", "distinct_count": 5},
                    {"name": "campaign", "data_type": "varchar", "distinct_count": 18},
                    {"name": "site_name", "data_type": "varchar", "distinct_count": 4},
                    {"name": "total_calls", "data_type": "integer"},
                    {"name": "unique_connected_calls", "data_type": "integer"},
                    {"name": "call_date", "data_type": "date"},
                ],
            }
        ],
    }
    profile = profile_dataset(schema, db_type="excel")
    raw = """{
      "title": "Dial Dashboard",
      "analyses": [
        {"purpose": "headline_measure", "metric": "Total Calls", "visualization": "kpi", "title": "Total Calls"},
        {"purpose": "headline_measure", "metric": "Unique Connected Calls", "visualization": "kpi", "title": "Total Unique Connected Calls"},
        {"purpose": "breakdown", "metric": "Record Count", "dimension": "Group Name", "visualization": "bar", "title": "Record Count by Group Name"},
        {"purpose": "entity_count", "metric": "Active Campaigns", "dimension": "Campaign", "visualization": "bar", "title": "Active Campaigns by Campaign"},
        {"purpose": "entity_count", "metric": "Active Campaigns", "dimension": "Site Name", "visualization": "bar", "title": "Active Campaigns by Site Name"},
        {"purpose": "trend", "metric": "Total Calls", "visualization": "line", "grain": "month", "title": "Total Calls Trend Over Time"}
      ]
    }"""
    plan = parse_llm_analytical_plan(raw, profile)
    assert plan is not None
    proposed = [d for d in plan.decisions if d.get("validation_result") == "proposed"]
    accepted = [d for d in plan.decisions if d.get("validation_result") == "accepted"]
    rejected = [d for d in plan.decisions if d.get("validation_result") == "rejected"]
    assert proposed
    titles = {a.title for a in plan.analyses}
    assert "Total Calls" in titles
    assert not any("Record Count by Group Name" == a.title for a in plan.analyses)
    assert not any("by Campaign" in a.title and "Active" in a.title for a in plan.analyses)
    executed = {
        "build_mode": "analytical_llm",
        "analysis_decisions": plan.decisions,
        "widgets": [
            {
                "id": "trend-call-date",
                "type": "chart",
                "chart": "line",
                "title": "Total Calls Trend Over Time",
                "metric": "Total Calls",
                "rows": [{"period": "2024-01", "value": 10}, {"period": "2024-02", "value": 11}],
            }
        ],
    }
    gated = evaluate_executed_widgets(executed, profile, source="openai")
    assert all(w.get("chart") != "line" for w in gated["widgets"] if w.get("type") != "ai_insight")
    assert rejected or any(d["validation_result"] == "rejected" for d in gated["analysis_decisions"])
    assert accepted
