"""Analytical quality tests using token-poor column names only."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from app.agent.analysis_quality import (
    evaluate_executed_widgets,
    evaluate_plan_analyses,
    attach_canonical_result,
    infer_value_kind,
)
from app.agent.analytical_plan import (
    AnalysisSpec,
    format_profile_for_prompt,
    heuristic_analytical_plan,
    parse_llm_analytical_plan,
)
from app.agent.dashboard import build_dashboard, build_dashboard_spec
from app.agent.dashboard_insight_facts import extract_dashboard_insight_facts
from app.agent.dataset_profiler import profile_dataset


def _table(
    columns: list[dict],
    *,
    row_count: int | None = 1000,
    extra_profiling: dict | None = None,
) -> dict:
    profiling = {"column_stats": {}, "part_whole_pairs": []}
    if extra_profiling:
        profiling.update(extra_profiling)
    for col in columns:
        stats = {}
        if col.get("distinct_count") is not None:
            stats["distinct_count"] = col["distinct_count"]
        if col.get("null_count") is not None:
            stats["null_count"] = col["null_count"]
        if col.get("null_ratio") is not None:
            stats["null_ratio"] = col["null_ratio"]
        if col.get("min") is not None:
            stats["min"] = col["min"]
        if col.get("max") is not None:
            stats["max"] = col["max"]
        if col.get("date_min") is not None:
            stats["date_min"] = col["date_min"]
        if col.get("date_max") is not None:
            stats["date_max"] = col["date_max"]
        if col.get("date_bucket_count") is not None:
            stats["date_bucket_count"] = col["date_bucket_count"]
        if stats:
            profiling["column_stats"][col["name"]] = stats
    return {
        "db_type": "postgres",
        "tables": [
            {
                "table_name": "t_main",
                "schema_name": "public",
                "estimated_row_count": row_count,
                "row_count": row_count,
                "columns": columns,
                "profiling": profiling,
            }
        ],
    }


DATASET_A = _table(
    [
        {"name": "col_a", "data_type": "varchar", "distinct_count": 4, "null_ratio": 0.0},
        {"name": "col_b", "data_type": "integer", "min": 1, "max": 90, "null_ratio": 0.0},
        {"name": "col_e", "data_type": "varchar", "distinct_count": 998, "null_ratio": 0.0},
        {"name": "col_d", "data_type": "date", "distinct_count": 2, "date_min": "2024-01-01", "date_max": "2024-01-02"},
    ],
    row_count=1000,
)

DATASET_A_EXECUTE = _table(
    [
        {"name": "col_a", "data_type": "varchar", "distinct_count": 4},
        {"name": "col_b", "data_type": "integer", "min": 1, "max": 90},
        {"name": "col_e", "data_type": "varchar", "distinct_count": 998},
        {"name": "col_d", "data_type": "date"},
    ],
    row_count=1000,
)

DATASET_B = _table(
    [
        {"name": "col_a", "data_type": "varchar", "distinct_count": 4},
        {"name": "col_b", "data_type": "integer", "min": 1, "max": 90},
        {"name": "col_e", "data_type": "varchar", "distinct_count": 998},
        {
            "name": "col_d",
            "data_type": "date",
            "distinct_count": 400,
            "date_bucket_count": 400,
            "date_min": "2024-01-01",
            "date_max": "2025-02-04",
        },
    ],
    row_count=1000,
)

DATASET_C = _table(
    [
        {"name": "col_a", "data_type": "varchar", "distinct_count": 6},
        {"name": "col_b", "data_type": "numeric", "min": 0, "max": 500},
        {"name": "col_e", "data_type": "varchar", "distinct_count": 990},
    ],
    row_count=1000,
)

DATASET_D = _table(
    [
        {"name": "col_a", "data_type": "varchar", "distinct_count": 3},
        {"name": "col_c", "data_type": "integer", "min": 0, "max": 10},
        {"name": "col_d", "data_type": "integer", "min": 1, "max": 12},
    ],
    row_count=200,
    extra_profiling={"part_whole_pairs": [{"part": "col_c", "whole": "col_d"}]},
)

DATASET_ONE_CAT = _table(
    [
        {"name": "col_a", "data_type": "varchar", "distinct_count": 1},
        {"name": "col_b", "data_type": "integer", "min": 1, "max": 9},
    ],
    row_count=80,
)

DATASET_UNIQUE_ONLY = _table(
    [
        {"name": "col_e", "data_type": "varchar", "distinct_count": 500},
        {"name": "col_f", "data_type": "varchar", "distinct_count": 498},
        {"name": "col_b", "data_type": "integer", "min": 1, "max": 3},
    ],
    row_count=500,
)


def test_high_uniqueness_identifier_not_used_as_dimension() -> None:
    profile = profile_dataset(DATASET_C, db_type="postgres")
    ident_cols = {f.column.lower() for f in profile.identifiers}
    assert "col_e" in ident_cols
    assert profile.dimension_by_name("Col E") is None
    dim_cols = {d.column.lower() for d in profile.dimensions}
    assert "col_a" in dim_cols
    plan = heuristic_analytical_plan(profile)
    assert not any(
        (a.dimension or "").lower() in {"col e", "col_e"} for a in plan.analyses
    )


def test_count_distinct_grouped_by_self_rejected() -> None:
    profile = profile_dataset(DATASET_A, db_type="postgres")
    analyses = [
        AnalysisSpec(
            purpose="entity_count",
            metric="Record Count",
            metric_expression="COUNT(DISTINCT col_a)",
            dimension="Col A",
            aggregation="count_distinct",
            visualization="bar",
            title="Distinct Col A by Col A",
            id="chart-tauto",
        )
    ]
    kept, decisions = evaluate_plan_analyses(analyses, profile, source="openai")
    assert kept == []
    assert any("tautological" in d.reason.lower() or "COUNT(DISTINCT D)" in d.reason for d in decisions)


def test_two_point_series_rejected_as_trend() -> None:
    profile = profile_dataset(DATASET_A, db_type="postgres")
    date = profile.dates[0]
    assert date.distinct_count == 2 or date.cardinality == 2
    analyses = [
        AnalysisSpec(
            purpose="trend",
            metric="Col B",
            metric_expression="SUM(col_b)",
            grain="month",
            time_field="col_d",
            aggregation="sum",
            visualization="line",
            title="Col B Over Time",
            id="trend-d",
        )
    ]
    # Plan-time: bucket count unknown on this date field in DATASET_A (no date_bucket_count).
    # Result-time must still drop a 2-point line.
    gated = evaluate_executed_widgets(
        {
            "build_mode": "analytical_llm",
            "widgets": [
                {
                    "id": "trend-d",
                    "type": "chart",
                    "chart": "line",
                    "title": "Col B Over Time",
                    "metric": "Col B",
                    "rows": [
                        {"period": "2024-01", "value": 10},
                        {"period": "2024-02", "value": 12},
                    ],
                }
            ],
        },
        profile,
        source="openai",
    )
    assert all(w.get("id") != "trend-d" for w in gated["widgets"])
    assert any("time bucket" in d["reason"] for d in gated["analysis_decisions"])


def test_two_point_date_field_rejected_when_bucket_count_known() -> None:
    schema = _table(
        [
            {"name": "col_a", "data_type": "varchar", "distinct_count": 4},
            {"name": "col_b", "data_type": "integer"},
            {"name": "col_d", "data_type": "date", "date_bucket_count": 2, "distinct_count": 2},
        ]
    )
    profile = profile_dataset(schema, db_type="postgres")
    analyses = [
        AnalysisSpec(
            purpose="trend",
            metric="Col B",
            grain="day",
            time_field="col_d",
            visualization="line",
            title="Trend",
            id="trend-d",
        )
    ]
    kept, decisions = evaluate_plan_analyses(analyses, profile, source="openai")
    assert kept == []
    assert any("date bucket" in d.reason.lower() or "time series" in d.reason.lower() for d in decisions)


def test_400_point_time_series_is_valid_trend_candidate() -> None:
    profile = profile_dataset(DATASET_B, db_type="postgres")
    date = next(d for d in profile.dates if d.column == "col_d")
    assert date.date_bucket_count == 400
    analyses = [
        AnalysisSpec(
            purpose="trend",
            metric="Col B",
            grain="day",
            time_field="col_d",
            visualization="line",
            title="Trend",
            id="trend-d",
        )
    ]
    kept, decisions = evaluate_plan_analyses(analyses, profile, source="openai")
    assert len(kept) == 1
    assert kept[0].visualization == "line"
    assert any(d.validation_result == "accepted" for d in decisions)


def test_one_category_breakdown_rejected() -> None:
    profile = profile_dataset(DATASET_ONE_CAT, db_type="postgres")
    analyses = [
        AnalysisSpec(
            purpose="breakdown",
            metric="Col B",
            dimension="Col A",
            visualization="bar",
            title="Col B by Col A",
            id="chart-a",
        )
    ]
    kept, _ = evaluate_plan_analyses(analyses, profile, source="heuristic")
    assert kept == []
    gated = evaluate_executed_widgets(
        {
            "build_mode": "analytical_heuristic",
            "widgets": [
                {
                    "id": "chart-a",
                    "type": "chart",
                    "chart": "bar",
                    "metric": "Col B",
                    "dimension": "Col A",
                    "rows": [{"category": "only", "value": 12}],
                }
            ],
        },
        source="heuristic",
    )
    assert all(w.get("id") != "chart-a" for w in gated["widgets"])


def test_flat_series_rejected() -> None:
    gated = evaluate_executed_widgets(
        {
            "build_mode": "analytical_llm",
            "widgets": [
                {
                    "id": "chart-flat",
                    "type": "chart",
                    "chart": "bar",
                    "metric": "Col B",
                    "dimension": "Col A",
                    "rows": [{"category": f"v{i}", "value": 10} for i in range(4)],
                }
            ],
        },
        source="openai",
    )
    assert all(w.get("id") != "chart-flat" for w in gated["widgets"])


def test_useful_metric_by_moderate_dimension_accepted() -> None:
    profile = profile_dataset(DATASET_A, db_type="postgres")
    dim = profile.dimension_by_name("Col A")
    assert dim is not None
    assert dim.uniqueness_ratio is not None
    assert dim.uniqueness_ratio < 0.8
    assert dim.distinct_count == 4
    analyses = [
        AnalysisSpec(
            purpose="breakdown",
            metric="Col B",
            dimension="Col A",
            visualization="bar",
            title="Col B by Col A",
            id="chart-a",
        )
    ]
    kept, decisions = evaluate_plan_analyses(analyses, profile, source="openai")
    assert len(kept) == 1
    assert any(d.validation_result == "accepted" and d.useful for d in decisions)


def test_profile_statistics_are_available_to_planner() -> None:
    profile = profile_dataset(DATASET_A, db_type="postgres")
    catalog = format_profile_for_prompt(profile)
    blob = " ".join(catalog.values())
    assert "rows = 1000" in catalog["dataset_facts"]
    assert "distinct_count = 4" in catalog["candidate_dimensions"]
    assert "uniqueness_ratio" in catalog["candidate_dimensions"]
    assert "identifier-like" in catalog["identifier_fields"]
    assert "col_e" in catalog["identifier_fields"].lower()
    assert "date_min" in catalog["candidate_dates"]
    assert "col_b" in blob.lower()


def test_planner_cannot_invent_columns() -> None:
    profile = profile_dataset(DATASET_A, db_type="postgres")
    raw = """{
      "title": "Bad",
      "analyses": [
        {"purpose": "fake", "metric": "Not A Column", "visualization": "kpi", "title": "Fake"},
        {"purpose": "ok", "metric": "Col B", "visualization": "kpi", "title": "Col B"}
      ]
    }"""
    plan = parse_llm_analytical_plan(raw, profile)
    assert plan is not None
    metrics = {a.metric.lower() for a in plan.analyses}
    assert "not a column" not in metrics
    assert "col b" in metrics


@patch("app.agent.dashboard._validate_and_run")
def test_result_evaluator_runs_on_live_dashboard_path(mock_run) -> None:
    def fake_run(_cid, _schema, _db_type, sql, **_kwargs):
        text = sql.upper()
        if " AS PERIOD" in text or "DATE_TRUNC" in text:
            return (
                [{"period": "2024-01", "value": 10}, {"period": "2024-02", "value": 11}],
                sql,
            )
        if "GROUP BY" in text:
            return (
                [
                    {"category": "w", "value": 10},
                    {"category": "x", "value": 40},
                    {"category": "y", "value": 25},
                    {"category": "z", "value": 7},
                ],
                sql,
            )
        return ([{"value": 100}], sql)

    mock_run.side_effect = fake_run
    dashboard = build_dashboard(
        DATASET_A_EXECUTE,
        connection_id="conn-1",
        question="overview",
        db_type="postgres",
        llm=None,
        tenant_id=uuid4(),
    )
    decisions = dashboard.get("analysis_decisions") or []
    assert any(d.get("stage") == "result" for d in decisions)
    assert not any(w.get("chart") == "line" for w in dashboard.get("widgets") or [])
    assert any(
        d.get("stage") == "result" and "time bucket" in (d.get("reason") or "")
        for d in decisions
    )


def test_naive_max_min_kpi_ratio_not_generated() -> None:
    facts = extract_dashboard_insight_facts(
        {
            "widgets": [
                {
                    "id": "k1",
                    "type": "kpi",
                    "title": "Col B",
                    "value": 110,
                    "rows": [{"value": 110}],
                    "quality": {"metric_column": "col_b"},
                },
                {
                    "id": "k2",
                    "type": "kpi",
                    "title": "Col C",
                    "value": 100,
                    "rows": [{"value": 100}],
                    "quality": {"metric_column": "col_c"},
                },
            ]
        }
    )
    assert facts.comparisons == []
    text = " ".join(facts.ranked_highlights)
    assert "1.10x" not in text
    assert " vs " not in text


def test_legitimate_part_whole_ratio_is_percentage() -> None:
    facts = extract_dashboard_insight_facts(
        {
            "part_whole_pairs": [{"part": "col_c", "whole": "col_d"}],
            "widgets": [
                {
                    "id": "k1",
                    "type": "kpi",
                    "title": "Col C",
                    "value": 91.1,
                    "rows": [{"value": 91.1}],
                    "quality": {"metric_column": "col_c", "part_of": "col_d"},
                },
                {
                    "id": "k2",
                    "type": "kpi",
                    "title": "Col D",
                    "value": 100,
                    "rows": [{"value": 100}],
                    "quality": {"metric_column": "col_d"},
                },
            ]
        }
    )
    assert facts.comparisons
    assert facts.comparisons[0].rate_pct == 91.1
    assert "91.1%" in " ".join(facts.ranked_highlights)


def test_failed_ai_plan_does_not_emit_count_star_template() -> None:
    class FailLLM:
        def complete(self, _prompt: str) -> str:
            return "not-json"

    spec = build_dashboard_spec(
        DATASET_A,
        db_type="postgres",
        question="create dashboard",
        llm=FailLLM(),
    )
    assert spec.get("build_mode") == "analytical_heuristic_fallback"
    assert spec.get("planner_fallback") is True
    ids = {w.get("id") for w in spec.get("widgets") or []}
    assert "kpi-rows" not in ids
    sql_blob = " ".join(str(w.get("sql") or "") for w in spec.get("widgets") or []).upper()
    assert "COL_B" in sql_blob or "col_b" in sql_blob.lower()


def test_unique_columns_do_not_become_bar_dimensions() -> None:
    spec = build_dashboard_spec(DATASET_UNIQUE_ONLY, db_type="postgres")
    sql_blob = " ".join(str(w.get("sql") or "") for w in spec.get("widgets") or []).lower()
    assert "col_e" not in sql_blob or "count(distinct" in sql_blob
    for widget in spec.get("widgets") or []:
        if widget.get("chart") in {"bar", "pie"}:
            assert widget.get("dimension") not in {"Col E", "Col F"}
            dim_sql = str(widget.get("sql") or "").lower()
            assert "col_e" not in dim_sql
            assert "col_f" not in dim_sql


def test_part_whole_profile_exposes_derived_rate() -> None:
    profile = profile_dataset(DATASET_D, db_type="postgres")
    assert any(p.part.lower() == "col_c" and p.whole.lower() == "col_d" for p in profile.part_whole_pairs)
    names = [m.name.lower() for m in profile.derived_metrics]
    assert any("col c" in n and "col d" in n for n in names)


def test_stats_parser_skips_high_uniqueness_integer_pairs() -> None:
    from app.schema_profiling import parse_table_stats_row

    alias_map = {
        "columns": [
            {"index": 0, "name": "col_c", "data_type": "integer"},
            {"index": 1, "name": "col_d", "data_type": "integer"},
            {"index": 2, "name": "col_e", "data_type": "integer"},
        ],
        "pairs": [
            {"a_index": 0, "a_name": "col_c", "b_index": 1, "b_name": "col_d"},
            {"a_index": 0, "a_name": "col_c", "b_index": 2, "b_name": "col_e"},
        ],
    }
    row = {
        "__row_count": 200,
        "d_0": 8,
        "d_1": 10,
        "d_2": 190,
        "n_0": 0,
        "n_1": 0,
        "n_2": 0,
        "min_0": 0,
        "max_0": 9,
        "min_1": 1,
        "max_1": 12,
        "min_2": 1000,
        "max_2": 50000,
        "sum_0": 80,
        "sum_1": 100,
        "sum_2": 900000,
        "pw_0_1": 0,
        "pw_1_0": 40,
        "pw_0_2": 0,
        "pw_2_0": 200,
    }
    parsed = parse_table_stats_row(row, alias_map)
    assert parsed["estimated_row_count"] == 200
    assert parsed["column_stats"]["col_c"]["distinct_count"] == 8
    pairs = {(p["part"], p["whole"]) for p in parsed["part_whole_pairs"]}
    assert ("col_c", "col_d") in pairs
    assert ("col_c", "col_e") not in pairs
    assert ("col_e", "col_c") not in pairs


DATASET_MEASURE_LIKE = _table(
    [
        {"name": "col_a", "data_type": "varchar", "distinct_count": 4},
        {"name": "col_b", "data_type": "integer", "min": 1, "max": 90},
        {"name": "col_c", "data_type": "varchar", "distinct_count": 12},
        {"name": "date_x", "data_type": "date", "date_bucket_count": 40},
        {"name": "value_y", "data_type": "numeric", "min": 1, "max": 500},
    ],
    row_count=71,
    extra_profiling={
        "column_samples": {
            "col_a": ["w", "x", "y", "z"],
            "col_c": ["6.33%", "1.26%", "0.96%", "2.10%", "0.45%"],
        }
    },
)


def test_percent_like_samples_are_not_grouping_dimensions() -> None:
    profile = profile_dataset(DATASET_MEASURE_LIKE, db_type="postgres")
    dim_cols = {d.column.lower() for d in profile.dimensions}
    assert "col_c" not in dim_cols
    assert "col_a" in dim_cols
    plan = heuristic_analytical_plan(profile)
    assert not any(
        (a.dimension or "").lower() in {"col c", "col_c"} for a in plan.analyses
    )


def test_percent_like_result_labels_rejected() -> None:
    gated = evaluate_executed_widgets(
        {
            "build_mode": "analytical_llm",
            "widgets": [
                {
                    "id": "chart-pct",
                    "type": "chart",
                    "chart": "bar",
                    "title": "Value Y by Col C",
                    "metric": "Value Y",
                    "dimension": "Col C",
                    "aggregation": "sum",
                    "value_kind": "count",
                    "quality": {"metric_kind": "count"},
                    "rows": [
                        {"category": "6.33%", "value": 51},
                        {"category": "1.26%", "value": 2},
                        {"category": "0.96%", "value": 1},
                        {"category": "2.10%", "value": 1},
                    ],
                }
            ],
        },
        source="openai",
    )
    assert all(w.get("id") != "chart-pct" for w in gated["widgets"])
    reasons = " ".join(d["reason"] for d in gated["analysis_decisions"])
    assert "measure-like" in reasons.lower() or "meaningful share" in reasons.lower()


def test_canonical_result_keeps_count_not_percent() -> None:
    widget = {
        "id": "chart-a",
        "type": "chart",
        "chart": "bar",
        "aggregation": "sum",
        "quality": {"metric_kind": "count"},
        "rows": [
            {"category": "w", "value": 51},
            {"category": "x", "value": 12},
            {"category": "y", "value": 8},
        ],
    }
    attach_canonical_result(widget)
    assert widget["value_kind"] == "count"
    assert widget["result"]["value_kind"] == "count"
    assert widget["result"]["is_percentage"] is False
    points = {p["label"]: p for p in widget["result"]["points"]}
    assert points["w"]["raw_value"] == 51
    assert abs(points["w"]["share_of_series"] - 51 / 71) < 1e-6
    facts = extract_dashboard_insight_facts({"widgets": [widget]})
    text = " ".join(facts.ranked_highlights)
    assert "51" in text
    assert "6.33%" not in text
    assert "top contributor is" not in text
    assert facts.categories[0].value_kind == "count"


def test_counts_are_not_value_kind_percentage() -> None:
    assert infer_value_kind(metric_kind="count", aggregation="sum") == "count"
    assert infer_value_kind(metric_kind="additive", aggregation="sum") == "amount"
    assert infer_value_kind(metric_kind="rate", aggregation="avg") == "rate"
    pie = {
        "id": "pie-count",
        "type": "pie",
        "chart": "pie",
        "aggregation": "sum",
        "quality": {"metric_kind": "count"},
        "rows": [
            {"category": "w", "value": 40},
            {"category": "x", "value": 30},
            {"category": "y", "value": 20},
        ],
    }
    gated = evaluate_executed_widgets(
        {"build_mode": "analytical_llm", "widgets": [pie]},
        source="openai",
    )
    kept = [w for w in gated["widgets"] if w.get("id") == "pie-count"]
    assert kept
    assert kept[0]["chart"] == "bar"
    assert kept[0]["value_kind"] == "count"
    assert kept[0]["result"]["points"][0]["raw_value"] in {40, 30, 20}


def test_one_dominant_category_long_tail_rejected() -> None:
    gated = evaluate_executed_widgets(
        {
            "build_mode": "analytical_heuristic",
            "widgets": [
                {
                    "id": "chart-tail",
                    "type": "chart",
                    "chart": "bar",
                    "aggregation": "sum",
                    "quality": {"metric_kind": "count"},
                    "rows": [
                        {"category": "w", "value": 51},
                        {"category": "x", "value": 2},
                        {"category": "y", "value": 1},
                        {"category": "z", "value": 1},
                    ],
                }
            ],
        },
        source="heuristic",
    )
    assert all(w.get("id") != "chart-tail" for w in gated["widgets"])


def test_meaningless_names_still_plan_from_stats() -> None:
    profile = profile_dataset(DATASET_A, db_type="postgres")
    plan = heuristic_analytical_plan(profile)
    assert plan.analyses
    metrics = {a.metric.lower() for a in plan.analyses}
    dims = {(a.dimension or "").lower() for a in plan.analyses}
    assert any("col b" in m for m in metrics)
    assert "col e" not in dims
    assert "col_e" not in dims


def test_build_does_not_merge_unrelated_widgets_when_thin() -> None:
    from unittest.mock import patch

    from app.agent.dashboard import build_dashboard

    unique_only = _table(
        [
            {"name": "col_e", "data_type": "varchar", "distinct_count": 70},
            {"name": "col_f", "data_type": "varchar", "distinct_count": 68},
        ],
        row_count=70,
    )

    def fake_run(_cid, _schema, _db_type, sql, **_kwargs):
        return ([{"value": 70}], sql)

    with patch("app.agent.dashboard._validate_and_run", side_effect=fake_run):
        dashboard = build_dashboard(
            unique_only,
            connection_id="conn-1",
            question="overview",
            db_type="postgres",
            llm=None,
            tenant_id=uuid4(),
        )
    ids = {w.get("id") for w in dashboard.get("widgets") or []}
    assert "kpi-rows" not in ids
    for widget in dashboard.get("widgets") or []:
        if widget.get("chart") in {"bar", "pie"}:
            dim_sql = str(widget.get("sql") or "").lower()
            assert "col_e" not in dim_sql
            assert "col_f" not in dim_sql


def test_non_meaningful_category_does_not_get_insight() -> None:
    facts = extract_dashboard_insight_facts(
        {
            "widgets": [
                {
                    "id": "k1",
                    "type": "kpi",
                    "title": "Value Y",
                    "value": 71,
                    "rows": [{"value": 71}],
                    "result": {"value_kind": "count", "raw_value": 71},
                },
                {
                    "id": "c1",
                    "type": "chart",
                    "chart": "bar",
                    "title": "Value Y by Col C",
                    "rows": [
                        {"category": "6.33%", "value": 51},
                        {"category": "1.26%", "value": 2},
                    ],
                    "result": {
                        "value_kind": "count",
                        "labels_measure_like": True,
                        "useful_point_count": 1,
                        "series_total": 53,
                        "points": [
                            {"label": "6.33%", "raw_value": 51, "share_of_series": 0.962},
                            {"label": "1.26%", "raw_value": 2, "share_of_series": 0.038},
                        ],
                    },
                },
            ]
        }
    )
    assert facts.categories == []
    text = " ".join(facts.ranked_highlights)
    assert "6.33%" not in text
    assert "71" in text
