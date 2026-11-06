"""Connection-scoped semantic catalog isolation."""

from __future__ import annotations

from sqlalchemy import select

from app.encryption import CredentialEncryption
from app.models import Dimension, Metric
from app.services.schema_discovery_service import run_schema_discovery
from app.services.semantic_discovery import discover_semantic_suggestions
from app.services.semantic_service import load_connection_semantic_catalog
from tests.test_helpers_discovery import (
    StubMultiSchemaProvider,
    disable_semantic_llm,
    make_test_db,
    patch_provider,
    seed_connection,
)


def _conn_tables(schema_name: str, suffix: str) -> dict:
    return {
        "tables": [
            {
                "schema_name": schema_name,
                "table_name": f"fact_{suffix}",
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


def test_semantic_catalogs_are_disjoint_per_connection(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn_a = seed_connection(db, conn_name="Conn A")
    _, conn_b = seed_connection(db, conn_name="Conn B", tenant=tenant)

    tables_a = _conn_tables("warehouse_a", "a")
    tables_b = _conn_tables("warehouse_b", "b")

    def factory(db_type: str, url: str) -> StubMultiSchemaProvider:
        if "conn-a" in url:
            return StubMultiSchemaProvider(tables_a)
        return StubMultiSchemaProvider(tables_b)

    monkeypatch.setattr("app.services.schema_discovery_service.ProviderFactory.create", factory)
    disable_semantic_llm(monkeypatch)

    encryptor = CredentialEncryption()
    conn_a.encrypted_url = encryptor.encrypt("postgresql://reader:reader@localhost/conn-a")
    conn_b.encrypted_url = encryptor.encrypt("postgresql://reader:reader@localhost/conn-b")
    db.commit()

    run_schema_discovery(db, connection_id=conn_a.id, tenant_id=tenant.id, scope="all")
    run_schema_discovery(db, connection_id=conn_b.id, tenant_id=tenant.id, scope="all")

    discover_semantic_suggestions(db, conn_a.id, tenant.id)
    discover_semantic_suggestions(db, conn_b.id, tenant.id)

    metrics_a = db.scalars(
        select(Metric).where(
            Metric.tenant_id == tenant.id,
            Metric.connection_id == conn_a.id,
        )
    ).all()
    metrics_b = db.scalars(
        select(Metric).where(
            Metric.tenant_id == tenant.id,
            Metric.connection_id == conn_b.id,
        )
    ).all()
    assert metrics_a
    assert metrics_b
    assert all(m.connection_id == conn_a.id for m in metrics_a)
    assert all(m.connection_id == conn_b.id for m in metrics_b)

    refs_a = {m.source_column_ref for m in metrics_a}
    refs_b = {m.source_column_ref for m in metrics_b}
    assert refs_a.isdisjoint(refs_b)

    catalog_a = load_connection_semantic_catalog(
        db, tenant.id, conn_a.id, include_suggested=True
    )
    catalog_b = load_connection_semantic_catalog(
        db, tenant.id, conn_b.id, include_suggested=True
    )
    assert catalog_a["metrics"]
    assert catalog_b["metrics"]
    assert {m["connection_id"] for m in catalog_a["metrics"]} == {str(conn_a.id)}
    assert {m["connection_id"] for m in catalog_b["metrics"]} == {str(conn_b.id)}


def test_wrong_connection_catalog_excludes_other_metrics(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn_a = seed_connection(db, conn_name="Conn A")
    _, conn_b = seed_connection(db, conn_name="Conn B", tenant=tenant)

    db.add(
        Metric(
            tenant_id=tenant.id,
            connection_id=conn_a.id,
            name="Revenue A",
            expression="SUM(warehouse_a.fact_a.revenue_amt)",
            label="Revenue",
            status="confirmed",
            source_column_ref="warehouse_a.fact_a.revenue_amt",
        )
    )
    db.commit()

    catalog_for_b = load_connection_semantic_catalog(db, tenant.id, conn_b.id)
    assert catalog_for_b["metrics"] == []
