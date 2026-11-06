"""Orchestrate file dataset upload, ingestion, discovery, and lifecycle."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import log_audit
from app.config import get_settings
from app.dataset_errors import (
    DatasetAccessDeniedError,
    DatasetNotFoundError,
    DatasetNotReadyError,
    FileTooLargeError,
    MalformedFileError,
    UnsupportedFileTypeError,
)
from app.encryption import CredentialEncryption
from app.models import Connection, Dataset, User
from app.redis_client import schema_cache
from app.services.connection_service import delete_connection_record
from app.services.csv_ingestion import build_csv_table_spec
from app.services.excel_ingestion import discover_excel_sheets, parse_excel_sheet
from app.services.file_storage import (
    delete_connection_storage,
    duckdb_path_for_connection,
    sanitize_filename,
    store_upload,
)
from app.services.schema_discovery_service import run_schema_discovery
from app.services.semantic_discovery import discover_semantic_suggestions
from app.source_types import SourceType
from app.tools.duckdb_engine import build_connection_manifest, load_table_into_duckdb

logger = logging.getLogger(__name__)

_CSV_EXTENSIONS = {".csv"}
_EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}
_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _detect_source_type(filename: str, content_type: str | None) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _CSV_EXTENSIONS:
        return SourceType.csv.value
    if ext in _EXCEL_EXTENSIONS:
        return SourceType.excel.value
    if content_type in {"text/csv", "application/csv"}:
        return SourceType.csv.value
    if content_type in {
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return SourceType.excel.value
    raise UnsupportedFileTypeError("Only CSV and Excel (.xlsx) files are supported")


def validate_upload(filename: str, content: bytes, *, content_type: str | None) -> str:
    settings = get_settings()
    if not content:
        raise MalformedFileError("File is empty")
    if len(content) > settings.file_max_size_bytes:
        raise FileTooLargeError(
            f"File exceeds maximum size ({settings.file_max_size_bytes} bytes)"
        )
    return _detect_source_type(filename, content_type)


def _dataset_to_dict(row: Dataset) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "connection_id": row.connection_id,
        "name": row.name,
        "display_name": row.display_name,
        "source_type": row.source_type,
        "physical_table": row.physical_table,
        "sheet_name": row.sheet_name,
        "status": row.status,
        "version": row.version,
        "row_count": row.row_count,
        "column_count": row.column_count,
        "original_filename": row.original_filename,
        "content_type": row.content_type,
        "file_size_bytes": row.file_size_bytes,
        "ingestion_error": row.ingestion_error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def get_tenant_dataset(
    db: Session,
    *,
    dataset_id: UUID,
    tenant_id: UUID,
) -> Dataset:
    row = db.scalar(
        select(Dataset).where(Dataset.id == dataset_id, Dataset.tenant_id == tenant_id)
    )
    if row is None:
        raise DatasetNotFoundError("Dataset not found")
    return row


def list_datasets_for_tenant(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID | None = None,
) -> list[Dataset]:
    stmt = select(Dataset).where(Dataset.tenant_id == tenant_id)
    if connection_id is not None:
        stmt = stmt.where(Dataset.connection_id == connection_id)
    return list(db.scalars(stmt.order_by(Dataset.created_at.desc())).all())


def resolve_active_dataset(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    dataset_id: UUID | None = None,
    dataset_hint: str | None = None,
) -> tuple[Dataset | None, str | None]:
    """Return (dataset, clarification_message). Clarification when ambiguous."""
    datasets = list_datasets_for_tenant(
        db, tenant_id=tenant_id, connection_id=connection_id
    )
    ready = [d for d in datasets if d.status == "ready"]
    if not ready:
        return None, None
    if dataset_id is not None:
        match = next((d for d in ready if d.id == dataset_id), None)
        if match is None:
            raise DatasetNotFoundError("Dataset not found on this connection")
        return match, None
    if dataset_hint:
        hint = dataset_hint.strip().lower()
        matches = [
            d
            for d in ready
            if hint in d.display_name.lower()
            or hint in d.name.lower()
            or (d.sheet_name and hint in d.sheet_name.lower())
        ]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            names = ", ".join(d.display_name for d in matches)
            return None, f"Multiple datasets match '{dataset_hint}': {names}. Which one?"
    if len(ready) == 1:
        return ready[0], None
    names = ", ".join(d.display_name for d in ready)
    return None, f"This connection has multiple datasets ({names}). Which dataset should I use?"


def _ingest_table_specs(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    source_type: str,
    specs: list[dict[str, Any]],
    original_filename: str,
    content_type: str | None,
    file_size: int,
    checksum: str,
) -> list[Dataset]:
    duck_path = duckdb_path_for_connection(tenant_id, connection_id)
    created: list[Dataset] = []

    for spec in specs:
        dataset_id = uuid4()
        physical_table = load_table_into_duckdb(
            duck_path,
            dataset_id=dataset_id,
            columns=spec["columns"],
            rows=spec["rows"],
        )
        display = spec.get("display_name") or spec.get("sheet_name") or Path(original_filename).stem
        sheet = spec.get("sheet_name")
        name_base = sheet or Path(original_filename).stem
        row = Dataset(
            id=dataset_id,
            tenant_id=tenant_id,
            connection_id=connection_id,
            name=name_base[:120],
            display_name=str(display)[:200],
            source_type=source_type,
            physical_table=physical_table,
            sheet_name=sheet,
            status="ready",
            version=1,
            row_count=int(spec.get("row_count") or 0),
            column_count=len(spec.get("columns") or []),
            original_filename=sanitize_filename(original_filename),
            content_type=content_type,
            file_size_bytes=file_size,
            file_checksum=checksum,
            columns_json=json.dumps(spec.get("columns") or []),
        )
        db.add(row)
        created.append(row)

    db.flush()
    return created


def ingest_uploaded_file(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    source_type: str,
    content: bytes,
    original_filename: str,
    content_type: str | None,
    checksum: str,
    sheet_names: list[str] | None = None,
) -> list[Dataset]:
    """Parse file content and load tables into the connection DuckDB store."""
    settings = get_settings()
    specs: list[dict[str, Any]] = []

    if source_type == SourceType.csv.value:
        spec = build_csv_table_spec(content)
        spec["display_name"] = Path(original_filename).stem
        specs.append(spec)
    else:
        sheets = sheet_names
        if not sheets:
            discovered = discover_excel_sheets(content)
            if not discovered:
                raise MalformedFileError("Workbook has no usable worksheets")
            if len(discovered) > settings.file_max_worksheets:
                raise MalformedFileError(
                    f"Workbook exceeds maximum worksheet count ({settings.file_max_worksheets})"
                )
            sheets = [s["sheet_name"] for s in discovered]
        for sheet in sheets:
            parsed = parse_excel_sheet(content, sheet)
            parsed["display_name"] = sheet
            specs.append(parsed)

    return _ingest_table_specs(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        source_type=source_type,
        specs=specs,
        original_filename=original_filename,
        content_type=content_type,
        file_size=len(content),
        checksum=checksum,
    )


def upload_dataset_file(
    db: Session,
    *,
    user: User,
    name: str,
    filename: str,
    content: bytes,
    content_type: str | None = None,
    sheet_names: list[str] | None = None,
) -> dict[str, Any]:
    """Full upload → ingest → discover → semantic suggestion pipeline."""
    tenant_id = user.tenant_id
    source_type = validate_upload(filename, content, content_type=content_type)

    connection_id = uuid4()
    _, checksum = store_upload(tenant_id, connection_id, filename=filename, content=content)
    duck_path = duckdb_path_for_connection(tenant_id, connection_id)
    manifest = build_connection_manifest(duck_path)
    encryptor = CredentialEncryption()

    conn = Connection(
        id=connection_id,
        tenant_id=tenant_id,
        name=name.strip()[:120],
        db_type=source_type,
        encrypted_url=encryptor.encrypt(manifest),
        last_discover_status="pending",
    )
    db.add(conn)
    db.flush()

    logger.info(
        "dataset_upload_started tenant_id=%s connection_id=%s source_type=%s size=%s",
        tenant_id,
        connection_id,
        source_type,
        len(content),
    )

    try:
        datasets = ingest_uploaded_file(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            source_type=source_type,
            content=content,
            original_filename=filename,
            content_type=content_type,
            checksum=checksum,
            sheet_names=sheet_names,
        )
    except Exception as exc:
        conn.last_discover_status = "failed"
        db.commit()
        logger.warning(
            "dataset_ingestion_failed tenant_id=%s connection_id=%s error=%s",
            tenant_id,
            connection_id,
            type(exc).__name__,
        )
        raise

    discovery = run_schema_discovery(
        db,
        connection_id=connection_id,
        tenant_id=tenant_id,
        scope="default",
        profile=True,
    )
    semantic_counts = discover_semantic_suggestions(
        db,
        connection_id=connection_id,
        tenant_id=tenant_id,
        llm_client=None,
        use_llm=False,
    )

    log_audit(
        db,
        user=user,
        action="dataset.upload",
        resource_type="connection",
        resource_id=connection_id,
        metadata={
            "source_type": source_type,
            "dataset_count": len(datasets),
            "file_size_bytes": len(content),
            "discovery_status": discovery.status,
        },
    )
    db.commit()

    logger.info(
        "dataset_ingestion_completed tenant_id=%s connection_id=%s datasets=%s status=%s",
        tenant_id,
        connection_id,
        len(datasets),
        discovery.status,
    )

    return {
        "connection_id": connection_id,
        "datasets": [_dataset_to_dict(d) for d in datasets],
        "discovery_status": discovery.status,
        "semantic_counts": semantic_counts,
    }


def delete_dataset(
    db: Session,
    *,
    user: User,
    dataset_id: UUID,
) -> None:
    row = get_tenant_dataset(db, dataset_id=dataset_id, tenant_id=user.tenant_id)
    connection_id = row.connection_id
    tenant_id = row.tenant_id

    db.delete(row)
    remaining = list_datasets_for_tenant(db, tenant_id=tenant_id, connection_id=connection_id)
    if not remaining:
        conn = db.scalar(
            select(Connection).where(
                Connection.id == connection_id,
                Connection.tenant_id == tenant_id,
            )
        )
        if conn is not None:
            delete_connection_record(db, conn)
        else:
            schema_cache.invalidate(tenant_id, connection_id)
            delete_connection_storage(tenant_id, connection_id)

    log_audit(
        db,
        user=user,
        action="dataset.delete",
        resource_type="dataset",
        resource_id=dataset_id,
    )
    db.commit()


def ensure_dataset_ready(dataset: Dataset) -> None:
    if dataset.status != "ready":
        raise DatasetNotReadyError(
            dataset.ingestion_error or "Dataset is not ready for queries"
        )
