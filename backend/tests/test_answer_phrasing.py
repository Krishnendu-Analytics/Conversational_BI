"""Answers talk about the business, not the machinery: no SQL or table names
reach the narrator, rank columns read as positions, and ties are ties."""

from app.agent.chat_insight_facts import extract_chat_insight_facts
from app.agent.chat_insight_narrator import narrate_chat_insights
from app.agent.dashboard_insight_facts import (
    CategoryEntry,
    CategoryFact,
    DashboardInsightFacts,
    _build_ranked_highlights,
)
from app.agent.measure_kind import classify_sql_columns, kind_from_name


def test_rank_columns_are_ranks() -> None:
    kinds = classify_sql_columns(
        "SELECT region, SUM(sales) AS sales_2025, "
        "RANK() OVER (ORDER BY SUM(sales) DESC) AS r1, "
        "DENSE_RANK() OVER (ORDER BY SUM(sales) DESC) AS r2, "
        "ROW_NUMBER() OVER (ORDER BY SUM(sales) DESC) AS r3 "
        "FROM fact_sales GROUP BY region"
    )
    assert kinds["sales_2025"] == "amount"
    assert kinds["r1"] == kinds["r2"] == kinds["r3"] == "rank"
    # A running total is still a running total.
    assert classify_sql_columns("SELECT SUM(x) OVER (ORDER BY d) AS cum FROM t")["cum"] == "running_total"


def test_rank_names_are_ranks_without_breaking_similar_words() -> None:
    assert kind_from_name("sales_rank_2025") == "rank"
    assert kind_from_name("SalesRanking") == "rank"
    assert kind_from_name("frank_amount") is None  # "rank" inside a word
    assert kind_from_name("gross_margin_pct") == "percentage"


def _category(title: str, kind: str, entries: list[tuple[str, float]], share: bool = True) -> CategoryFact:
    total = sum(v for _, v in entries)
    return CategoryFact(
        widget_id=title,
        title=title,
        value_kind=kind,
        category_count=len(entries),
        top=[
            CategoryEntry(label=label, value=value, share_pct=(100 * value / total) if share else None)
            for label, value in entries
        ],
        significance_score=1.0,
    )


def test_fallback_skips_rank_columns_and_reports_ties() -> None:
    facts = DashboardInsightFacts(
        categories=[
            _category("Sales 2025 Total by Region", "amount", [("East India", 180.73e9), ("North India", 120e9)]),
            _category("Sales Rank 2025 by Region", "rank", [("North India", 4), ("South India", 3)]),
            _category("Customers by City", "count", [("New York", 1), ("Chicago", 1), ("Boston", 1)]),
        ]
    )
    bullets = _build_ranked_highlights(facts)
    text = "\n".join(bullets)
    assert "Sales 2025 Total by Region: highest is East India" in text
    assert "Rank" not in text
    assert "Customers by City: tie between New York, Chicago, Boston at 1 each." in bullets
    assert "highest is Boston" not in text


def test_narrator_never_sees_sql_or_ids() -> None:
    sql = "SELECT SUM(amount) AS total_spend FROM data_mart.fact_procurement WHERE calendar_year = 2025"
    facts = extract_chat_insight_facts(
        [{"total_spend": 4162810000000}],
        question="total procurement spend in 2025",
        sql=sql,
        tenant_id="tenant-123",
        connection_id="conn-456",
    )
    prompts: list[str] = []

    class LLM:
        def complete(self, prompt: str) -> str:
            prompts.append(prompt)
            return "Total procurement spend in 2025 was 4,162,810,000,000."

    narrate_chat_insights(facts, LLM(), question="total procurement spend in 2025")
    assert prompts, "the narrator was not called"
    for leaked in ("fact_procurement", "data_mart", "SELECT", "calendar_year", "tenant-123", "conn-456"):
        assert leaked not in prompts[0], leaked
    assert "4162810000000" in prompts[0]  # the business figure is still there


def test_top_one_queries_fetch_enough_rows_to_see_ties() -> None:
    from app.agent.workflow import _widen_top_one

    grouped = "SELECT city, COUNT(*) AS n FROM customers GROUP BY city ORDER BY n DESC, city LIMIT 1"
    assert _widen_top_one(grouped, "which city has the most customers?").endswith("LIMIT 5")
    assert _widen_top_one(grouped + ";", "which city has the most customers?").endswith("LIMIT 5;")
    tsql = "SELECT TOP 1 city, COUNT(*) AS n FROM customers GROUP BY city ORDER BY n DESC"
    assert _widen_top_one(tsql, "which city has the most customers?").startswith("SELECT TOP 5 city")
    # Asked for exactly one, not a ranking, or already a real top-N: untouched.
    assert _widen_top_one(grouped, "show the top 1 city") == grouped
    assert _widen_top_one("SELECT * FROM orders ORDER BY created_at DESC LIMIT 1", "latest order") .endswith("LIMIT 1")
    top10 = grouped.replace("LIMIT 1", "LIMIT 10")
    assert _widen_top_one(top10, "top 10 cities") == top10


def test_labels_never_carry_table_names_and_titles_are_readable() -> None:
    facts = extract_chat_insight_facts(
        [{"region": "East India", "total_sales": 180.73e9, "database_source": "data_mart.fact_sales"}],
        question="which region had the highest sales in 2025?",
        sql="SELECT region, SUM(x) AS total_sales, 'data_mart.fact_sales' AS database_source FROM t GROUP BY region",
    )
    # Everything the narrator is shown (the SQL itself is never shown).
    text = facts.model_dump_json(exclude={"sql"})
    assert "data_mart" not in text and "fact_sales" not in text
    assert "East India" in text

    scalar = extract_chat_insight_facts(
        [{"total_planned_production_mt": 91.81e6}], question="planned production", sql="SELECT SUM(x) AS total_planned_production_mt FROM t"
    )
    assert "Total Planned Production Mt" in "\n".join(scalar.ranked_highlights)
    assert "total_planned_production_mt:" not in "\n".join(scalar.ranked_highlights)


def test_similarly_named_entities_can_be_compared() -> None:
    from app.agent.chat_insight_facts import grounding_context_from_chat_facts
    from app.agent.insight_validation import validate_grounded_narrative

    rows = [
        {"metric": "Actual Sales Quantity", "quantity_mt": 12172204.0},
        {"metric": "Budgeted Sales Quantity", "quantity_mt": 49717096.0},
    ]
    ctx = grounding_context_from_chat_facts(extract_chat_insight_facts(rows, question="actual vs budget", sql="x"))
    good = ("- **Actual Sales Quantity:** 12,172,204.0 Mt\n"
            "- **Budgeted Sales Quantity:** 49,717,096.0 Mt\nBudgeted Sales Quantity is higher.")
    assert validate_grounded_narrative(good, ctx)
    # Swapped values: the distinguishing words (actual / budgeted) catch it.
    swapped = "- **Budgeted Sales Quantity:** 12,172,204.0 Mt\n- **Actual Sales Quantity:** 49,717,096.0 Mt"
    assert not validate_grounded_narrative(swapped, ctx)


def test_comparison_sentences_attach_each_number_to_its_own_label() -> None:
    from app.agent.chat_insight_facts import grounding_context_from_chat_facts
    from app.agent.insight_validation import validate_grounded_narrative

    rows = [
        {"metric": "Actual Sales Quantity", "quantity_mt": 12172204.0},
        {"metric": "Budgeted Sales Quantity", "quantity_mt": 49717096.0},
    ]
    ctx = grounding_context_from_chat_facts(extract_chat_insight_facts(rows, question="actual vs budget", sql="x"))
    assert validate_grounded_narrative(
        "Actual Sales Quantity (12,172,204) is well below Budgeted Sales Quantity (49,717,096).", ctx
    )
    assert not validate_grounded_narrative(
        "Actual Sales Quantity (49,717,096) is above Budgeted Sales Quantity (12,172,204).", ctx
    )


def test_safety_validator_accepts_derived_tables_but_stays_strict() -> None:
    import pytest

    from app.tools.sql_safety import SchemaValidator

    schema = {"tables": [
        {"schema_name": "dm", "table_name": "sales", "columns": [{"name": "qty"}, {"name": "month"}]},
        {"schema_name": "dm", "table_name": "targets", "columns": [{"name": "target_qty"}, {"name": "month"}]},
    ]}
    v = SchemaValidator(schema, "postgres")
    drill_across = (
        "SELECT s.month, s.actual_qty, t.budget_qty FROM "
        "(SELECT month, SUM(qty) AS actual_qty FROM dm.sales GROUP BY month) s JOIN "
        "(SELECT month, SUM(target_qty) AS budget_qty FROM dm.targets GROUP BY month) t ON s.month = t.month"
    )
    assert v.validate(drill_across) == drill_across
    # A column the subquery does not output is still rejected...
    with pytest.raises(ValueError):
        v.validate("SELECT s.invented FROM (SELECT SUM(qty) AS actual_qty FROM dm.sales) s")
    # ...and so is a bad column inside the subquery itself.
    with pytest.raises(ValueError):
        v.validate("SELECT s.x FROM (SELECT SUM(nope) AS x FROM dm.sales) s")
