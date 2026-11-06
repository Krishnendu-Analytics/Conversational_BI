"""Measure-kind classification from SQL structure, semantic layer and names."""

from __future__ import annotations

import pytest

from app.agent.measure_kind import classify_sql_columns, resolve_column_kinds

MULTI_JOIN_SQL = """
SELECT z.zone, s.state,
       SUM(f.sales_value) AS sales_value,
       SUM(f.sales_value) - SUM(f.sales_cost) AS gross_profit,
       ROUND(100.0 * SUM(f.gross_profit) / NULLIF(SUM(f.sales_value), 0), 2) AS gpm,
       SUM(f.sales_value) / SUM(SUM(f.sales_value)) OVER () AS contrib,
       AVG(f.unit_price) AS p,
       COUNT(DISTINCT f.customer_id) AS customers,
       MAX(f.sales_value) AS best_sale,
       SUM(f.sales_value) / 1000 AS sales_k
FROM fact_sales f
JOIN dim_state s ON s.id = f.state_id
JOIN dim_zone z ON z.id = s.zone_id
GROUP BY z.zone, s.state
"""


@pytest.mark.parametrize(
    ("column", "kind"),
    [
        ("sales_value", "amount"),
        ("gross_profit", "amount"),
        ("gpm", "percentage"),
        ("contrib", "ratio"),
        ("p", "average"),
        ("customers", "count"),
        ("best_sale", "maximum"),
        ("sales_k", "amount"),
    ],
)
def test_multi_join_expressions(column: str, kind: str) -> None:
    assert classify_sql_columns(MULTI_JOIN_SQL)[column] == kind


def test_kinds_flow_through_cte_and_subquery() -> None:
    cte = (
        "WITH t AS (SELECT plant, SUM(downtime_hours) d, SUM(available_hours) a "
        "FROM x JOIN y ON x.k = y.k GROUP BY plant) "
        "SELECT plant, d, CAST(d AS FLOAT) / NULLIF(a, 0) * 100 AS oee FROM t"
    )
    assert classify_sql_columns(cte) == {"d": "amount", "oee": "percentage"}

    sub = (
        "SELECT q.region, q.r FROM (SELECT region, CAST(SUM(a) AS DECIMAL) / SUM(b) AS r "
        "FROM t GROUP BY region) q"
    )
    assert classify_sql_columns(sub)["r"] == "ratio"


def test_running_total_is_not_additive() -> None:
    sql = "SELECT month, SUM(SUM(sales)) OVER (ORDER BY month) AS cum FROM t GROUP BY month"
    assert classify_sql_columns(sql)["cum"] == "running_total"


def test_sql_structure_beats_misleading_name() -> None:
    # "margin_amt" is an additive amount even though the name says margin.
    sql = "SELECT zone, SUM(sales) - SUM(cost) AS margin_amt FROM t GROUP BY zone"
    assert resolve_column_kinds(["zone", "margin_amt"], sql=sql) == {"margin_amt": "amount"}


def test_semantic_layer_used_when_sql_is_opaque() -> None:
    catalog = {
        "metrics": [
            {"name": "Yield Loss", "expression": "SUM(loss) / SUM(input_qty)"},
            {"name": "Plant OEE", "expression": "oee_value", "format": "percent"},
        ]
    }
    kinds = resolve_column_kinds(["yield_loss", "plant_oee"], sql=None, semantic_catalog=catalog)
    assert kinds == {"yield_loss": "ratio", "plant_oee": "percentage"}


def test_name_fallback_handles_camel_case_and_skips_false_positives() -> None:
    kinds = resolve_column_kinds(["grossMarginPct", "duration_hours", "operated_hours"])
    assert kinds == {"grossMarginPct": "percentage"}


def test_unparseable_sql_does_not_raise() -> None:
    assert classify_sql_columns("not sql at all ((") == {}


@pytest.mark.parametrize(
    "name",
    ["phone", "Phone Number", "mobile_no", "contact_number", "zip_code", "pincode", "account_number", "card_no"],
)
def test_numeric_codes_are_not_measures(name: str) -> None:
    from app.agent.measure_kind import is_code_like_name

    assert is_code_like_name(name)


@pytest.mark.parametrize("name", ["revenue", "total_calls", "telephony_cost", "license_count", "patient_count"])
def test_real_measures_are_not_codes(name: str) -> None:
    from app.agent.measure_kind import is_code_like_name

    assert not is_code_like_name(name)
