"""P1-1: Chat insight grounding, fact validation, and domain-agnostic truthfulness."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.agent.chat_insight_facts import (
    extract_chat_insight_facts,
    grounding_context_from_chat_facts,
)
from app.agent.chat_insight_narrator import narrate_chat_insights
from app.agent.dashboard_insight_facts import (
    InsightFactStatus,
    extract_dashboard_insight_facts,
    validate_narrative_numbers,
)
from app.agent.insight_validation import (
    ComparisonDirectionFact,
    EntityLeaderFact,
    GroundingContext,
    GroundingOutcome,
    NumericClaim,
    RankingFact,
    TrendDirectionFact,
    parse_numeric_token,
    render_deterministic_grounded_answer,
    validate_grounded_narrative,
)
from app.agent.query_plan import QueryPlan, TimeSpec
from app.agent.workflow import InsightGenerator


def _ctx(**kwargs: Any) -> GroundingContext:
    return GroundingContext(**kwargs)


@pytest.mark.parametrize(
    "token,expected",
    [
        ("123", 123.0),
        ("1,234", 1234.0),
        ("1.23", 1.23),
        ("12.5%", 12.5),
        ("-4.7%", -4.7),
        ("₹1,25,000", 125000.0),
        ("$12,500", 12500.0),
        ("€1,200", 1200.0),
        ("1.2K", 1200.0),
        ("2.5M", 2_500_000.0),
        ("3.4B", 3_400_000_000.0),
    ],
)
def test_parse_numeric_token_formats(token: str, expected: float) -> None:
    assert parse_numeric_token(token) == expected


def test_valid_rounding_tolerance() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        numeric_claims=[NumericClaim(value=142.0, formatted="142", metric="Count", aliases=["count"])],
    )
    assert validate_grounded_narrative("The count is 142.", ctx)
    assert validate_grounded_narrative("The count is 141.", ctx)


def test_invalid_rounding() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        numeric_claims=[NumericClaim(value=142.0, formatted="142", metric="Count", aliases=["count"])],
    )
    assert not validate_grounded_narrative("The count is 153.", ctx)


def test_adversarial_wrong_constituency_count() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        numeric_claims=[
            NumericClaim(value=142.0, formatted="142", metric="Constituencies", aliases=["constituencies", "constituenc"])
        ],
    )
    assert validate_grounded_narrative("There were 142 constituencies.", ctx)
    assert not validate_grounded_narrative("There were 153 constituencies.", ctx)


def test_adversarial_wrong_metric_for_number() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        numeric_claims=[
            NumericClaim(value=1_000_000.0, formatted="1,000,000", metric="Revenue", aliases=["revenue"]),
            NumericClaim(value=500.0, formatted="500", metric="Orders", aliases=["orders"]),
        ],
    )
    assert not validate_grounded_narrative("Orders were 1,000,000.", ctx)
    assert validate_grounded_narrative("Revenue was 1,000,000.", ctx)


def test_adversarial_wrong_highest_entity() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        entity_leaders=[
            EntityLeaderFact(dimension="Region", leader_label="A", leader_value=500.0, runner_up_label="B"),
            EntityLeaderFact(dimension="Region", leader_label="B", leader_value=300.0),
        ],
        rankings=[RankingFact(dimension="Region", ordered_labels=["A", "B", "C"], ordered_values=[500, 300, 100])],
    )
    assert not validate_grounded_narrative("B ranked first by volume.", ctx)


def test_adversarial_inverted_change_direction() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        comparisons=[ComparisonDirectionFact(label="Metric", direction="up", change_pct=20.0)],
        numeric_claims=[
            NumericClaim(value=120.0, metric="current", aliases=["current"]),
            NumericClaim(value=100.0, metric="previous", aliases=["previous"]),
            NumericClaim(value=20.0, metric="change", aliases=["change", "increased", "decreased", "value"], unit="%"),
        ],
    )
    assert not validate_grounded_narrative("The value decreased 20%.", ctx)
    assert validate_grounded_narrative("The value increased 20%.", ctx)


def test_adversarial_february_lower_when_up() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        trends=[TrendDirectionFact(label="Monthly", direction="up", points=3, change_pct=20.0)],
        comparisons=[ComparisonDirectionFact(label="Monthly", direction="up", change_pct=20.0)],
    )
    assert not validate_grounded_narrative("February was 20% lower.", ctx)


def test_adversarial_trend_with_one_point() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        trends=[TrendDirectionFact(label="Metric", direction="unknown", points=1)],
        numeric_claims=[NumericClaim(value=100.0, metric="Value", aliases=["value"])],
    )
    assert not validate_grounded_narrative("The metric increased over the period.", ctx)


def test_valid_trend_up() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        trends=[TrendDirectionFact(label="Metric", direction="up", points=3, change_pct=40.0)],
    )
    assert validate_grounded_narrative("The metric increased over the period.", ctx)


def test_adversarial_unsupported_causation() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        numeric_claims=[NumericClaim(value=20.0, metric="Decline", aliases=["declined"], unit="%")],
        allows_causation=False,
    )
    assert validate_grounded_narrative("The metric declined 20%.", ctx)
    assert not validate_grounded_narrative(
        "The metric declined 20% because customers were unhappy.", ctx
    )


def test_adversarial_filtered_context_wrong_value() -> None:
    facts = extract_chat_insight_facts(
        [{"category": "North", "value": 100}],
        question="North total",
        filters=[{"field": "Region", "operator": "eq", "value": "North"}],
        tenant_id=uuid4(),
        connection_id=str(uuid4()),
    )
    ctx = grounding_context_from_chat_facts(facts)
    assert validate_grounded_narrative("North total is 100.", ctx)
    assert not validate_grounded_narrative("Company-wide total is 500.", ctx)


def test_extract_chat_facts_from_scalar_row() -> None:
    facts = extract_chat_insight_facts([{"value": 67.5}], question="What is turnout?")
    assert facts.row_count == 1
    assert facts.kpis[0].value == 67.5


def test_extract_chat_facts_empty_rows() -> None:
    facts = extract_chat_insight_facts([], question="Anything")
    assert facts.status == InsightFactStatus.INSUFFICIENT_DATA


def test_extract_chat_facts_grouped_rows() -> None:
    facts = extract_chat_insight_facts(
        [
            {"category": "A", "value": 500},
            {"category": "B", "value": 300},
            {"category": "C", "value": 100},
        ],
        question="Top categories",
    )
    assert facts.categories
    assert facts.categories[0].top[0].label == "A"


def test_narrator_invalid_llm_uses_fallback() -> None:
    facts = extract_chat_insight_facts(
        [{"category": "West", "value": 600}, {"category": "East", "value": 400}],
        question="Regional split",
    )

    class BadLLM:
        def complete(self, prompt: str) -> str:
            return "East had 999 units and dominated everything."

    answer, mode, outcome = narrate_chat_insights(facts, BadLLM(), question="Regional split")
    assert mode == "deterministic_fallback"
    assert outcome == GroundingOutcome.FALLBACK
    assert "999" not in answer


def test_narrator_valid_llm_accepted() -> None:
    facts = extract_chat_insight_facts([{"value": 142}], question="How many?")

    class GoodLLM:
        def complete(self, prompt: str) -> str:
            return "The total is 142."

    answer, mode, outcome = narrate_chat_insights(facts, GoodLLM(), question="How many?")
    assert mode == "llm"
    assert outcome in {GroundingOutcome.VALID, GroundingOutcome.REGENERATED}
    assert "142" in answer


def test_narrator_llm_unavailable_fallback() -> None:
    facts = extract_chat_insight_facts([{"value": 50}], question="Total")
    answer, mode, outcome = narrate_chat_insights(facts, None, question="Total")
    assert mode == "deterministic_fallback"
    assert outcome == GroundingOutcome.FALLBACK
    assert answer


def test_narrator_insufficient_data() -> None:
    facts = extract_chat_insight_facts([], question="Growth?")
    answer, mode, outcome = narrate_chat_insights(facts, None, question="Growth?")
    assert outcome == GroundingOutcome.INSUFFICIENT_DATA
    assert "enough data" in answer.lower() or "no rows" in answer.lower()


def test_insight_generator_grounded_sql_path() -> None:
    ig = InsightGenerator()
    out = ig.run(
        {
            "intent": "sql",
            "question": "Total?",
            "sql": "SELECT 100 AS value",
            "rows": [{"value": 100}],
            "tenant_id": str(uuid4()),
            "connection_id": str(uuid4()),
        }
    )
    assert out.get("answer")
    assert out.get("chat_grounding")
    assert "100" in out["answer"] or "enough data" in out["answer"].lower()


def test_chat_facts_scope_key_differs_by_tenant() -> None:
    rows = [{"value": 10}]
    a = extract_chat_insight_facts(rows, tenant_id=uuid4(), connection_id="c1")
    b = extract_chat_insight_facts(rows, tenant_id=uuid4(), connection_id="c1")
    assert a.scope_key != b.scope_key


def test_chat_facts_scope_key_differs_by_connection() -> None:
    tid = uuid4()
    rows = [{"value": 10}]
    a = extract_chat_insight_facts(rows, tenant_id=tid, connection_id=str(uuid4()))
    b = extract_chat_insight_facts(rows, tenant_id=tid, connection_id=str(uuid4()))
    assert a.scope_key != b.scope_key


def test_dashboard_validate_delegates_to_shared_validator() -> None:
    dash = {
        "id": "d1",
        "widgets": [
            {"id": "k1", "type": "kpi", "title": "Total", "value": 100, "rows": [{"value": 100}]},
            {
                "id": "c1",
                "type": "chart",
                "title": "By Region",
                "chart": "bar",
                "rows": [{"category": "A", "value": 60}, {"category": "B", "value": 40}],
            },
        ],
    }
    facts = extract_dashboard_insight_facts(dash)
    assert validate_narrative_numbers("Total is 100.", facts)
    assert not validate_narrative_numbers("Total is 999.", facts)


def test_deterministic_fallback_no_fabrication() -> None:
    ctx = _ctx(
        status=InsightFactStatus.INSUFFICIENT_DATA,
        insufficient_reason="No data.",
    )
    answer = render_deterministic_grounded_answer(ctx)
    assert "No data" in answer


def test_yoy_comparison_validation() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        comparisons=[ComparisonDirectionFact(label="YoY", direction="up", change_pct=25.0)],
        numeric_claims=[NumericClaim(value=25.0, unit="%", aliases=["yoy", "year", "growth"])],
    )
    assert validate_grounded_narrative("YoY growth was 25%.", ctx)
    assert not validate_grounded_narrative("YoY growth was 30%.", ctx)


def test_time_context_not_ytd_from_last_year_plan() -> None:
    plan = QueryPlan(
        intent="comparison",
        metrics=["Revenue"],
        time=TimeSpec(field="order_date", grain="year", range="last_30_days"),
    )
    facts = extract_chat_insight_facts(
        [{"value": 100}],
        question="compare with last year",
        query_plan=plan,
    )
    assert facts.time_context is not None
    assert facts.time_context.get("range") != "ytd"


def test_malformed_llm_stub_fallback() -> None:
    facts = extract_chat_insight_facts([{"value": 42}], question="Count")

    class StubLLM:
        def complete(self, prompt: str) -> str:
            return "[LLM stub]"

    answer, mode, _ = narrate_chat_insights(facts, StubLLM())
    assert mode == "deterministic_fallback"
    assert "42" in answer or "Value" in answer


def test_correct_ranking_highest() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        rankings=[RankingFact(dimension="Items", ordered_labels=["Alpha", "Beta"], ordered_values=[500, 300])],
        entity_leaders=[EntityLeaderFact(dimension="Items", leader_label="Alpha", leader_value=500.0)],
    )
    assert validate_grounded_narrative("Alpha ranked first.", ctx)


def test_incorrect_ranking_lowest() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        rankings=[RankingFact(dimension="Items", ordered_labels=["Alpha", "Beta"], ordered_values=[500, 300])],
    )
    assert not validate_grounded_narrative("Alpha had the lowest value.", ctx)


def test_correct_categorical_winner() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        entity_leaders=[
            EntityLeaderFact(dimension="Segment", leader_label="Party A", leader_value=142.0),
        ],
        numeric_claims=[
            NumericClaim(value=142.0, dimension="Segment", dimension_value="Party A", aliases=["party", "a"]),
        ],
    )
    assert validate_grounded_narrative("Party A won the most segments.", ctx)
    assert not validate_grounded_narrative("Party B won the most segments.", ctx)


def test_correct_ratio_and_incorrect_ratio() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        numeric_claims=[
            NumericClaim(value=25.0, formatted="25%", metric="Share", aliases=["share", "ratio"], unit="%"),
        ],
    )
    assert validate_grounded_narrative("Share was 25%.", ctx)
    assert not validate_grounded_narrative("Share was 20%.", ctx)


def test_missing_comparison_period_rejected() -> None:
    ctx = _ctx(
        status=InsightFactStatus.OK,
        numeric_claims=[NumericClaim(value=120.0, metric="Current", aliases=["current"])],
        comparisons=[],
    )
    assert not validate_grounded_narrative("Growth was 20%.", ctx)


def test_filtered_fact_accepted_unfiltered_rejected() -> None:
    filtered = extract_chat_insight_facts(
        [{"category": "North", "value": 100}],
        question="North volume",
        filters=[{"field": "Region", "operator": "eq", "value": "North"}],
    )
    ctx = grounding_context_from_chat_facts(filtered)
    assert validate_grounded_narrative("North volume is 100.", ctx)
    assert not validate_grounded_narrative("Total volume is 500.", ctx)


def test_stale_unfiltered_facts_rejected_when_filtered() -> None:
    unfiltered_ctx = grounding_context_from_chat_facts(
        extract_chat_insight_facts([{"value": 500}], question="Total")
    )
    filtered_ctx = grounding_context_from_chat_facts(
        extract_chat_insight_facts(
            [{"value": 100}],
            question="North only",
            filters=[{"field": "Region", "operator": "eq", "value": "North"}],
        )
    )
    assert validate_grounded_narrative("Total is 500.", unfiltered_ctx)
    assert not validate_grounded_narrative("Total is 500.", filtered_ctx)


def test_narrator_regeneration_after_invalid() -> None:
    facts = extract_chat_insight_facts([{"value": 88}], question="Count")
    calls: list[str] = []

    class RetryLLM:
        def complete(self, prompt: str) -> str:
            calls.append(prompt)
            if len(calls) == 1:
                return "The count is 999."
            return "The count is 88."

    answer, mode, outcome = narrate_chat_insights(facts, RetryLLM(), question="Count")
    assert len(calls) == 2
    assert mode == "llm"
    assert outcome == GroundingOutcome.REGENERATED
    assert "88" in answer


def test_validator_error_fails_safe(monkeypatch) -> None:
    facts = extract_chat_insight_facts([{"value": 10}], question="Count")

    def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(
        "app.agent.chat_insight_narrator.validate_grounded_narrative",
        boom,
    )

    class GoodLLM:
        def complete(self, prompt: str) -> str:
            return "Count is 10."

    answer, mode, outcome = narrate_chat_insights(facts, GoodLLM())
    assert mode == "deterministic_fallback"
    assert outcome == GroundingOutcome.ERROR
    assert "10" in answer


def test_conversation_scope_key_includes_question() -> None:
    rows = [{"value": 1}]
    a = extract_chat_insight_facts(rows, question="Q1", tenant_id=uuid4(), connection_id="c1")
    b = extract_chat_insight_facts(rows, question="Q2", tenant_id=a.tenant_id, connection_id=a.connection_id)
    assert a.scope_key != b.scope_key


def test_multi_level_grouping_keeps_every_dimension_and_rolls_up():
    rows = [
        {"zone": "East", "state": "Bihar", "sales_value": 88.7, "profit_margin_percentage": 4.94},
        {"zone": "East", "state": "Odisha", "sales_value": 92.6, "profit_margin_percentage": 4.86},
        {"zone": "West", "state": "Gujarat", "sales_value": 80.0, "profit_margin_percentage": 5.10},
        {"zone": "West", "state": "Goa", "sales_value": 70.0, "profit_margin_percentage": 5.00},
    ]
    facts = extract_chat_insight_facts(
        rows,
        question="zone wise, state wise sales and margin",
        sql=(
            "SELECT z.zone, s.state, SUM(f.sales_value) AS sales_value, "
            "ROUND(100.0 * SUM(f.gross_profit) / NULLIF(SUM(f.sales_value), 0), 2) "
            "AS profit_margin_percentage "
            "FROM fact_sales f JOIN dim_state s ON s.id = f.state_id "
            "JOIN dim_zone z ON z.id = s.zone_id GROUP BY z.zone, s.state"
        ),
    )
    by_title = {c.title: c for c in facts.categories}

    # Row-level fact labels carry both zone and state, not a bare "East".
    sales = by_title["Sales Value by Zone, State"]
    assert sales.top[0].label == "East / Odisha"

    # Zone subtotal reflects the sum of its states.
    rollup = by_title["Sales Value Total by Zone"]
    assert rollup.top[0].label == "East"
    assert rollup.top[0].value == 88.7 + 92.6

    # Percentages are never summed or given a share of a series total.
    margin = by_title["Profit Margin Percentage by Zone, State"]
    assert margin.value_kind == "percentage"
    assert margin.total is None
    assert all(e.share_pct is None for e in margin.top)
    assert "Profit Margin Percentage Total by Zone" not in by_title
    assert not any("of the total" in h for h in facts.ranked_highlights if "Margin" in h)


def test_cryptic_ratio_alias_is_never_summed_or_shared():
    """A ratio aliased with no telltale name (e.g. "gpm") is classified from
    the SQL expression, not the alias."""
    rows = [
        {"zone": "East", "state": "Bihar", "gpm": 0.049},
        {"zone": "East", "state": "Odisha", "gpm": 0.048},
        {"zone": "West", "state": "Goa", "gpm": 0.051},
    ]
    facts = extract_chat_insight_facts(
        rows,
        sql=(
            "SELECT zone, state, SUM(gp) / NULLIF(SUM(sales), 0) AS gpm "
            "FROM f JOIN d ON d.k = f.k GROUP BY zone, state"
        ),
    )
    (cat,) = facts.categories
    assert cat.value_kind == "ratio"
    assert cat.total is None
    assert all(e.share_pct is None for e in cat.top)


def test_plan_metric_titles_match_by_name_not_position():
    rows = [
        {"zone": "East", "gross_profit": 4.4, "sales_value": 88.7},
        {"zone": "West", "gross_profit": 4.1, "sales_value": 80.0},
    ]
    facts = extract_chat_insight_facts(
        rows,
        sql="SELECT zone, SUM(gp) AS gross_profit, SUM(s) AS sales_value FROM t GROUP BY zone",
        query_plan={"intent": "aggregation", "metrics": ["Sales Value", "Gross Profit"], "dimensions": ["zone"]},
    )
    titles = {c.title: c.top[0].value for c in facts.categories}
    assert titles["Gross Profit by zone"] == 4.4
    assert titles["Sales Value by zone"] == 88.7


def test_single_row_with_label_and_several_measures_keeps_the_entity_name():
    # "which seller has the most delivery issues" -> one row, one label, four
    # measures. The label used to be dropped (bare KPIs only), so the answer
    # claimed no name was provided while the result table showed it.
    from app.agent.chat_insight_facts import extract_chat_insight_facts

    row = {
        "customer_name": "Industrial Customer 045",
        "delivery_issue_count": 99,
        "issue_quantity_mt": 50119.0,
        "late_delivery_rate_percent": 48.48,
    }
    facts = extract_chat_insight_facts(
        [row],
        question="which customer has the most delivery issues",
        sql="SELECT customer_name, COUNT(*) AS delivery_issue_count, SUM(q) AS issue_quantity_mt, "
        "100.0 * AVG(CASE WHEN f = 0 THEN 1.0 ELSE 0 END) AS late_delivery_rate_percent FROM t GROUP BY 1 LIMIT 1",
    )
    assert not facts.kpis
    assert {c.top[0].label for c in facts.categories} == {"Industrial Customer 045"}
    text = "\n".join(facts.ranked_highlights)
    assert "Industrial Customer 045 — 99." in text
    assert "48.48%" in text
    assert "series total" not in text and "100.0%" not in text


def _delivery_issue_ctx():
    from app.agent.chat_insight_facts import extract_chat_insight_facts, grounding_context_from_chat_facts

    rows = [
        {"customer_name": "Industrial Customer 045", "delivery_issue_count": 99,
         "issue_quantity_mt": 50119.0, "late_delivery_rate_percent": 48.48},
        {"customer_name": "Retail Customer 012", "delivery_issue_count": 87,
         "issue_quantity_mt": 40210.0, "late_delivery_rate_percent": 41.2},
    ]
    facts = extract_chat_insight_facts(
        rows,
        question="which customer has the most delivery issues",
        sql="SELECT customer_name, COUNT(*) AS delivery_issue_count, SUM(q) AS issue_quantity_mt, "
        "100.0 * AVG(x) AS late_delivery_rate_percent FROM t GROUP BY 1 ORDER BY 2 DESC",
    )
    return facts, grounding_context_from_chat_facts(facts)


def test_natural_answer_quoting_the_rows_is_grounded():
    # Digits inside an entity name ("Customer 045") are not value claims;
    # one entity's count/quantity/rate don't contradict each other; and
    # "most" in one sentence doesn't rank the runner-up in the next.
    _, ctx = _delivery_issue_ctx()
    answer = (
        "**Industrial Customer 045** has the most delivery issues: 99 late dispatches "
        "totalling 50,119 MT, with a late delivery rate of 48.48%. "
        "Retail Customer 012 follows with 87 issues."
    )
    assert validate_grounded_narrative(answer, ctx)


def test_row_grounding_still_rejects_wrong_or_invented_claims():
    _, ctx = _delivery_issue_ctx()
    assert not validate_grounded_narrative("Retail Customer 012 has the most delivery issues with 87.", ctx)
    assert not validate_grounded_narrative("Retail Customer 012 has 99 delivery issues.", ctx)
    assert not validate_grounded_narrative("Industrial Customer 045 has 120 delivery issues.", ctx)


def test_narrator_prompt_carries_the_result_rows():
    facts, _ = _delivery_issue_ctx()
    seen: dict[str, str] = {}

    class _Capture:
        def complete(self, prompt: str) -> str:
            seen["prompt"] = prompt
            return "Industrial Customer 045 has 99 delivery issues."

    narrate_chat_insights(facts, _Capture(), question=facts.question)
    assert "RESULT ROWS (all 2 rows" in seen["prompt"]
    assert '"customer_name": "Industrial Customer 045"' in seen["prompt"]


def test_year_and_month_columns_become_a_chronological_period():
    # Numeric year + month used to be dropped as identifier-like, leaving no
    # label ("not enough dashboard data"), and a month name alone repeated
    # across years. Both now fold into one period label in calendar order.
    rows = [
        {"calendar_year": 2025, "month_name": "October", "late_shipments": 414},
        {"calendar_year": 2024, "month_name": "October", "late_shipments": 388},
        {"calendar_year": 2025, "month_name": "April", "late_shipments": 350},
        {"calendar_year": 2024, "month_name": "April", "late_shipments": 360},
    ]
    facts = extract_chat_insight_facts(
        rows, question="late shipments by month",
        sql="SELECT calendar_year, month_name, COUNT(*) AS late_shipments FROM t GROUP BY 1, 2",
    )
    assert facts.trends and facts.trends[0].first_value == 360 and facts.trends[0].last_value == 414

    numeric = [
        {"calendar_year": 2025, "month_number": m, "late_deliveries": v}
        for m, v in [(3, 300), (1, 100), (2, 200)]
    ]
    facts = extract_chat_insight_facts(
        numeric, question="late deliveries",
        sql="SELECT calendar_year, month AS month_number, COUNT(*) AS late_deliveries FROM t GROUP BY 1, 2",
    )
    assert facts.status != InsightFactStatus.INSUFFICIENT_DATA
    assert facts.trends[0].first_value == 100 and facts.trends[0].last_value == 300


def test_trend_is_first_to_last_period_not_max_to_min():
    from app.agent.dashboard_insight_facts import extract_dashboard_insight_facts

    dashboard = {
        "id": "",
        "widgets": [{
            "id": "w", "type": "chart", "chart": "line", "title": "Sales Over Time",
            # ORDER BY period DESC, as dashboard trend widgets are compiled.
            "rows": [{"period": f"2025-{m:02d}-01", "value": v} for m, v in [(12, 400), (11, 900), (10, 100), (9, 200)]],
        }],
    }
    trend = extract_dashboard_insight_facts(dashboard).trends[0]
    assert (trend.first_value, trend.last_value) == (200, 400)
