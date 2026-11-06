"""Partial schema discovery when some schemas are inaccessible."""

from __future__ import annotations

from tests.test_helpers_discovery import (
    StubMultiSchemaProvider,
    make_test_db,
    patch_provider,
    seed_connection,
)
from app.services.schema_discovery_service import run_schema_discovery


def test_partial_discovery_skips_denied_schema(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)

    tables = {
        "tables": [
            {
                "schema_name": "acct_us",
                "table_name": "orders",
                "columns": [
                    {
                        "name": "order_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    }
                ],
                "foreign_keys": [],
            },
            {
                "schema_name": "billing_eu",
                "table_name": "invoices",
                "columns": [
                    {
                        "name": "invoice_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    }
                ],
                "foreign_keys": [],
            },
        ]
    }
    provider = StubMultiSchemaProvider(tables, deny_schemas={"billing_eu"})
    patch_provider(monkeypatch, provider)

    result = run_schema_discovery(
        db,
        connection_id=conn.id,
        tenant_id=tenant.id,
        scope="all",
    )

    assert result.status == "partial"
    assert len(result.tables) == 1
    assert result.tables[0]["schema_name"] == "acct_us"
    assert any("billing_eu" in warning for warning in result.warnings)
    db.refresh(conn)
    assert conn.last_discover_status == "partial"
