from app.tools.sql_safety import SQLSafetyPipeline


def _schema() -> dict:
    return {
        "connection_id": "connection-123",
        "db_type": "postgres",
        "tables": [
            {
                "table_name": "orders",
                "columns": [
                    {"name": "amount"},
                    {"name": "order_date"},
                ],
                "foreign_keys": [],
            }
        ],
    }


def test_gate1_invalid_sql_syntax_rejected() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema())
    try:
        pipeline.validate("SELEC * FROM orders")
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "Invalid SQL syntax" in str(exc)


def test_gate2_statement_whitelisting_only_select_allowed() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema())
    try:
        pipeline.validate("DELETE FROM orders")
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "Only SELECT statements are allowed" in str(exc)


def test_gate2_create_rejected() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema())
    try:
        pipeline.validate("CREATE TABLE orders (amount integer)")
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "Only SELECT statements are allowed" in str(exc)


def test_gate2_update_rejected() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema())
    try:
        pipeline.validate("UPDATE orders SET amount = 1")
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "Only SELECT statements are allowed" in str(exc)


def test_gate2_with_cte_select_allowed() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema())
    sql = (
        "WITH recent AS (SELECT amount FROM orders) "
        "SELECT amount FROM recent"
    )
    validated = pipeline.validate(sql)
    assert "LIMIT" in validated


def test_gate3_schema_validation_rejects_unknown_table() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema())
    try:
        pipeline.validate("SELECT amount FROM unknown_table")
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "Table 'unknown_table' does not exist" in str(exc)


def test_gate3_schema_validation_rejects_unknown_column() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema())
    try:
        pipeline.validate("SELECT bad_column FROM orders")
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "Column 'bad_column' does not exist" in str(exc)


def test_gate3_allows_select_alias_in_order_by() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema())
    sql = (
        "SELECT order_date, COUNT(*) AS total_orders "
        "FROM orders GROUP BY order_date "
        "ORDER BY total_orders DESC"
    )
    validated = pipeline.validate(sql)
    assert "total_orders" in validated
    assert "LIMIT" in validated


def test_gate3_rejects_invented_bare_column() -> None:
    """Bare columns must exist; they are not treated as aliases."""
    pipeline = SQLSafetyPipeline(schema=_schema())
    try:
        pipeline.validate(
            "SELECT telecaller_name, COUNT(*) AS total_calls FROM orders "
            "GROUP BY telecaller_name"
        )
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "telecaller_name" in str(exc)


def test_gate3_still_rejects_unknown_physical_column() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema())
    try:
        pipeline.validate(
            "SELECT amount AS revenue FROM orders ORDER BY missing_col"
        )
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "missing_col" in str(exc)


def test_gate3_rejects_unknown_column_on_qualified_table() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema(), db_type="postgres")
    try:
        pipeline.validate(
            "SELECT o.amount FROM orders AS o JOIN unknown u ON o.id = u.id"
        )
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "does not exist" in str(exc)


def test_gate3_rejects_invented_column_in_count() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema(), db_type="postgres")
    try:
        pipeline.validate(
            "SELECT COUNT(agreement_number) AS total FROM orders"
        )
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "agreement_number" in str(exc)


def test_gate4_execution_guard_injects_limit_when_missing() -> None:
    pipeline = SQLSafetyPipeline(schema=_schema(), db_type="postgres")
    validated = pipeline.validate("SELECT amount FROM orders")
    assert "LIMIT 10000" in validated


def _multi_schema() -> dict:
    return {
        "connection_id": "connection-123",
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [{"name": "amount"}, {"name": "order_date"}],
                "foreign_keys": [],
            },
            {
                "schema_name": "sales",
                "table_name": "orders",
                "columns": [{"name": "deal_amount"}, {"name": "closed_at"}],
                "foreign_keys": [],
            },
        ],
    }


def test_gate3_allows_schema_qualified_table() -> None:
    pipeline = SQLSafetyPipeline(schema=_multi_schema())
    validated = pipeline.validate(
        "SELECT deal_amount FROM sales.orders"
    )
    assert "LIMIT" in validated


def test_gate3_rejects_ambiguous_bare_table_across_schemas() -> None:
    pipeline = SQLSafetyPipeline(schema=_multi_schema())
    try:
        pipeline.validate("SELECT amount FROM orders")
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "orders" in str(exc)


def test_gate3_allows_qualified_alias_columns() -> None:
    pipeline = SQLSafetyPipeline(schema=_multi_schema())
    validated = pipeline.validate(
        "SELECT o.deal_amount FROM sales.orders AS o"
    )
    assert "deal_amount" in validated


from app.tools.sql_safety import assert_read_only_sql


def _assert_mutation_rejected(sql: str, db_type: str = "postgres") -> None:
    pipeline = SQLSafetyPipeline(schema=_schema(), db_type=db_type)
    try:
        pipeline.validate(sql)
        assert False, f"pipeline allowed: {sql}"
    except ValueError:
        pass
    try:
        assert_read_only_sql(sql, db_type=db_type)
        assert False, f"execution guard allowed: {sql}"
    except ValueError:
        pass


def test_gate2_insert_rejected() -> None:
    _assert_mutation_rejected("INSERT INTO orders (amount) VALUES (1)")


def test_gate2_truncate_rejected() -> None:
    _assert_mutation_rejected("TRUNCATE TABLE orders")


def test_gate2_alter_rejected() -> None:
    _assert_mutation_rejected("ALTER TABLE orders ADD COLUMN x integer")


def test_gate2_drop_rejected() -> None:
    _assert_mutation_rejected("DROP TABLE orders")


def test_gate2_merge_rejected() -> None:
    _assert_mutation_rejected(
        "MERGE INTO orders t USING orders s ON t.amount = s.amount "
        "WHEN MATCHED THEN UPDATE SET amount = 1"
    )


def test_gate2_grant_revoke_rejected() -> None:
    _assert_mutation_rejected("GRANT SELECT ON orders TO public")
    _assert_mutation_rejected("REVOKE SELECT ON orders FROM public")


def test_gate2_call_exec_rejected() -> None:
    _assert_mutation_rejected("CALL do_something()")
    _assert_mutation_rejected("EXEC sp_help", db_type="mssql")


def test_gate2_multi_statement_rejected() -> None:
    _assert_mutation_rejected("SELECT amount FROM orders; DELETE FROM orders")


def test_gate2_select_into_rejected() -> None:
    _assert_mutation_rejected("SELECT * INTO orders_copy FROM orders")


def test_gate2_modifying_cte_rejected() -> None:
    _assert_mutation_rejected(
        "WITH gone AS (DELETE FROM orders RETURNING amount) SELECT amount FROM gone"
    )


def test_execution_guard_allows_select() -> None:
    sql = assert_read_only_sql("SELECT amount FROM orders", db_type="postgres")
    assert "SELECT" in sql.upper()
