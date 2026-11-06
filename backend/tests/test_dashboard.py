"""Tests for schema-driven dashboard builder."""

from unittest.mock import patch
from uuid import uuid4

from app.agent.dashboard import (
    best_table_match_score,
    build_dashboard,
    build_dashboard_spec,
    dashboard_to_rows,
    edit_dashboard_spec,
    primary_chart_suggestion,
)


AGREEMENT_SCHEMA = {
    "tables": [
        {
            "table_name": "auth_user",
            "schema_name": "public",
            "columns": [
                {"name": "id", "data_type": "integer", "is_pk": True},
                {"name": "username", "data_type": "varchar"},
            ],
        },
        {
            "table_name": "agreement_data",
            "schema_name": "public",
            "columns": [
                {"name": "id", "data_type": "integer", "is_pk": True},
                {"name": "agreement_amount", "data_type": "numeric"},
                {"name": "status", "data_type": "varchar"},
                {"name": "signed_at", "data_type": "timestamp"},
            ],
        },
    ]
}


FLEXI_SCHEMA = {
    "tables": [
        {
            "table_name": "tab_dl_idfc_mtd_allocation_cc_bl_dtl",
            "schema_name": "public",
            "columns": [
                {"name": "client_code", "data_type": "varchar"},
                {"name": "balance_tenure", "data_type": "integer"},
                {"name": "principal_outstanding", "data_type": "numeric"},
                {"name": "allocation_date", "data_type": "date"},
            ],
        },
        {
            "table_name": "tab_dl_idfc_flexi_apr_dtl",
            "schema_name": "public",
            "columns": [
                {"name": "client_code", "data_type": "varchar"},
                {"name": "total_calls", "data_type": "integer"},
                {"name": "talk_time", "data_type": "numeric"},
                {"name": "call_date", "data_type": "date"},
            ],
        },
    ]
}


def test_build_dashboard_spec_picks_business_table_not_auth() -> None:
    spec = build_dashboard_spec(AGREEMENT_SCHEMA, question="create dashboard")

    assert spec["widgets"]
    sql_blob = " ".join(str(w.get("sql") or "") for w in spec["widgets"])
    assert "agreement_data" in sql_blob
    assert "auth_user" not in sql_blob


def test_build_dashboard_spec_includes_kpi_and_charts() -> None:
    spec = build_dashboard_spec(AGREEMENT_SCHEMA, db_type="postgres")
    types = {w["type"] for w in spec["widgets"]}

    assert "kpi" in types
    assert "chart" in types
    assert any(w.get("chart") == "bar" for w in spec["widgets"])
    assert any(w.get("chart") == "line" for w in spec["widgets"])


def test_question_routes_to_matching_table_by_name_tokens() -> None:
    spec = build_dashboard_spec(
        FLEXI_SCHEMA,
        question="give me the flexi apr report",
        focus_text="flexi apr report",
    )

    assert spec["source_table"] == "tab_dl_idfc_flexi_apr_dtl"
    sql_blob = " ".join(str(w.get("sql") or "") for w in spec["widgets"])
    assert "flexi_apr" in sql_blob


def test_best_table_match_score_finds_overlap() -> None:
    score = best_table_match_score("flexi apr report", FLEXI_SCHEMA)
    assert score >= 60


def test_edit_dashboard_add_trend_when_missing() -> None:
    base = {
        "title": "Test",
        "source_table": "agreement_data",
        "widgets": [
            {
                "id": "kpi-rows",
                "type": "kpi",
                "title": "Total Records",
                "sql": "SELECT COUNT(*) AS value FROM public.agreement_data",
            }
        ],
    }
    updated, notes = edit_dashboard_spec(
        base, "add trend chart", AGREEMENT_SCHEMA, "postgres"
    )

    assert any(w.get("id", "").startswith("trend-") for w in updated["widgets"])
    assert notes


def test_edit_dashboard_trend_already_exists_returns_note() -> None:
    base = build_dashboard_spec(AGREEMENT_SCHEMA)
    updated, notes = edit_dashboard_spec(
        base, "add trend chart", AGREEMENT_SCHEMA, "postgres"
    )

    assert any("already" in n.lower() for n in notes)
    assert len(updated["widgets"]) == len(base["widgets"])


def test_edit_dashboard_remove_kpi() -> None:
    base = build_dashboard_spec(AGREEMENT_SCHEMA)
    updated, notes = edit_dashboard_spec(base, "remove kpi", AGREEMENT_SCHEMA, "postgres")

    assert not any(w.get("type") == "kpi" for w in updated["widgets"])
    assert notes


def test_primary_chart_suggestion_prefers_bar_or_line() -> None:
    spec = build_dashboard_spec(AGREEMENT_SCHEMA)
    suggestion = primary_chart_suggestion(spec)

    assert suggestion in {"bar", "line"}


def test_dashboard_to_rows_from_executed_widget() -> None:
    dashboard = {
        "title": "Test",
        "widgets": [
            {
                "id": "chart-status",
                "type": "chart",
                "title": "By Status",
                "chart": "bar",
                "rows": [
                    {"category": "Active", "value": 10},
                    {"category": "Closed", "value": 5},
                ],
            }
        ],
    }

    rows = dashboard_to_rows(dashboard)

    assert rows == [
        {"category": "Active", "value": 10},
        {"category": "Closed", "value": 5},
    ]


@patch("app.agent.dashboard._validate_and_run")
def test_build_dashboard_executes_widgets(mock_run) -> None:
    mock_run.return_value = ([{"value": 42}], "SELECT COUNT(*) AS value FROM public.agreement_data")

    dashboard = build_dashboard(
        AGREEMENT_SCHEMA,
        connection_id="conn-1",
        question="overview",
        db_type="postgres",
        llm=None,
        tenant_id=uuid4(),
    )

    assert dashboard["widgets"]
    assert mock_run.called
    assert dashboard["widgets"][0].get("rows") == [{"value": 42}]


def test_planner_normalizes_valid_widget_sql() -> None:
    from app.agent.dashboard import _normalize_planner_spec

    schema = AGREEMENT_SCHEMA
    raw = {
        "title": "Sales",
        "source_table": "agreement_data",
        "widgets": [
            {
                "id": "kpi-1",
                "type": "kpi",
                "title": "Total",
                "sql": "SELECT COUNT(*) AS value FROM public.agreement_data",
            }
        ],
    }
    spec = _normalize_planner_spec(raw, schema=schema, db_type="postgres")
    assert spec is not None
    assert len(spec["widgets"]) == 1


def test_plan_dashboard_with_llm_uses_semantic(monkeypatch) -> None:
    from app.agent.dashboard import plan_dashboard_with_llm

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            assert "Revenue" in prompt
            assert "SELECT" not in prompt.split("Return ONLY")[-1] or "Do NOT output SQL" in prompt
            return """{
              "title": "Revenue Dashboard",
              "analyses": [
                {
                  "purpose": "headline_measure",
                  "metric": "Revenue",
                  "dimension": null,
                  "aggregation": "sum",
                  "visualization": "kpi",
                  "title": "Revenue"
                }
              ]
            }"""

    semantic = {
        "metrics": [{"name": "Revenue", "expression": "SUM(agreement_amount)"}],
        "dimensions": [],
    }
    spec = plan_dashboard_with_llm(
        schema=AGREEMENT_SCHEMA,
        semantic=semantic,
        question="create dashboard",
        focus_text="revenue",
        db_type="postgres",
        llm=FakeLLM(),
    )
    assert spec is not None
    assert spec["build_mode"] == "semantic_llm"
    assert spec["widgets"][0]["title"] == "Revenue"
    assert "sql" in spec["widgets"][0]
    assert "SUM" in spec["widgets"][0]["sql"].upper()


EXCEL_CALL_SCHEMA = {
    "tables": [
        {
            "table_name": "t_agent_wise",
            "schema_name": "main",
            "columns": [
                {"name": "user", "data_type": "VARCHAR"},
                {"name": "talk_time", "data_type": "VARCHAR"},
                {"name": "idle_time", "data_type": "TIME"},
                {"name": "call_date", "data_type": "DATE"},
                {"name": "total_calls", "data_type": "BIGINT"},
            ],
        }
    ]
}


def test_excel_heuristic_uses_calendar_dates_not_durations() -> None:
    spec = build_dashboard_spec(EXCEL_CALL_SCHEMA, db_type="excel", question="insight dashboard")
    sql_blob = " ".join(str(w.get("sql") or "") for w in spec["widgets"]).lower()

    assert "try_cast" in sql_blob
    assert "call_date" in sql_blob
    assert "date_trunc('month', talk_time)" not in sql_blob
    assert "date_trunc('month', idle_time)" not in sql_blob
    assert 'sum(talk_time)' not in sql_blob
    assert 'sum("talk_time")' not in sql_blob
    assert '"user"' in sql_blob
    assert any(w.get("chart") == "line" for w in spec["widgets"])
    assert any(w.get("chart") == "bar" for w in spec["widgets"])


def test_excel_llm_planner_rejects_invented_and_duration_metrics() -> None:
    from app.agent.dashboard import plan_dashboard_with_llm

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            assert "Do NOT output SQL" in prompt
            assert "Talk Time" in prompt or "talk_time" in prompt.lower()
            return """{
              "title": "Call Dashboard",
              "analyses": [
                {
                  "purpose": "headline_measure",
                  "metric": "Total Calls",
                  "dimension": null,
                  "aggregation": "sum",
                  "visualization": "kpi",
                  "title": "Total Calls"
                },
                {
                  "purpose": "invented",
                  "metric": "Total Revenue",
                  "dimension": null,
                  "aggregation": "sum",
                  "visualization": "kpi",
                  "title": "Total Revenue"
                },
                {
                  "purpose": "bad_duration",
                  "metric": "Talk Time",
                  "grain": "month",
                  "aggregation": "sum",
                  "visualization": "line",
                  "title": "Talk Time Trend"
                },
                {
                  "purpose": "trend",
                  "metric": "Total Calls",
                  "grain": "month",
                  "time_field": "call_date",
                  "aggregation": "sum",
                  "visualization": "line",
                  "title": "Monthly Calls"
                }
              ]
            }"""

    spec = plan_dashboard_with_llm(
        schema=EXCEL_CALL_SCHEMA,
        semantic=None,
        question="create dashboard",
        focus_text="",
        db_type="excel",
        llm=FakeLLM(),
    )
    assert spec is not None
    titles = [w["title"] for w in spec["widgets"]]
    assert "Total Calls" in titles
    assert "Total Revenue" not in titles
    assert "Talk Time Trend" not in titles
    trend = next(w for w in spec["widgets"] if "TRY_CAST" in w.get("sql", "") or w.get("chart") == "line")
    assert "TRY_CAST" in trend["sql"]
    assert "talk_time" not in trend["sql"].lower()

