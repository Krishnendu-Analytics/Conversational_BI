"""Tests for the single-domain query tool (app.agent.domain_tool).

Collaborators are monkeypatched at the module level (domain_tool imports
them by name from workflow.py/query_plan.py/etc.) rather than re-driving
the full real SQL-generation pipeline — those building blocks already have
their own dedicated coverage elsewhere; these tests verify run_domain_query
calls them in the right order and maps results into DomainEvidence
correctly, including every error path an autonomous investigator would
need to handle without a human to ask.
"""

from __future__ import annotations

from app.agent import domain_tool
from app.agent.domain_catalog import Domain
from app.agent.query_plan import QueryPlan
from app.agent.query_plan_validation import (
    PlanValidationResult,
    QueryClarification,
)

_SALES_DOMAIN = Domain(name="Sales", table="public.fact_sales")


class _FakeSchemaCache:
    def __init__(self, schema):
        self.schema = schema

    def get(self, tenant_id, connection_id):
        return self.schema


class _FakeSemanticCache:
    def __init__(self, catalog):
        self.catalog = catalog

    def get(self, tenant_id, connection_id=None):
        return self.catalog


class _FakeSafetyPipeline:
    def __init__(self, *, schema, db_type):
        self.schema = schema
        self.db_type = db_type

    def validate(self, sql):
        return sql


def _schema() -> dict:
    return {
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "fact_sales",
                "columns": [{"name": "amount", "data_type": "numeric"}],
                "foreign_keys": [],
            }
        ],
    }


def _plan() -> QueryPlan:
    return QueryPlan(intent="aggregation", metrics=["Total Sales"])


def _patch_common(monkeypatch, *, schema=None, catalog=None):
    monkeypatch.setattr(
        domain_tool,
        "resolve_schema_for_connection",
        lambda *_a, **_k: schema if schema is not None else _schema(),
    )
    monkeypatch.setattr(
        domain_tool,
        "resolve_semantic_for_connection",
        lambda *_a, **_k: catalog if catalog is not None else {},
    )
    monkeypatch.setattr(
        domain_tool, "filter_confirmed_semantic_catalog", lambda c: c or {}
    )
    monkeypatch.setattr(domain_tool, "SchemaCache", lambda: _FakeSchemaCache(schema))
    monkeypatch.setattr(domain_tool, "SemanticCache", lambda: _FakeSemanticCache(catalog))
    # Tier 2 (the schema-grounded LLM SQL tier) defaults to "nothing" so a
    # test that isn't specifically exercising it can't accidentally reach a
    # real LLM API — every test below that cares about tier 2 overrides
    # this explicitly.
    monkeypatch.setattr(
        domain_tool, "generate_schema_grounded_sql", lambda **_k: (None, False)
    )


def test_run_domain_query_missing_schema_is_an_error(monkeypatch) -> None:
    _patch_common(monkeypatch, schema=None)
    monkeypatch.setattr(domain_tool, "resolve_schema_for_connection", lambda *_a, **_k: None)

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert not evidence.ok
    assert "No discovered schema" in evidence.error


def test_run_domain_query_missing_db_type_is_an_error(monkeypatch) -> None:
    _patch_common(
        monkeypatch,
        schema={
            "db_type": "",
            "tables": [
                {
                    "schema_name": "public",
                    "table_name": "fact_sales",
                    "columns": [],
                    "foreign_keys": [],
                }
            ],
        },
    )

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert not evidence.ok
    assert "Database type is missing" in evidence.error


def test_run_domain_query_no_structured_plan_is_an_error_when_tier_2_also_fails(
    monkeypatch,
) -> None:
    """Tier 1 (structured plan) unavailable, tier 2 (schema-grounded LLM
    SQL) also produces nothing by default (see _patch_common) -> the
    generic "language model didn't return usable SQL" message from tier 3,
    not a tier-1-specific one — tier 1's specific failure reason is
    deliberately discarded once we fall through, since tier 2 gets a clean
    shot at the question instead of this domain giving up on tier 1 alone.
    """
    _patch_common(monkeypatch)
    monkeypatch.setattr(domain_tool, "parse_query_plan", lambda *_a, **_k: None)

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert not evidence.ok
    assert "usable SQL" in evidence.error


def test_run_domain_query_no_plan_falls_through_to_schema_grounded_tier(monkeypatch) -> None:
    """The actual fix this file exists to lock in: when tier 1 can't
    resolve a structured plan, tier 2 (schema-grounded LLM SQL) gets a real
    shot at the question instead of the domain giving up immediately."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(domain_tool, "parse_query_plan", lambda *_a, **_k: None)

    sql = "SELECT unit_name, SUM(amount) AS total FROM fact_sales GROUP BY unit_name"
    monkeypatch.setattr(
        domain_tool, "generate_schema_grounded_sql", lambda **_k: (sql, False)
    )
    monkeypatch.setattr(domain_tool, "ensure_valid_sql", lambda *_a, **_k: (sql, None, None))
    monkeypatch.setattr(domain_tool, "SQLSafetyPipeline", _FakeSafetyPipeline)
    monkeypatch.setattr(
        domain_tool, "run_readonly_query", lambda *_a, **_k: [{"unit_name": "PP", "total": 5}]
    )

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert evidence.ok
    assert evidence.sql == sql


def test_run_domain_query_invalid_plan_falls_through_to_schema_grounded_tier(
    monkeypatch,
) -> None:
    """Tier 1 rejects the structured plan (e.g. an ambiguous field) —
    tier 2 still gets a real shot rather than surfacing that rejection as
    the domain's final answer."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(domain_tool, "parse_query_plan", lambda *_a, **_k: _plan())
    monkeypatch.setattr(domain_tool, "normalize_query_plan", lambda plan, **_k: plan)
    monkeypatch.setattr(
        domain_tool,
        "validate_query_plan",
        lambda *_a, **_k: PlanValidationResult(
            ok=False,
            clarification=QueryClarification(kind="pick_metric", message="Which metric?"),
        ),
    )

    sql = "SELECT SUM(amount) AS total FROM fact_sales"
    monkeypatch.setattr(
        domain_tool, "generate_schema_grounded_sql", lambda **_k: (sql, False)
    )
    monkeypatch.setattr(domain_tool, "ensure_valid_sql", lambda *_a, **_k: (sql, None, None))
    monkeypatch.setattr(domain_tool, "SQLSafetyPipeline", _FakeSafetyPipeline)
    monkeypatch.setattr(domain_tool, "run_readonly_query", lambda *_a, **_k: [{"total": 100}])

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert evidence.ok
    assert evidence.sql == sql


def test_run_domain_query_compile_failure_falls_through_to_schema_grounded_tier(
    monkeypatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(domain_tool, "parse_query_plan", lambda *_a, **_k: _plan())
    monkeypatch.setattr(domain_tool, "normalize_query_plan", lambda plan, **_k: plan)
    monkeypatch.setattr(
        domain_tool, "validate_query_plan", lambda *_a, **_k: PlanValidationResult(ok=True)
    )

    def _boom(*_a, **_k):
        raise ValueError("no fact table")

    monkeypatch.setattr(domain_tool, "compile_query_plan", _boom)

    sql = "SELECT SUM(amount) AS total FROM fact_sales"
    monkeypatch.setattr(
        domain_tool, "generate_schema_grounded_sql", lambda **_k: (sql, False)
    )
    monkeypatch.setattr(domain_tool, "ensure_valid_sql", lambda *_a, **_k: (sql, None, None))
    monkeypatch.setattr(domain_tool, "SQLSafetyPipeline", _FakeSafetyPipeline)
    monkeypatch.setattr(domain_tool, "run_readonly_query", lambda *_a, **_k: [{"total": 100}])

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert evidence.ok
    assert evidence.sql == sql


def test_run_domain_query_tier_2_no_matching_data_is_an_error(monkeypatch) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(domain_tool, "parse_query_plan", lambda *_a, **_k: None)
    monkeypatch.setattr(
        domain_tool, "generate_schema_grounded_sql", lambda **_k: (None, True)
    )

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert not evidence.ok
    assert "doesn't appear to be relevant" in evidence.error


def _patch_through_compile(monkeypatch, sql="SELECT SUM(amount) AS total_sales FROM fact_sales"):
    _patch_common(monkeypatch)
    monkeypatch.setattr(domain_tool, "parse_query_plan", lambda *_a, **_k: _plan())
    monkeypatch.setattr(domain_tool, "normalize_query_plan", lambda plan, **_k: plan)
    monkeypatch.setattr(
        domain_tool, "validate_query_plan", lambda *_a, **_k: PlanValidationResult(ok=True)
    )
    monkeypatch.setattr(domain_tool, "compile_query_plan", lambda *_a, **_k: sql)
    monkeypatch.setattr(domain_tool, "clean_generated_sql", lambda s: s)
    monkeypatch.setattr(domain_tool, "postprocess_generated_sql", lambda s, _dt: s)
    monkeypatch.setattr(domain_tool, "SQLSafetyPipeline", _FakeSafetyPipeline)


def test_run_domain_query_generation_error_is_surfaced(monkeypatch) -> None:
    _patch_through_compile(monkeypatch)
    monkeypatch.setattr(
        domain_tool, "ensure_valid_sql", lambda *_a, **_k: (None, "LLM produced no SQL", None)
    )

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert not evidence.ok
    assert evidence.error == "LLM produced no SQL"


def test_run_domain_query_happy_path_returns_grounded_evidence(monkeypatch) -> None:
    sql = "SELECT SUM(amount) AS total_sales FROM fact_sales"
    _patch_through_compile(monkeypatch, sql=sql)
    monkeypatch.setattr(domain_tool, "ensure_valid_sql", lambda *_a, **_k: (sql, None, None))
    monkeypatch.setattr(
        domain_tool, "run_readonly_query", lambda *_a, **_k: [{"total_sales": 100}]
    )

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert evidence.ok
    assert evidence.error is None
    assert evidence.sql == sql
    assert evidence.fallback_label is None
    assert evidence.facts is not None
    assert evidence.facts.row_count == 1


def test_run_domain_query_execution_error_non_retryable_stops(monkeypatch) -> None:
    sql = "SELECT SUM(amount) AS total_sales FROM fact_sales"
    _patch_through_compile(monkeypatch, sql=sql)
    monkeypatch.setattr(domain_tool, "ensure_valid_sql", lambda *_a, **_k: (sql, None, None))

    def _raise(*_a, **_k):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(domain_tool, "run_readonly_query", _raise)
    monkeypatch.setattr(domain_tool, "is_non_retryable_execution_error", lambda exc: True)
    monkeypatch.setattr(domain_tool, "user_facing_execution_error", lambda exc: "Access denied")

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert not evidence.ok
    assert evidence.error == "Access denied"


def test_run_domain_query_execution_error_recovers_via_fallback(monkeypatch) -> None:
    sql = "SELECT SUM(bad_col) AS total_sales FROM fact_sales"
    recovered_sql = "SELECT COUNT(*) AS row_count FROM fact_sales"
    _patch_through_compile(monkeypatch, sql=sql)
    monkeypatch.setattr(domain_tool, "ensure_valid_sql", lambda *_a, **_k: (sql, None, None))
    monkeypatch.setattr(domain_tool, "is_non_retryable_execution_error", lambda exc: False)
    monkeypatch.setattr(
        domain_tool,
        "recover_sql_after_execution_error",
        lambda **_k: (recovered_sql, "Sales record count"),
    )

    calls = {"n": 0}

    def _run(_conn, sql_arg, **_k):
        calls["n"] += 1
        if sql_arg == sql:
            raise RuntimeError("column bad_col does not exist")
        return [{"row_count": 5}]

    monkeypatch.setattr(domain_tool, "run_readonly_query", _run)

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert evidence.ok
    assert evidence.sql == recovered_sql
    assert evidence.fallback_label == "Sales record count"
    assert calls["n"] == 2


def test_run_domain_query_execution_error_no_recovery_is_an_error(monkeypatch) -> None:
    sql = "SELECT SUM(bad_col) AS total_sales FROM fact_sales"
    _patch_through_compile(monkeypatch, sql=sql)
    monkeypatch.setattr(domain_tool, "ensure_valid_sql", lambda *_a, **_k: (sql, None, None))
    monkeypatch.setattr(domain_tool, "is_non_retryable_execution_error", lambda exc: False)
    monkeypatch.setattr(
        domain_tool, "recover_sql_after_execution_error", lambda **_k: (None, None)
    )

    def _raise(*_a, **_k):
        raise RuntimeError("column bad_col does not exist")

    monkeypatch.setattr(domain_tool, "run_readonly_query", _raise)

    evidence = domain_tool.run_domain_query(
        "total sales", tenant_id="00000000-0000-0000-0000-000000000001", connection_id="c1", domain=_SALES_DOMAIN
    )

    assert not evidence.ok
    assert "Query execution failed" in evidence.error


def test_run_domain_query_never_leaks_into_a_different_domains_table(monkeypatch) -> None:
    """Regression test for a real observed bug: with no confirmed catalog
    and an unresolved plan, the query compiler's own fallback picks the
    *first* table in whatever schema it's given. Before domain scoping,
    every domain call got the full multi-table schema, so every domain
    silently queried the same first table — six different domain names in
    an investigation report, one identical row count underneath all of
    them. This drives the REAL (unmocked) parse/compile pipeline against a
    two-table schema and checks the "Production" domain's SQL only ever
    references fact_production, never fact_sales (which is listed first).
    """
    multi_table_schema = {
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "fact_sales",
                "columns": [{"name": "amount", "data_type": "numeric"}],
                "foreign_keys": [],
            },
            {
                "schema_name": "public",
                "table_name": "fact_production",
                "columns": [{"name": "output", "data_type": "numeric"}],
                "foreign_keys": [],
            },
        ],
    }
    monkeypatch.setattr(
        domain_tool, "resolve_schema_for_connection", lambda *_a, **_k: multi_table_schema
    )
    monkeypatch.setattr(domain_tool, "resolve_semantic_for_connection", lambda *_a, **_k: {})
    monkeypatch.setattr(domain_tool, "filter_confirmed_semantic_catalog", lambda c: c or {})

    class StubLLM:
        """No .client attribute — parse_query_plan takes the heuristic
        path (no catalog to match, so an empty/unresolved plan), and any
        raw-text repair/fallback call returns an unusable stub string,
        same as the real LLMClient with no LLM_API_KEY configured."""

        def complete(self, prompt: str) -> str:
            return "[llm stub] missing LLM_API_KEY"

    production_domain = Domain(name="Production", table="public.fact_production")
    monkeypatch.setattr(
        domain_tool, "run_readonly_query", lambda *_a, **_k: [{"output": 100}]
    )

    evidence = domain_tool.run_domain_query(
        "why did actual production fall below the monthly target",
        tenant_id="00000000-0000-0000-0000-000000000001",
        connection_id="c1",
        domain=production_domain,
        llm=StubLLM(),
    )

    assert evidence.sql is not None
    assert "fact_production" in evidence.sql.lower()
    assert "fact_sales" not in evidence.sql.lower()
