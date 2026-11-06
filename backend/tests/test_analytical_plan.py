"""Analytical planner compiles profiler candidates without LLM-authored SQL."""

from app.agent.analytical_plan import (
    compile_analytical_plan,
    heuristic_analytical_plan,
    parse_llm_analytical_plan,
    prune_invalid_widgets,
    widget_result_is_valid,
)
from app.agent.dashboard import build_dashboard_spec
from app.agent.dataset_profiler import profile_dataset
from tests.test_dataset_profiler import CALL_CENTER_SCHEMA, MARKETING_SCHEMA, SALES_SCHEMA


def _sql_blob(spec: dict) -> str:
    return " ".join(str(w.get("sql") or "") for w in spec.get("widgets") or []).lower()


def test_call_center_dashboard_has_multiple_compiled_widgets() -> None:
    spec = build_dashboard_spec(
        CALL_CENTER_SCHEMA,
        db_type="excel",
        question="Create a dashboard from this dataset.",
    )
    types = {w["type"] for w in spec["widgets"]}
    sql = _sql_blob(spec)

    assert len(spec["widgets"]) >= 3
    assert "kpi" in types
    assert any(w.get("chart") in {"bar", "pie"} or w.get("type") in {"chart", "pie"} for w in spec["widgets"])
    assert "total_calls" in sql or "sum(" in sql
    assert "avg(" in sql and "talk_pct" in sql
    assert 'sum("talk_time")' not in sql
    assert "sum(talk_time)" not in sql
    assert spec.get("analytical_plan")
    assert all("SELECT" in str(w.get("sql") or "").upper() for w in spec["widgets"] if w.get("type") != "ai_insight")


def test_call_center_does_not_sum_rates() -> None:
    spec = build_dashboard_spec(CALL_CENTER_SCHEMA, db_type="excel")
    for widget in spec["widgets"]:
        sql = str(widget.get("sql") or "").lower()
        if "talk_pct" in sql:
            assert "avg(" in sql
            assert "sum(" not in sql.replace(" ", "").lower()


def test_sales_dashboard_covers_kpis_breakdown_and_trend() -> None:
    spec = build_dashboard_spec(
        SALES_SCHEMA,
        db_type="postgres",
        question="Create a dashboard from this dataset.",
    )
    sql = _sql_blob(spec)
    charts = [w.get("chart") for w in spec["widgets"]]

    assert len(spec["widgets"]) >= 3
    assert any(w.get("type") == "kpi" for w in spec["widgets"])
    assert "bar" in charts
    assert "line" in charts
    assert "revenue" in sql
    assert "order_date" in sql
    assert "date_trunc" in sql
    assert any(
        token in sql for token in ("region", "customer_name", "product_name")
    )


def test_marketing_dashboard_includes_derived_or_volume_widgets() -> None:
    spec = build_dashboard_spec(
        MARKETING_SCHEMA,
        db_type="postgres",
        question="Create a dashboard from this dataset.",
    )
    sql = _sql_blob(spec)
    titles = " ".join(w.get("title") or "" for w in spec["widgets"]).lower()

    assert len(spec["widgets"]) >= 3
    assert "impressions" in sql or "clicks" in sql or "spend" in sql
    assert "campaign" in sql
    assert "date" in sql
    assert any(w.get("chart") == "line" for w in spec["widgets"])
    assert "click-through" in titles or "clicks" in sql


def test_llm_plan_cannot_invent_columns() -> None:
    profile = profile_dataset(SALES_SCHEMA, db_type="postgres")
    raw = """{
      "title": "Bad",
      "analyses": [
        {"purpose": "fake", "metric": "ACHT", "visualization": "kpi", "title": "ACHT"},
        {"purpose": "ok", "metric": "Revenue", "visualization": "kpi", "title": "Revenue"}
      ]
    }"""
    plan = parse_llm_analytical_plan(raw, profile)
    assert plan is not None
    metrics = {a.metric.lower() for a in plan.analyses}
    assert "acht" not in metrics
    assert "revenue" in metrics


def test_heuristic_plan_has_no_sql() -> None:
    profile = profile_dataset(SALES_SCHEMA, db_type="postgres")
    plan = heuristic_analytical_plan(profile)
    dumped = plan.model_dump()
    assert "sql" not in dumped
    assert all("sql" not in a for a in dumped["analyses"])
    assert len(plan.analyses) >= 2


def test_compile_uses_compiler_not_raw_select_from_plan() -> None:
    profile = profile_dataset(SALES_SCHEMA, db_type="postgres")
    plan = heuristic_analytical_plan(profile)
    spec = compile_analytical_plan(
        plan,
        profile=profile,
        schema=SALES_SCHEMA,
        db_type="postgres",
    )
    assert spec["widgets"]
    for widget in spec["widgets"]:
        assert widget["sql"].upper().startswith("SELECT")
        assert "FROM" in widget["sql"].upper()


def test_prune_drops_empty_and_error_widgets() -> None:
    dashboard = {
        "widgets": [
            {"id": "ok", "type": "kpi", "value": 10, "rows": [{"value": 10}]},
            {"id": "err", "type": "kpi", "error": "failed", "rows": []},
            {"id": "empty", "type": "chart", "chart": "bar", "rows": []},
            {"id": "nulls", "type": "kpi", "rows": [{"value": None}]},
            {"id": "insight", "type": "ai_insight", "content": "hi"},
        ]
    }
    pruned = prune_invalid_widgets(dashboard)
    ids = {w["id"] for w in pruned["widgets"]}
    assert ids == {"ok", "insight"}
    assert widget_result_is_valid({"type": "kpi", "value": 3})
    assert not widget_result_is_valid({"type": "chart", "chart": "bar", "rows": []})
