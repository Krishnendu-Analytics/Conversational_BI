"""End-to-end multi-schema discovery → semantic → SQL compilation pipeline."""

from __future__ import annotations

from sqlalchemy import select

from app.agent.query_plan import QueryPlan
from app.agent.sql_compiler import compile_query_plan
from app.models import Dimension, Metric
from app.services.schema_discovery_service import run_schema_discovery
from app.services.semantic_discovery import discover_semantic_suggestions
from app.services.semantic_service import load_connection_semantic_catalog
from app.tools.sql_safety import SQLSafetyPipeline
from tests.test_helpers_discovery import (
    StubMultiSchemaProvider,
    confirm_dimension,
    confirm_metric,
    disable_semantic_llm,
    make_test_db,
    multi_schema_tables,
    patch_provider,
    seed_connection,
)


def test_multi_schema_e2e_pipeline(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db, tenant_name="MultiSchemaCo")

    provider = StubMultiSchemaProvider(multi_schema_tables())
    patch_provider(monkeypatch, provider)
    disable_semantic_llm(monkeypatch)

    discovery = run_schema_discovery(
        db,
        connection_id=conn.id,
        tenant_id=tenant.id,
        scope="all",
    )
    assert discovery.status == "completed"
    assert set(discovery.schemas_used) == {"acct_us", "billing_eu"}
    assert len(discovery.tables) == 3

    bare_names = [t["table_name"] for t in discovery.tables]
    assert bare_names.count("orders") == 2

    discover_semantic_suggestions(db, conn.id, tenant.id)

    revenue = db.scalar(
        select(Metric).where(
            Metric.tenant_id == tenant.id,
            Metric.connection_id == conn.id,
            Metric.source_column_ref == "acct_us.orders.revenue_amt",
        )
    )
    region_dim = db.scalar(
        select(Dimension).where(
            Dimension.tenant_id == tenant.id,
            Dimension.connection_id == conn.id,
            Dimension.source_column_ref == "billing_eu.invoices.region",
        )
    )
    assert revenue is not None
    assert region_dim is not None
    assert revenue.status == "suggested"
    assert region_dim.status == "suggested"

    confirm_metric(db, revenue.id)
    confirm_dimension(db, region_dim.id)

    catalog = load_connection_semantic_catalog(db, tenant.id, conn.id)
    schema = {
        "connection_id": str(conn.id),
        "db_type": conn.db_type,
        "tables": discovery.tables,
    }

    # Ask for the confirmed rows by their own names (suggestions are named
    # after their columns, not a generic "Revenue"/"Region" category).
    plan = QueryPlan(
        intent="aggregation",
        metrics=[revenue.name],
        dimensions=[region_dim.name],
    )
    sql = compile_query_plan(plan, catalog, schema, conn.db_type)

    assert "JOIN" in sql.upper()
    assert "acct_us.orders" in sql
    assert "billing_eu.invoices" in sql
    assert "invoice_id" in sql

    validated = SQLSafetyPipeline(schema=schema).validate(sql)
    assert validated.startswith("SELECT")

    rows = provider.execute_read_only_query(validated)
    assert rows == [{"region": "EMEA", "revenue": 1000.0}]
