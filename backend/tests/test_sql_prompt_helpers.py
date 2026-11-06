"""Tests for SQL prompt helpers and post-processing."""

from app.agent.workflow import (
    _dialect_rules_for_prompt,
    _fallback_sql,
    _format_schema_for_prompt,
    _format_semantic_for_prompt,
    _postprocess_sql,
    _question_hints_for_prompt,
)


def _postgres_sample_schema() -> dict:
    return {
        "connection_id": "connection-123",
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "customers",
                "columns": [
                    {
                        "name": "customer_id",
                        "data_type": "integer",
                        "is_pk": True,
                    },
                    {"name": "customer_name", "data_type": "varchar"},
                ],
                "foreign_keys": [],
            },
            {
                "schema_name": "public",
                "table_name": "products",
                "columns": [
                    {
                        "name": "product_id",
                        "data_type": "integer",
                        "is_pk": True,
                    },
                    {"name": "price", "data_type": "numeric"},
                ],
                "foreign_keys": [],
            },
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [
                    {
                        "name": "order_id",
                        "data_type": "integer",
                        "is_pk": True,
                    },
                    {"name": "customer_id", "data_type": "integer"},
                    {"name": "product_id", "data_type": "integer"},
                    {"name": "quantity", "data_type": "integer"},
                    {"name": "order_date", "data_type": "date"},
                ],
                "foreign_keys": [
                    {
                        "column": "customer_id",
                        "referred_table": "customers",
                        "referred_column": "customer_id",
                    },
                    {
                        "column": "product_id",
                        "referred_table": "products",
                        "referred_column": "product_id",
                    },
                ],
            },
        ],
    }


def test_format_schema_includes_pk_and_foreign_keys() -> None:
    rendered = _format_schema_for_prompt(_postgres_sample_schema())

    assert "public.orders" in rendered
    assert "order_id integer (pk)" in rendered
    assert "FK customer_id->public.customers.customer_id" in rendered
    assert "FK product_id->public.products.product_id" in rendered


def test_format_semantic_for_prompt_structures_catalog() -> None:
    rendered = _format_semantic_for_prompt(
        {
            "metrics": [
                {
                    "name": "Revenue",
                    "expression": "SUM(orders.quantity * products.price)",
                }
            ],
            "dimensions": [
                {"name": "City", "expression": "customers.city"},
            ],
        }
    )

    assert "Metrics" in rendered
    assert "Revenue: SUM(orders.quantity * products.price)" in rendered
    assert "Dimensions:" in rendered
    assert "City: customers.city" in rendered
    assert "Glossary" not in rendered


def test_format_semantic_empty_returns_none_configured() -> None:
    assert _format_semantic_for_prompt({}) == "None configured."
    assert _format_semantic_for_prompt(None) == "None configured."


def test_dialect_rules_for_postgres_include_interval_guidance() -> None:
    rules = _dialect_rules_for_prompt("postgres")
    assert "INTERVAL '3 months'" in rules
    assert "last quarter" in rules


def test_postprocess_sql_fixes_postgres_interval_syntax() -> None:
    broken = (
        "SELECT SUM(quantity) FROM orders "
        "WHERE order_date >= DATE_TRUNC('quarter', CURRENT_DATE) "
        "- INTERVAL '3' months"
    )
    fixed = _postprocess_sql(broken, "postgres")
    assert "INTERVAL '3 months'" in fixed
    assert "INTERVAL '3' months" not in fixed


def test_postprocess_sql_leaves_mysql_intervals_unchanged() -> None:
    sql = "SELECT 1 WHERE d >= DATE_SUB(NOW(), INTERVAL 3 MONTH)"
    assert _postprocess_sql(sql, "mysql") == sql


def test_question_hints_for_dashboard_lists_tables() -> None:
    schema = _postgres_sample_schema()
    hints = _question_hints_for_prompt("create a dashboard for this db", schema)
    assert "Overview/dashboard mode" in hints
    assert "public.orders" in hints
    assert "Do NOT assume" in hints


def test_fallback_sql_overview_uses_union_of_counts() -> None:
    sql = _fallback_sql(
        _postgres_sample_schema(),
        "give me an overview dashboard",
    )
    assert sql is not None
    assert "UNION ALL" in sql
    assert "row_count" in sql
    assert "public.orders" in sql


def test_fallback_sql_single_table_uses_numeric_sum() -> None:
    schema = {
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [
                    {"name": "quantity", "data_type": "integer"},
                ],
                "foreign_keys": [],
            }
        ],
    }
    sql = _fallback_sql(schema, "total quantity")
    assert sql == "SELECT SUM(quantity) AS total_quantity FROM public.orders"
