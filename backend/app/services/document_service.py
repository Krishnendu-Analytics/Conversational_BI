"""Synchronous document upload and RAG ingestion pipeline."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit import log_audit
from app.config import get_settings
from app.models import Document, DocumentChunk, User
from app.services.document_extractor import (
    DocumentExtractor,
    EmptyDocumentError,
    MalformedDocumentError,
    UnsupportedDocumentTypeError,
    sniff_content_type,
)
from app.services.document_storage import (
    absolute_storage_path,
    delete_document_storage,
    store_document_bytes,
)
from app.services.embeddings import EmbeddingError, EmbeddingProvider, get_embedding_provider
from app.services.file_storage import sanitize_filename
from app.services.text_chunker import TextChunker

logger = logging.getLogger(__name__)


class DocumentTooLargeError(ValueError):
    code = "document_too_large"


class DocumentNotFoundError(ValueError):
    code = "document_not_found"


class DocumentAccessDeniedError(ValueError):
    code = "document_access_denied"


def upload_and_ingest_document(
    db: Session,
    *,
    user: User,
    filename: str,
    content: bytes,
    content_type: str | None = None,
    embedder: EmbeddingProvider | None = None,
) -> Document:
    """Upload → extract → chunk → embed → store (synchronous)."""
    settings = get_settings()
    if not content:
        raise EmptyDocumentError("Empty file")
    if len(content) > settings.document_max_size_bytes:
        raise DocumentTooLargeError(
            f"File exceeds limit of {settings.document_max_size_bytes} bytes"
        )

    safe_name = sanitize_filename(filename)
    sniffed = sniff_content_type(safe_name, content_type)
    document_id = uuid4()
    rel_path, checksum = store_document_bytes(
        user.tenant_id,
        document_id,
        filename=safe_name,
        content=content,
    )

    now = datetime.now(timezone.utc)
    doc = Document(
        id=document_id,
        tenant_id=user.tenant_id,
        filename=safe_name,
        original_filename=safe_name,
        content_type=sniffed,
        storage_path=rel_path,
        file_size_bytes=len(content),
        checksum_sha256=checksum,
        status="processing",
        created_at=now,
        updated_at=now,
    )
    db.add(doc)
    db.flush()

    try:
        _ingest_document_bytes(
            db,
            doc=doc,
            content=content,
            embedder=embedder or get_embedding_provider(),
        )
        doc.status = "ready"
        doc.error_message = None
        doc.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(doc)
        log_audit(
            db,
            user=user,
            action="document.upload",
            resource_type="document",
            resource_id=doc.id,
            metadata={"filename": safe_name, "chunks": doc.chunk_count, "status": doc.status},
        )
        db.commit()
        return doc
    except Exception as exc:  # noqa: BLE001
        logger.exception("document_ingest_failed document=%s", document_id)
        doc.status = "failed"
        doc.error_message = _safe_error(exc)
        doc.updated_at = datetime.now(timezone.utc)
        db.commit()
        log_audit(
            db,
            user=user,
            action="document.ingest_failed",
            resource_type="document",
            resource_id=doc.id,
            metadata={"error": type(exc).__name__},
        )
        db.commit()
        raise


def _ingest_document_bytes(
    db: Session,
    *,
    doc: Document,
    content: bytes,
    embedder: EmbeddingProvider,
) -> None:
    settings = get_settings()
    extracted = DocumentExtractor().extract(
        content,
        filename=doc.filename,
        content_type=doc.content_type,
    )
    chunker = TextChunker(
        chunk_size=settings.document_chunk_size,
        overlap=settings.document_chunk_overlap,
    )
    pieces = chunker.chunk(extracted.text, max_chunks=settings.document_max_chunks)
    if not pieces:
        raise EmptyDocumentError("No chunks produced from document")

    vectors = embedder.embed_documents([p.text for p in pieces])
    if len(vectors) != len(pieces):
        raise EmbeddingError("Embedding provider returned unexpected batch size")

    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    for piece, vector in zip(pieces, vectors):
        meta = {"content_type": extracted.content_type}
        if extracted.page_count is not None:
            meta["page_count"] = extracted.page_count
        db.add(
            DocumentChunk(
                id=uuid4(),
                tenant_id=doc.tenant_id,
                document_id=doc.id,
                content=piece.text,
                chunk_index=piece.index,
                token_count=piece.token_count,
                metadata_json=json.dumps(meta),
                embedding=list(vector),
            )
        )
    doc.chunk_count = len(pieces)
    doc.content_type = extracted.content_type
    db.flush()


def get_tenant_document(db: Session, *, document_id: UUID, tenant_id: UUID) -> Document:
    doc = db.scalar(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    if doc is None:
        raise DocumentNotFoundError("Document not found")
    return doc


def list_tenant_documents(db: Session, *, tenant_id: UUID) -> list[Document]:
    return list(
        db.scalars(
            select(Document)
            .where(Document.tenant_id == tenant_id)
            .order_by(Document.created_at.desc())
        ).all()
    )


def delete_tenant_document(db: Session, *, user: User, document_id: UUID) -> None:
    doc = get_tenant_document(db, document_id=document_id, tenant_id=user.tenant_id)
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    db.delete(doc)
    db.commit()
    delete_document_storage(user.tenant_id, document_id)
    log_audit(
        db,
        user=user,
        action="document.delete",
        resource_type="document",
        resource_id=document_id,
        metadata={"filename": doc.filename},
    )
    db.commit()


def document_to_dict(doc: Document) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "tenant_id": str(doc.tenant_id),
        "filename": doc.filename,
        "original_filename": doc.original_filename,
        "content_type": doc.content_type,
        "status": doc.status,
        "error_message": doc.error_message,
        "chunk_count": doc.chunk_count,
        "file_size_bytes": doc.file_size_bytes,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


def _safe_error(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            UnsupportedDocumentTypeError,
            EmptyDocumentError,
            MalformedDocumentError,
            DocumentTooLargeError,
            EmbeddingError,
        ),
    ):
        return str(exc)
    return f"Ingestion failed ({type(exc).__name__})"


# Keep absolute_storage_path import used for future re-ingest paths
_ = absolute_storage_path
