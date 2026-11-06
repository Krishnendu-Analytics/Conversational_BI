"""File/DuckDB dashboard SQL helpers."""

from app.agent.file_dashboard_sql import (
    adapt_file_dashboard_spec,
    duckdb_user_message,
    is_calendar_date_column,
    looks_like_duration_name,
    wrap_date_trunc_for_duckdb,
)
from app.sql_dialect import compiler_db_type
from app.tools.sql_safety import SQLSafetyPipeline


EXCEL_SCHEMA = {
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


def test_compiler_maps_files_to_postgres_time_sql() -> None:
    assert compiler_db_type("csv") == "postgres"
    assert compiler_db_type("excel") == "postgres"
    assert compiler_db_type("duckdb") == "postgres"


def test_duration_names_are_not_calendar_dates() -> None:
    assert looks_like_duration_name("talk_time")
    assert looks_like_duration_name("idle_time")
    assert not looks_like_duration_name("call_date")
    assert not looks_like_duration_name("created_time")
    assert not is_calendar_date_column({"name": "talk_time", "data_type": "VARCHAR"})
    assert not is_calendar_date_column({"name": "idle_time", "data_type": "TIME"})
    assert is_calendar_date_column({"name": "call_date", "data_type": "DATE"})
    assert is_calendar_date_column({"name": "signed_at", "data_type": "TIMESTAMP"})


def test_wrap_date_trunc_is_idempotent() -> None:
    sql = "SELECT DATE_TRUNC('month', call_date) AS period FROM main.t_agent_wise"
    wrapped = wrap_date_trunc_for_duckdb(sql)
    assert "TRY_CAST(call_date AS TIMESTAMP)" in wrapped
    assert wrap_date_trunc_for_duckdb(wrapped) == wrapped


def test_adapt_drops_duration_widgets_and_dedupes_count() -> None:
    spec = {
        "title": "Calls",
        "widgets": [
            {
                "id": "kpi-calls",
                "type": "kpi",
                "title": "Total Calls",
                "sql": "SELECT COUNT(*) AS value FROM main.t_agent_wise",
            },
            {
                "id": "kpi-revenue",
                "type": "kpi",
                "title": "Total Revenue",
                "sql": "SELECT COUNT(*) AS value FROM main.t_agent_wise",
            },
            {
                "id": "trend-idle",
                "type": "chart",
                "title": "Idle Time Trend",
                "sql": (
                    "SELECT DATE_TRUNC('month', idle_time) AS period, "
                    "COUNT(*) AS value FROM main.t_agent_wise "
                    "GROUP BY 1"
                ),
                "chart": "line",
            },
            {
                "id": "kpi-talk",
                "type": "kpi",
                "title": "Talk Time",
                "sql": "SELECT SUM(talk_time) AS value FROM main.t_agent_wise",
            },
            {
                "id": "trend-date",
                "type": "chart",
                "title": "By Month",
                "sql": (
                    "SELECT DATE_TRUNC('month', call_date) AS period, "
                    "COUNT(*) AS value FROM main.t_agent_wise GROUP BY 1"
                ),
                "chart": "line",
            },
        ],
    }
    adapted = adapt_file_dashboard_spec(spec, EXCEL_SCHEMA)
    assert adapted is not None
    titles = [w["title"] for w in adapted["widgets"]]
    assert titles.count("Total Calls") == 1
    assert "Total Revenue" not in titles
    assert "Idle Time Trend" not in titles
    assert "Talk Time" not in titles
    trend_sql = next(w["sql"] for w in adapted["widgets"] if w["id"] == "trend-date")
    assert "TRY_CAST(call_date AS TIMESTAMP)" in trend_sql


def test_safety_pipeline_accepts_duckdb_try_cast() -> None:
    sql = (
        "SELECT DATE_TRUNC('month', TRY_CAST(call_date AS TIMESTAMP)) AS period, "
        "COUNT(*) AS value FROM main.t_agent_wise "
        "GROUP BY DATE_TRUNC('month', TRY_CAST(call_date AS TIMESTAMP)) "
        "ORDER BY period LIMIT 24"
    )
    safe = SQLSafetyPipeline(schema=EXCEL_SCHEMA, db_type="excel").validate(sql)
    assert "TRY_CAST" in safe


def test_duckdb_conversion_error_is_specific() -> None:
    message = duckdb_user_message(
        Exception("Conversion Error: Could not convert string '00:01:23'")
    )
    assert "Duration" in message or "convert" in message.lower()
    generic = duckdb_user_message(Exception("Binder Error: Referenced column \"foo\" not found"))
    assert "column" in generic.lower()
