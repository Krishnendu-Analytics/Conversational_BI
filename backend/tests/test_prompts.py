from app.agent.prompts import (
    PROMPT_VERSION,
    get_prompt,
)


def test_intent_classifier_prompt_loads():
    prompt = get_prompt("intent_classifier")

    assert prompt.name == "intent_classifier"
    assert prompt.version == PROMPT_VERSION


def test_intent_classifier_prompt_renders():
    prompt = get_prompt("intent_classifier")

    rendered = prompt.render(
        question="Show me all customers",
        conversation_context="(none)",
    )

    assert "Show me all customers" in rendered
    assert "sql" in rendered
    assert "rag" in rendered
    assert "hybrid" in rendered
    assert "greeting" in rendered
    assert "schema" in rendered


def test_sql_generation_prompt_loads():
    prompt = get_prompt("sql_generation")

    assert prompt.name == "sql_generation"
    assert prompt.version == PROMPT_VERSION


def test_sql_generation_prompt_renders():
    prompt = get_prompt("sql_generation")

    rendered = prompt.render(
        question="Show me the top 5 customers",
        db_type="postgres",
        schema="customers(customer_id integer (pk), customer_name varchar)",
        semantic_context="Metrics:\n  - Revenue: SUM(orders.quantity * products.price)",
        conversation_context="",
        question_hints="None.",
        dialect_rules="- INTERVAL syntax: INTERVAL '3 months'",
    )

    assert "Show me the top 5 customers" in rendered
    assert "postgres" in rendered
    assert "customers(customer_id integer (pk)" in rendered
    assert "Revenue" in rendered
    assert "SELECT" in rendered
    assert "INTERVAL" in rendered


def test_insights_prompt_loads():
    prompt = get_prompt("insights")

    assert prompt.name == "insights"
    assert prompt.version == PROMPT_VERSION


def test_unknown_prompt_raises_error():
    try:
        get_prompt("unknown_prompt")
        assert False, "Expected KeyError"
    except KeyError:
        pass


def test_wrong_prompt_version_raises_error():
    try:
        get_prompt("intent_classifier", version="v99")
        assert False, "Expected KeyError"
    except KeyError:
        pass
