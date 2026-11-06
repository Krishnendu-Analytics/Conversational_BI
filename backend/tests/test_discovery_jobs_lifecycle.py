"""Discovery job lifecycle states."""

from __future__ import annotations

from tests.test_helpers_discovery import (
    StubMultiSchemaProvider,
    make_test_db,
    multi_schema_tables,
    patch_provider,
    seed_connection,
)
from app.services.discovery_jobs import (
    get_latest_discovery_status,
    run_schema_discovery_sync,
    set_sync_mode,
)


def test_discovery_job_lifecycle_sync_mode(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)

    provider = StubMultiSchemaProvider(multi_schema_tables())
    patch_provider(monkeypatch, provider)

    set_sync_mode(True)
    run = run_schema_discovery_sync(
        db,
        connection_id=conn.id,
        tenant_id=tenant.id,
        scope="all",
    )

    assert run.status in {"completed", "partial"}
    assert run.started_at is not None
    assert run.completed_at is not None
    assert run.tables_discovered == 3

    latest = get_latest_discovery_status(
        db,
        connection_id=conn.id,
        tenant_id=tenant.id,
    )
    assert latest is not None
    assert latest.id == run.id
    assert latest.status == run.status

    db.refresh(conn)
    assert conn.last_discover_status in {"completed", "partial"}
