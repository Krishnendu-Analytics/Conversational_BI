"""REST API for document upload and RAG corpus management."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routes.auth import get_current_user, require_role
from app.schemas import DocumentListResponse, DocumentResponse
from app.services.document_extractor import (
    EmptyDocumentError,
    MalformedDocumentError,
    UnsupportedDocumentTypeError,
)
from app.services.document_service import (
    DocumentAccessDeniedError,
    DocumentNotFoundError,
    DocumentTooLargeError,
    delete_tenant_document,
    document_to_dict,
    get_tenant_document,
    list_tenant_documents,
    upload_and_ingest_document,
)
from app.services.embeddings import EmbeddingError

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


def _http_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "")
    status_code = status.HTTP_400_BAD_REQUEST
    if code in {"document_not_found"}:
        status_code = status.HTTP_404_NOT_FOUND
    elif code in {"document_access_denied"}:
        status_code = status.HTTP_403_FORBIDDEN
    return HTTPException(status_code=status_code, detail=str(exc))


@router.post("/upload", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    content = await file.read()
    try:
        doc = upload_and_ingest_document(
            db,
            user=current_user,
            filename=file.filename or "document.txt",
            content=content,
            content_type=file.content_type,
        )
    except (
        UnsupportedDocumentTypeError,
        EmptyDocumentError,
        MalformedDocumentError,
        DocumentTooLargeError,
        EmbeddingError,
    ) as exc:
        raise _http_error(exc) from exc
    return DocumentResponse.model_validate(document_to_dict(doc))


@router.get("", response_model=DocumentListResponse)
def list_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = list_tenant_documents(db, tenant_id=current_user.tenant_id)
    return DocumentListResponse(
        documents=[DocumentResponse.model_validate(document_to_dict(r)) for r in rows]
    )


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        doc = get_tenant_document(
            db, document_id=document_id, tenant_id=current_user.tenant_id
        )
    except DocumentNotFoundError as exc:
        raise _http_error(exc) from exc
    return DocumentResponse.model_validate(document_to_dict(doc))


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "analyst")),
):
    try:
        delete_tenant_document(db, user=current_user, document_id=document_id)
    except (DocumentNotFoundError, DocumentAccessDeniedError) as exc:
        raise _http_error(exc) from exc
