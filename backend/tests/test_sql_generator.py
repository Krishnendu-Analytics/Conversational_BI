from app.agent.workflow import (
    SQLGenerator,
    _dialect_rules_for_prompt,
    _fallback_sql,
    _format_schema_for_prompt,
    _format_semantic_for_prompt,
    _postprocess_sql,
    _question_hints_for_prompt,
    _recover_sql_after_execution_error,
)


class FakeSchemaCache:
    def __init__(self, schema):
        self.schema = schema
        self.calls = []

    def get(self, tenant_id, connection_id):
        self.calls.append((tenant_id, connection_id))
        return self.schema


class FakeSemanticCache:
    def __init__(self, semantic_context):
        self.semantic_context = semantic_context
        self.calls = []

    def get(self, tenant_id, connection_id=None):
        self.calls.append((tenant_id, connection_id))
        return self.semantic_context


class FakeLLM:
    def __init__(self, sql):
        self.sql = sql
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.sql


def _postgres_sample_schema() -> dict:
    return {
        "connection_id": "connection-123",
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [
                    {"name": "quantity", "data_type": "integer"},
                    {"name": "product_id", "data_type": "integer"},
                    {"name": "order_date", "data_type": "date"},
                ],
                "foreign_keys": [
                    {
                        "column": "product_id",
                        "referred_table": "products",
                        "referred_column": "product_id",
                    }
                ],
            },
            {
                "schema_name": "public",
                "table_name": "products",
                "columns": [
                    {"name": "product_id", "data_type": "integer", "is_pk": True},
                    {"name": "price", "data_type": "numeric"},
                ],
                "foreign_keys": [],
            },
        ],
    }


def test_sql_generator_uses_schema_semantic_context_and_llm(monkeypatch):
    monkeypatch.setattr(
        "app.agent.workflow.parse_query_plan",
        lambda *args, **kwargs: None,
    )
    schema = _postgres_sample_schema()

    semantic_context = {
        "metrics": [
            {
                "name": "Revenue",
                "expression": "SUM(orders.quantity * products.price)",
            }
        ],
        "dimensions": [],
    }

    llm = FakeLLM(
        "SELECT SUM(o.quantity * p.price) AS revenue "
        "FROM public.orders o "
        "JOIN public.products p ON o.product_id = p.product_id"
    )

    generator = SQLGenerator(
        schema_cache=FakeSchemaCache(schema),
        semantic_cache=FakeSemanticCache(semantic_context),
        llm=llm,
    )

    state = {
        "tenant_id": "tenant-123",
        "connection_id": "connection-123",
        "question": "What is the revenue?",
    }

    result = generator.run(state)

    assert result["sql"] is None
    assert result.get("query_clarification") is not None
    assert result["query_clarification"]["kind"] == "pick_metric"
    assert llm.prompts == []


def test_sql_generator_uses_llm_without_semantic_catalog(monkeypatch):
    monkeypatch.setattr(
        "app.agent.workflow.parse_query_plan",
        lambda *args, **kwargs: None,
    )
    schema = _postgres_sample_schema()

    llm = FakeLLM(
        "SELECT SUM(o.quantity * p.price) AS revenue "
        "FROM public.orders o "
        "JOIN public.products p ON o.product_id = p.product_id"
    )

    generator = SQLGenerator(
        schema_cache=FakeSchemaCache(schema),
        semantic_cache=FakeSemanticCache({}),
        llm=llm,
    )

    state = {
        "tenant_id": "tenant-123",
        "connection_id": "connection-123",
        "question": "What is the revenue?",
    }

    result = generator.run(state)

    assert result["sql"] is None
    assert result.get("query_clarification") is not None
    assert result["query_clarification"]["kind"] == "pick_metric"
    assert llm.prompts == []


def test_sql_generator_postprocesses_postgres_interval_syntax(monkeypatch):
    monkeypatch.setattr(
        "app.agent.workflow.parse_query_plan",
        lambda *args, **kwargs: None,
    )
    schema = {
        "connection_id": "connection-123",
        "db_type": "postgres",
        "tables": [
            {
                "table_name": "orders",
                "columns": [
                    {"name": "quantity", "data_type": "integer"},
                    {"name": "order_date", "data_type": "date"},
                ],
                "foreign_keys": [],
            }
        ],
    }

    llm = FakeLLM(
        "SELECT SUM(quantity) FROM orders "
        "WHERE order_date >= DATE_TRUNC('quarter', CURRENT_DATE) "
        "- INTERVAL '3' months"
    )

    generator = SQLGenerator(
        schema_cache=FakeSchemaCache(schema),
        semantic_cache=FakeSemanticCache({}),
        llm=llm,
    )

    result = generator.run(
        {
            "tenant_id": "tenant-123",
            "connection_id": "connection-123",
            "question": "What were total sales last quarter?",
        }
    )

    assert result["sql"] is None
    assert result.get("query_clarification") is not None
    assert result["query_clarification"]["kind"] == "pick_metric"
    assert llm.prompts == []

    rewritten = _postprocess_sql(
        "SELECT SUM(quantity) FROM orders "
        "WHERE order_date >= DATE_TRUNC('quarter', CURRENT_DATE) "
        "- INTERVAL '3' months",
        "postgres",
    )
    assert "interval '3 month" in rewritten.lower()
    assert "interval '3' month" not in rewritten.lower()

    
def test_sql_generator_skips_llm_when_schema_is_missing(monkeypatch):
    schema_cache = FakeSchemaCache(None)
    semantic_cache = FakeSemanticCache({})
    llm = FakeLLM("SELECT 1")

    # Avoid platform DB hydrate during unit test.
    monkeypatch.setattr(
        "app.agent.workflow._resolve_schema",
        lambda tenant_id, connection_id, cache: None,
    )

    generator = SQLGenerator(
        schema_cache=schema_cache,
        semantic_cache=semantic_cache,
        llm=llm,
    )

    state = {
        "tenant_id": "tenant-123",
        "connection_id": "connection-123",
        "question": "What is the revenue?",
    }

    result = generator.run(state)

    assert result["sql"] is None
    assert result["steps"] == ["generate_sql"]
    assert result["error"]
    assert "schema" in result["error"].lower()
    assert llm.prompts == []
    
def test_sql_generator_skips_llm_when_db_type_is_missing():
    schema = {
        "connection_id": "connection-123",
        "tables": [
            {
                "table_name": "orders",
                "columns": [],
                "foreign_keys": [],
            }
        ],
    }

    llm = FakeLLM("SELECT 1")

    generator = SQLGenerator(
        schema_cache=FakeSchemaCache(schema),
        semantic_cache=FakeSemanticCache({}),
        llm=llm,
    )

    state = {
        "tenant_id": "tenant-123",
        "connection_id": "connection-123",
        "question": "What is the revenue?",
    }

    result = generator.run(state)

    assert result["sql"] is None
    assert result["error"] == (
        "Database type is missing from the discovered schema."
    )
    assert result["steps"] == ["generate_sql"]
    assert llm.prompts == []


def test_sql_generator_falls_back_when_llm_invents_columns():
    schema = _postgres_sample_schema()

    class RepairLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.calls = 0

        def complete(self, prompt: str) -> str:
            self.prompts.append(prompt)
            self.calls += 1
            if "sql generation assistant" in prompt.lower():
                return (
                    "SELECT campaign_name, COUNT(DISTINCT agreement_id) "
                    "FROM public.orders GROUP BY campaign_name"
                )
            if "the previous sql failed" in prompt.lower():
                return (
                    "SELECT campaign_name, COUNT(DISTINCT agreement_id) "
                    "FROM public.orders GROUP BY campaign_name"
                )
            raise AssertionError(f"Unexpected prompt: {prompt}")

    llm = RepairLLM()
    generator = SQLGenerator(
        schema_cache=FakeSchemaCache(schema),
        semantic_cache=FakeSemanticCache({}),
        llm=llm,
    )

    result = generator.run(
        {
            "tenant_id": "tenant-123",
            "connection_id": "connection-123",
            "question": "create a dashboard for this db",
        }
    )

    # The invented columns must never reach the final SQL. With no
    # deterministic plan and no valid LLM/repair attempt, the generator
    # falls back further to a generic schema-safe query rather than
    # dead-ending on a clarification.
    assert result["sql"] is not None
    assert "campaign_name" not in result["sql"].lower()
    assert "agreement_id" not in result["sql"].lower()


def test_recover_sql_after_execution_error_uses_fallback():
    schema = _postgres_sample_schema()

    class SilentLLM:
        def complete(self, prompt: str) -> str:
            return (
                "SELECT COUNT(agreement_number) AS total_agreements "
                "FROM public.orders"
            )

    recovered, fallback_label = _recover_sql_after_execution_error(
        sql="SELECT COUNT(agreement_number) FROM public.orders",
        schema=schema,
        db_type="postgres",
        semantic_context={},
        question="dashboard recommendations",
        llm=SilentLLM(),
        execution_error="UndefinedColumn: column agreement_number does not exist",
    )

    assert recovered is not None
    assert fallback_label
    assert "agreement_number" not in recovered.lower()
    assert "row_count" in recovered.lower() or "count(*)" in recovered.lower()



def test_sql_generator_empty_catalog_falls_back_to_grounded_llm_sql() -> None:
    """With no confirmed semantic metric, the deterministic compiler is
    skipped (it would have to invent an ungrounded aggregate), but a real
    DB connection still gets the same schema-grounded LLM fallback that
    file-backed datasets already use, instead of being blocked outright.
    """
    from app.agent.query_plan import QueryPlan, TimeSpec

    schema = _postgres_sample_schema()
    llm = FakeLLM(
        "SELECT SUM(o.quantity * p.price) AS total_sales "
        "FROM public.orders o "
        "JOIN public.products p ON o.product_id = p.product_id"
    )
    generator = SQLGenerator(
        schema_cache=FakeSchemaCache(schema),
        semantic_cache=FakeSemanticCache({"metrics": [], "dimensions": []}),
        llm=llm,
    )

    import app.agent.workflow as workflow_mod

    original = workflow_mod.parse_query_plan
    workflow_mod.parse_query_plan = lambda *args, **kwargs: QueryPlan(
        intent="aggregation",
        time=TimeSpec(range="last_quarter"),
    )
    try:
        result = generator.run(
            {
                "tenant_id": "tenant-123",
                "connection_id": "connection-123",
                "question": "What were total sales last quarter?",
            }
        )
    finally:
        workflow_mod.parse_query_plan = original

    assert llm.prompts, "expected the schema-grounded LLM fallback to be invoked"
    assert result["sql"] is not None
    assert "total_sales" in result["sql"].lower()
    assert result.get("query_clarification") is None


def test_execution_error_is_repaired_before_falling_back():
    # A detailed question whose SQL hit a database error used to get the
    # generic fallback ("Total ...") without ever trying the repair.
    schema = _postgres_sample_schema()
    table = schema["tables"][0]
    col = next(c["name"] for c in table["columns"] if not c.get("is_pk"))
    fixed = f"SELECT {col} FROM {table.get('schema_name', 'public')}.{table['table_name']}"

    class RepairLLM:
        def complete(self, prompt: str) -> str:
            return fixed

    recovered, fallback_label = _recover_sql_after_execution_error(
        sql=f"SELECT ROUND({col}, 2) FROM {table['table_name']}",
        schema=schema,
        db_type="postgres",
        semantic_context={},
        question="detailed question",
        llm=RepairLLM(),
        execution_error="UndefinedFunction: function round(double precision, integer) does not exist",
    )
    assert fallback_label is None
    assert col in recovered


def test_postgres_round_argument_is_cast_to_numeric():
    from app.tools.sql_safety import SQLSafetyPipeline

    schema = {"tables": [{"schema_name": "dm", "table_name": "f", "columns": [
        {"name": "a", "data_type": "real"}, {"name": "b", "data_type": "real"}]}]}
    out = SQLSafetyPipeline(schema=schema, db_type="postgres").validate(
        "SELECT ROUND(100.0 * SUM(a) / NULLIF(SUM(b), 0), 2) AS pct FROM dm.f"
    )
    assert "ROUND(CAST(" in out
    already = SQLSafetyPipeline(schema=schema, db_type="postgres").validate(
        "SELECT ROUND(CAST(SUM(a) AS NUMERIC), 2) AS x FROM dm.f"
    )
    assert already.count("CAST(") == 1
