"""Confirmed semantic rows survive rediscovery unchanged."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Metric
from app.services.schema_discovery_service import run_schema_discovery
from app.services.semantic_discovery import discover_semantic_suggestions
from tests.test_helpers_discovery import (
    StubMultiSchemaProvider,
    disable_semantic_llm,
    make_test_db,
    patch_provider,
    seed_connection,
)


def test_confirmed_metric_expression_and_label_preserved(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)

    initial_tables = {
        "tables": [
            {
                "schema_name": "acct_us",
                "table_name": "orders",
                "columns": [
                    {
                        "name": "revenue_amt",
                        "data_type": "numeric",
                        "nullable": False,
                        "is_pk": False,
                    }
                ],
                "foreign_keys": [],
            }
        ]
    }
    provider = StubMultiSchemaProvider(initial_tables)
    patch_provider(monkeypatch, provider)
    disable_semantic_llm(monkeypatch)

    run_schema_discovery(db, connection_id=conn.id, tenant_id=tenant.id, scope="all")
    discover_semantic_suggestions(db, conn.id, tenant.id)

    metric = db.scalar(
        select(Metric).where(
            Metric.tenant_id == tenant.id,
            Metric.connection_id == conn.id,
        )
    )
    assert metric is not None

    metric.status = "confirmed"
    metric.name = "Custom Revenue"
    metric.expression = "SUM(acct_us.orders.revenue_amt) * 1.1"
    metric.label = "Adjusted Revenue"
    db.commit()

    updated_tables = {
        "tables": [
            {
                "schema_name": "acct_us",
                "table_name": "orders",
                "columns": [
                    {
                        "name": "revenue_amt",
                        "data_type": "numeric",
                        "nullable": False,
                        "is_pk": False,
                    },
                    {
                        "name": "region_code",
                        "data_type": "varchar",
                        "nullable": True,
                        "is_pk": False,
                    },
                ],
                "foreign_keys": [],
            }
        ]
    }
    provider = StubMultiSchemaProvider(updated_tables)
    patch_provider(monkeypatch, provider)

    run_schema_discovery(db, connection_id=conn.id, tenant_id=tenant.id, scope="all")
    discover_semantic_suggestions(db, conn.id, tenant.id)

    db.refresh(metric)
    assert metric.name == "Custom Revenue"
    assert metric.expression == "SUM(acct_us.orders.revenue_amt) * 1.1"
    assert metric.label == "Adjusted Revenue"
    assert metric.status == "confirmed"
    assert metric.physical_schema == "acct_us"
    assert metric.physical_table == "orders"
    assert metric.physical_column == "revenue_amt"
