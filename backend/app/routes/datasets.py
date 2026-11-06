"""REST API for CSV/Excel dataset upload and lifecycle."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dataset_errors import (
    DatasetError,
    DatasetNotFoundError,
    FileTooLargeError,
    IngestionLimitError,
    MalformedFileError,
    UnsupportedFileTypeError,
)
from app.models import User
from app.routes.auth import get_current_user, require_role
from app.schemas import DatasetListResponse, DatasetResponse, DatasetUploadResponse
from app.services.dataset_service import (
    delete_dataset,
    get_tenant_dataset,
    list_datasets_for_tenant,
    upload_dataset_file,
)

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


def _to_response(row) -> DatasetResponse:
    return DatasetResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        connection_id=row.connection_id,
        name=row.name,
        display_name=row.display_name,
        source_type=row.source_type,
        physical_table=row.physical_table,
        sheet_name=row.sheet_name,
        status=row.status,
        version=row.version,
        row_count=row.row_count,
        column_count=row.column_count,
        original_filename=row.original_filename,
        content_type=row.content_type,
        file_size_bytes=row.file_size_bytes,
        ingestion_error=row.ingestion_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _http_error(exc: DatasetError) -> HTTPException:
    status_code = status.HTTP_400_BAD_REQUEST
    if exc.code in {"dataset_not_found"}:
        status_code = status.HTTP_404_NOT_FOUND
    elif exc.code in {"dataset_access_denied"}:
        status_code = status.HTTP_403_FORBIDDEN
    return HTTPException(status_code=status_code, detail=str(exc))


@router.post("/upload", response_model=DatasetUploadResponse)
async def upload_dataset(
    name: str = Form(..., min_length=2, max_length=120),
    file: UploadFile = File(...),
    sheets: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Upload a CSV or Excel file; creates a file-backed connection and dataset(s)."""
    content = await file.read()
    sheet_names = [s.strip() for s in (sheets or "").split(",") if s.strip()] or None
    try:
        result = upload_dataset_file(
            db,
            user=current_user,
            name=name,
            filename=file.filename or "upload.csv",
            content=content,
            content_type=file.content_type,
            sheet_names=sheet_names,
        )
    except (UnsupportedFileTypeError, FileTooLargeError, MalformedFileError, IngestionLimitError) as exc:
        raise _http_error(exc) from exc

    return DatasetUploadResponse(
        connection_id=result["connection_id"],
        datasets=[DatasetResponse(**d) for d in result["datasets"]],
        discovery_status=result["discovery_status"],
        semantic_counts=result["semantic_counts"],
    )


@router.get("", response_model=DatasetListResponse)
def list_datasets(
    connection_id: UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = list_datasets_for_tenant(
        db,
        tenant_id=current_user.tenant_id,
        connection_id=connection_id,
    )
    return DatasetListResponse(datasets=[_to_response(r) for r in rows])


@router.get("/{dataset_id}", response_model=DatasetResponse)
def get_dataset(
    dataset_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        row = get_tenant_dataset(
            db, dataset_id=dataset_id, tenant_id=current_user.tenant_id
        )
    except DatasetNotFoundError as exc:
        raise _http_error(exc) from exc
    return _to_response(row)


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_dataset(
    dataset_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    try:
        delete_dataset(db, user=current_user, dataset_id=dataset_id)
    except DatasetNotFoundError as exc:
        raise _http_error(exc) from exc
