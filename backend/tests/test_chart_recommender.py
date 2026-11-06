"""Tests for rule-based chart recommendations."""

from app.agent.workflow import ChartRecommender, WorkflowState


def run_recommender(rows: list[dict]) -> dict:
    """Run the chart recommender with test rows."""
    recommender = ChartRecommender()

    state: WorkflowState = {
        "rows": rows,
    }

    return recommender.run(state)


def test_empty_rows_returns_none() -> None:
    """No query results should not produce a chart."""

    result = run_recommender([])

    assert result["chart_suggestion"] is None
    assert result["steps"] == ["recommend_chart"]


def test_single_row_returns_none() -> None:
    """A single summary/KPI row should not produce a chart."""

    result = run_recommender(
        [
            {
                "total_sales": 50000,
            }
        ]
    )

    assert result["chart_suggestion"] is None


def test_few_categories_returns_pie() -> None:
    """A small number of categories reads better as a share-of-total pie."""

    result = run_recommender(
        [
            {"product": "Laptop", "sales": 100},
            {"product": "Mobile", "sales": 200},
            {"product": "Tablet", "sales": 150},
        ]
    )

    assert result["chart_suggestion"] == "pie"


def test_many_categories_returns_bar() -> None:
    """Too many categories for a pie to be readable should suggest a bar chart."""

    result = run_recommender(
        [
            {"product": f"Product {i}", "sales": i * 10}
            for i in range(1, 9)
        ]
    )

    assert result["chart_suggestion"] == "bar"


def test_multiple_measures_by_category_returns_combo() -> None:
    """Two+ real measures sharing one label should compare side by side."""

    result = run_recommender(
        [
            {"product": "Laptop", "revenue": 100, "profit": 20},
            {"product": "Mobile", "revenue": 200, "profit": 50},
            {"product": "Tablet", "revenue": 150, "profit": 30},
        ]
    )

    assert result["chart_suggestion"] == "combo"


def test_two_measures_no_label_returns_scatter() -> None:
    """Two real measures with no categorical label suit a correlation view."""

    result = run_recommender(
        [
            {"revenue": 100, "profit": 20},
            {"revenue": 200, "profit": 50},
            {"revenue": 150, "profit": 30},
        ]
    )

    assert result["chart_suggestion"] == "scatter"


def test_identifier_columns_do_not_count_as_measures() -> None:
    """An id/key column must not be treated as a second measure or a label."""

    result = run_recommender(
        [
            {"customer_id": 1, "product": "Laptop", "sales": 100},
            {"customer_id": 2, "product": "Mobile", "sales": 200},
            {"customer_id": 3, "product": "Tablet", "sales": 150},
        ]
    )

    assert result["chart_suggestion"] == "pie"


def test_time_and_numeric_data_returns_line() -> None:
    """Time-based data with numeric values should suggest a line chart."""

    result = run_recommender(
        [
            {"month": "January", "sales": 100},
            {"month": "February", "sales": 150},
            {"month": "March", "sales": 200},
        ]
    )

    assert result["chart_suggestion"] == "line"


def test_year_column_returns_line() -> None:
    """Year-like columns should also be treated as time series."""

    result = run_recommender(
        [
            {"year": "2023", "sales": 100},
            {"year": "2024", "sales": 150},
        ]
    )

    assert result["chart_suggestion"] == "line"


def test_single_numeric_column_returns_none() -> None:
    """A single measure with no label and no second measure to correlate
    against has nothing meaningful to chart."""

    result = run_recommender(
        [
            {"sales": 100},
            {"sales": 200},
        ]
    )

    assert result["chart_suggestion"] is None


def test_only_text_columns_returns_none() -> None:
    """Rows without numeric data should not produce a chart."""

    result = run_recommender(
        [
            {"product": "Laptop", "category": "Electronics"},
            {"product": "Mobile", "category": "Electronics"},
        ]
    )

    assert result["chart_suggestion"] is None

def _suggest(rows, sql=None):
    state: WorkflowState = {"rows": rows, "sql": sql, "intent": "sql"}  # type: ignore[typeddict-item]
    return ChartRecommender().run(state)["chart_suggestion"]


def test_downtime_column_is_not_a_time_axis():
    rows = [{"plant": f"P{i}", "downtime_hours": 10 + i} for i in range(8)]
    assert _suggest(rows) == "bar"


def test_percentage_breakdown_is_never_a_pie():
    rows = [{"zone": z, "gpm": v} for z, v in [("E", 4.9), ("W", 5.1), ("N", 5.0)]]
    sql = "SELECT zone, 100.0 * SUM(gp) / NULLIF(SUM(s), 0) AS gpm FROM t GROUP BY zone"
    assert _suggest(rows, sql) == "bar"


def test_few_additive_parts_are_a_pie():
    rows = [{"zone": z, "sales": v} for z, v in [("E", 40), ("W", 30), ("N", 30)]]
    assert _suggest(rows, "SELECT zone, SUM(s) AS sales FROM t GROUP BY zone") == "pie"


def test_numeric_year_column_is_time_axis_not_measure():
    rows = [{"year": 2021 + i, "revenue": 100 + i} for i in range(4)]
    assert _suggest(rows) == "line"
