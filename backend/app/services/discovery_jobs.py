"""In-process schema discovery job runner with lifecycle states."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.llm.usage import ContextThreadPoolExecutor
from app.models import Connection, SchemaDiscoveryRun
from app.services.schema_discovery_service import run_schema_discovery

logger = logging.getLogger(__name__)

_executor = ContextThreadPoolExecutor(max_workers=2, thread_name_prefix="schema-discover")
_SYNC_MODE = False


def set_sync_mode(enabled: bool) -> None:
    """When True, jobs run inline (for tests)."""
    global _SYNC_MODE
    _SYNC_MODE = enabled


def is_sync_mode() -> bool:
    return _SYNC_MODE


def _update_run(db: Session, run_id: UUID, **fields: Any) -> None:
    run = db.get(SchemaDiscoveryRun, run_id)
    if run is None:
        return
    for key, value in fields.items():
        setattr(run, key, value)
    db.commit()


def _execute_run(
    run_id: UUID,
    connection_id: UUID,
    tenant_id: UUID,
    *,
    scope: str,
    schemas: list[str] | None,
    profile: bool,
    include_views: bool,
) -> None:
    db = SessionLocal()
    try:
        _update_run(
            db,
            run_id,
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        result = run_schema_discovery(
            db,
            connection_id=connection_id,
            tenant_id=tenant_id,
            scope=scope,
            schemas=schemas,
            profile=profile,
            include_views=include_views,
        )
        final_status = result.status if result.status in {"completed", "partial"} else "failed"
        if result.warnings and result.tables:
            final_status = "partial"
        elif result.warnings and not result.tables:
            final_status = "failed"
        _update_run(
            db,
            run_id,
            status=final_status,
            warnings_json=json.dumps(result.warnings, default=str),
            tables_discovered=result.tables_discovered,
            completed_at=datetime.now(timezone.utc),
            error_message=None if result.tables else (result.warnings[0] if result.warnings else None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Schema discovery job failed")
        _update_run(
            db,
            run_id,
            status="failed",
            error_message=str(exc),
            completed_at=datetime.now(timezone.utc),
        )
        conn = db.scalar(
            select(Connection).where(
                Connection.id == connection_id,
                Connection.tenant_id == tenant_id,
            )
        )
        if conn is not None:
            conn.last_discover_status = "failed"
            db.commit()
    finally:
        db.close()


def enqueue_schema_discovery(
    db: Session,
    *,
    connection_id: UUID,
    tenant_id: UUID,
    scope: str = "default",
    schemas: list[str] | None = None,
    profile: bool = False,
    include_views: bool = False,
) -> SchemaDiscoveryRun:
    """Create a queued discovery run and submit to executor (or run inline in sync mode)."""
    run = SchemaDiscoveryRun(
        tenant_id=tenant_id,
        connection_id=connection_id,
        status="queued",
        scope=scope,
        schemas_json=json.dumps(schemas or [], default=str),
    )
    db.add(run)
    conn = db.scalar(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.tenant_id == tenant_id,
        )
    )
    if conn is not None:
        conn.last_discover_status = "queued"
    db.commit()
    db.refresh(run)

    if _SYNC_MODE:
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        db.commit()
        try:
            result = run_schema_discovery(
                db,
                connection_id=connection_id,
                tenant_id=tenant_id,
                scope=scope,
                schemas=schemas,
                profile=profile,
                include_views=include_views,
            )
            final_status = result.status
            if result.warnings and result.tables:
                final_status = "partial"
            elif result.warnings and not result.tables:
                final_status = "failed"
            run.status = final_status
            run.warnings_json = json.dumps(result.warnings, default=str)
            run.tables_discovered = result.tables_discovered
            run.completed_at = datetime.now(timezone.utc)
            run.error_message = (
                None if result.tables else (result.warnings[0] if result.warnings else None)
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Schema discovery job failed")
            run.status = "failed"
            run.error_message = str(exc)
            run.completed_at = datetime.now(timezone.utc)
            if conn is not None:
                conn.last_discover_status = "failed"
        db.commit()
        db.refresh(run)
        return run

    _executor.submit(
        _execute_run,
        run.id,
        connection_id,
        tenant_id,
        scope=scope,
        schemas=schemas,
        profile=profile,
        include_views=include_views,
    )
    return run


def run_schema_discovery_sync(
    db: Session,
    *,
    connection_id: UUID,
    tenant_id: UUID,
    scope: str = "default",
    schemas: list[str] | None = None,
    profile: bool = False,
    include_views: bool = False,
) -> SchemaDiscoveryRun:
    """Test-friendly entry: runs discovery inline and returns completed run record."""
    prev = _SYNC_MODE
    set_sync_mode(True)
    try:
        return enqueue_schema_discovery(
            db,
            connection_id=connection_id,
            tenant_id=tenant_id,
            scope=scope,
            schemas=schemas,
            profile=profile,
            include_views=include_views,
        )
    finally:
        set_sync_mode(prev)


def get_latest_discovery_status(
    db: Session,
    *,
    connection_id: UUID,
    tenant_id: UUID,
) -> SchemaDiscoveryRun | None:
    return db.scalar(
        select(SchemaDiscoveryRun)
        .where(
            SchemaDiscoveryRun.connection_id == connection_id,
            SchemaDiscoveryRun.tenant_id == tenant_id,
        )
        .order_by(SchemaDiscoveryRun.created_at.desc())
    )
