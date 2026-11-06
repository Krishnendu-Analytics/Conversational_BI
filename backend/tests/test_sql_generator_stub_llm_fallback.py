"""Regression test: SQLGenerator must still answer when the LLM is a stub
(no LLM_API_KEY configured) and the connection has no confirmed semantic
catalog — this is exactly the state a fresh connection is in, and it must
not dead-end on a clarification when the deterministic fallback could
answer instead.

Root cause this locks in: LLMClient.complete() returns a "[llm stub] ..."
placeholder when no API key is set; _clean_sql() correctly discards that,
leaving `sql` empty. SQLGenerator.run then returned a clarification
directly — _ensure_valid_sql (the only place the deterministic
_build_fallback_choice fallback ran) was never reached, because that
function's own empty-sql check only paid off when it was actually called.
Fixed by attempting _build_fallback_choice before giving up.
"""

from __future__ import annotations

from app.agent.workflow import SQLGenerator

from tests.test_sql_generator import FakeSchemaCache, FakeSemanticCache


def _stub_llm_response() -> str:
    return "[llm stub] missing LLM_API_KEY (prompt length=123)"


class StubLLM:
    """Mirrors the real LLMClient's behavior with no API key configured:
    every .complete() call returns the same stub placeholder, regardless
    of prompt content."""

    def complete(self, prompt: str) -> str:
        return _stub_llm_response()


def _schema_with_numeric_metric() -> dict:
    return {
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "fact_sales",
                "columns": [
                    {"name": "sale_id", "data_type": "integer", "is_pk": True},
                    {"name": "amount", "data_type": "numeric"},
                ],
                "foreign_keys": [],
            }
        ],
    }


def test_sql_generator_falls_back_when_llm_is_a_stub_and_no_catalog() -> None:
    schema = _schema_with_numeric_metric()
    generator = SQLGenerator(
        schema_cache=FakeSchemaCache(schema),
        semantic_cache=FakeSemanticCache({}),
        llm=StubLLM(),
    )

    result = generator.run(
        {
            "tenant_id": "tenant-123",
            "connection_id": "connection-123",
            "question": "What were total sales last quarter?",
        }
    )

    # Must not dead-end on a clarification when a schema-safe deterministic
    # query is available — the whole point of the fallback.
    assert result.get("query_clarification") is None
    assert result["sql"] is not None
    assert "fact_sales" in result["sql"].lower()
    assert result.get("used_fallback_sql_label")


def test_sql_generator_still_clarifies_when_schema_has_no_tables() -> None:
    """The fallback genuinely has nothing to offer here — this must still
    produce a clarification, not crash or silently invent a table."""
    generator = SQLGenerator(
        schema_cache=FakeSchemaCache({"db_type": "postgres", "tables": []}),
        semantic_cache=FakeSemanticCache({}),
        llm=StubLLM(),
    )

    result = generator.run(
        {
            "tenant_id": "tenant-123",
            "connection_id": "connection-123",
            "question": "What were total sales last quarter?",
        }
    )

    assert result.get("query_clarification") is not None
    assert result.get("sql") is None
