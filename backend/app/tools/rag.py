"""Document search (RAG) — retrieval contracts and vector/pgvector retrieval."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Document, DocumentChunk
from app.services.embeddings import (
    EmbeddingProvider,
    cosine_similarity,
    get_embedding_provider,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RAGChunk:
    """One retrieved document fragment."""

    document_id: str
    chunk_id: str
    content: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RAGRetrieveRequest:
    """Input to the retrieval stage."""

    tenant_id: str
    question: str
    top_k: int = 5
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class RAGRetrieveResponse:
    """Output of the retrieval stage, ready to merge into workflow state."""

    query: str
    chunks: list[RAGChunk] = field(default_factory=list)
    context: str = ""
    stub: bool = False
    insufficient: bool = False

    def to_state_update(self) -> dict[str, Any]:
        """Map this response onto WorkflowState RAG fields."""
        return {
            "rag_query": self.query,
            "rag_chunks": [
                {
                    "document_id": c.document_id,
                    "chunk_id": c.chunk_id,
                    "content": c.content,
                    "score": c.score,
                    "metadata": dict(c.metadata),
                }
                for c in self.chunks
            ],
            "rag_context": self.context,
        }


def merge_sql_and_rag_for_insight(
    *,
    intent: str,
    question: str,
    sql: str | None,
    rows: list[dict[str, Any]] | None,
    rag_context: str | None,
) -> dict[str, str]:
    """Build the prompt ingredients after SQL / RAG / hybrid paths converge.

    Merge rules:
    - ``sql`` intent: use SQL + rows only.
    - ``rag`` intent: use rag_context only.
    - ``hybrid`` intent: include both SQL/rows and rag_context.
    - SQL numeric facts remain authoritative; RAG is qualitative context.
    """
    rows = rows or []
    rag_context = rag_context or ""

    if intent == "rag":
        return {
            "question": question,
            "sql": "",
            "rows": "",
            "rag_context": rag_context,
            "source": "rag",
        }

    if intent == "hybrid":
        return {
            "question": question,
            "sql": sql or "",
            "rows": str(rows),
            "rag_context": rag_context,
            "source": "hybrid",
        }

    return {
        "question": question,
        "sql": sql or "",
        "rows": str(rows),
        "rag_context": "",
        "source": "sql",
    }


def format_rag_context(chunks: Sequence[RAGChunk]) -> str:
    if not chunks:
        return ""
    blocks: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        name = chunk.metadata.get("document_name") or chunk.document_id
        blocks.append(
            f"[Source {i}] document={name} chunk={chunk.chunk_id} score={chunk.score:.3f}\n"
            f"{chunk.content}"
        )
    return "\n\n".join(blocks)


class KeywordDocumentRetriever:
    """In-memory keyword retriever (unit tests / fallback without DB)."""

    def __init__(self, chunks: list[RAGChunk] | None = None) -> None:
        self.chunks = chunks or []

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return set(re.findall(r"\b\w+\b", text.lower()))

    def retrieve(self, request: RAGRetrieveRequest) -> RAGRetrieveResponse:
        if not self.chunks:
            return RAGRetrieveResponse(
                query=request.question,
                chunks=[],
                context="",
                stub=False,
                insufficient=True,
            )

        question_tokens = self._tokens(request.question)
        if not question_tokens:
            return RAGRetrieveResponse(
                query=request.question,
                chunks=[],
                context="",
                insufficient=True,
            )

        scored: list[RAGChunk] = []
        for chunk in self.chunks:
            matching = question_tokens.intersection(self._tokens(chunk.content))
            if not matching:
                continue
            score = len(matching) / len(question_tokens)
            scored.append(
                RAGChunk(
                    document_id=chunk.document_id,
                    chunk_id=chunk.chunk_id,
                    content=chunk.content,
                    score=score,
                    metadata=dict(chunk.metadata),
                )
            )
        scored.sort(key=lambda c: (-c.score, c.chunk_id))
        results = scored[: max(request.top_k, 0)]
        return RAGRetrieveResponse(
            query=request.question,
            chunks=results,
            context=format_rag_context(results),
            insufficient=not results,
        )


# Backward-compatible alias used by older tests that inject in-memory chunks.
DocumentRetriever = KeywordDocumentRetriever


class VectorDocumentRetriever:
    """Tenant-scoped vector retrieval over DocumentChunk embeddings."""

    def __init__(
        self,
        db: Session,
        *,
        embedder: EmbeddingProvider | None = None,
        min_similarity: float | None = None,
    ) -> None:
        self.db = db
        self.embedder = embedder or get_embedding_provider()
        settings = get_settings()
        self.min_similarity = (
            min_similarity if min_similarity is not None else settings.rag_min_similarity
        )

    def retrieve(self, request: RAGRetrieveRequest) -> RAGRetrieveResponse:
        tenant_id = UUID(str(request.tenant_id))
        top_k = request.top_k or get_settings().rag_top_k
        document_ids = _parse_document_ids(request.filters)

        ready_exists = self.db.scalar(
            select(Document.id).where(
                Document.tenant_id == tenant_id,
                Document.status == "ready",
            )
        )
        if ready_exists is None and not document_ids:
            return RAGRetrieveResponse(
                query=request.question,
                chunks=[],
                context="",
                insufficient=True,
            )

        try:
            query_vec = self.embedder.embed_query(request.question)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rag_embed_query_failed: %s", type(exc).__name__)
            return RAGRetrieveResponse(
                query=request.question,
                chunks=[],
                context="",
                insufficient=True,
            )

        # Prefer pgvector SQL on Postgres; otherwise Python cosine (SQLite tests).
        dialect = self.db.get_bind().dialect.name
        if dialect == "postgresql":
            chunks = self._retrieve_pgvector(
                tenant_id=tenant_id,
                query_vec=query_vec,
                top_k=top_k,
                document_ids=document_ids,
            )
        else:
            chunks = self._retrieve_python(
                tenant_id=tenant_id,
                query_vec=query_vec,
                top_k=top_k,
                document_ids=document_ids,
            )

        return RAGRetrieveResponse(
            query=request.question,
            chunks=chunks,
            context=format_rag_context(chunks),
            insufficient=not chunks,
        )

    def _retrieve_pgvector(
        self,
        *,
        tenant_id: UUID,
        query_vec: list[float],
        top_k: int,
        document_ids: list[UUID] | None,
    ) -> list[RAGChunk]:
        from sqlalchemy import text

        # cosine distance operator <=> ; similarity = 1 - distance
        params: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "q": "[" + ",".join(str(float(x)) for x in query_vec) + "]",
            "top_k": top_k,
            "min_sim": self.min_similarity,
        }
        doc_filter = ""
        if document_ids:
            placeholders = []
            for i, did in enumerate(document_ids):
                key = f"doc_{i}"
                placeholders.append(f":{key}")
                params[key] = str(did)
            doc_filter = f"AND c.document_id IN ({', '.join(placeholders)})"

        sql = text(
            f"""
            SELECT c.id, c.document_id, c.content, c.chunk_index, c.metadata_json,
                   d.filename AS document_name,
                   1 - (c.embedding <=> CAST(:q AS vector)) AS score
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.tenant_id = CAST(:tenant_id AS uuid)
              AND d.tenant_id = CAST(:tenant_id AS uuid)
              AND d.status = 'ready'
              AND c.embedding IS NOT NULL
              {doc_filter}
              AND (1 - (c.embedding <=> CAST(:q AS vector))) >= :min_sim
            ORDER BY c.embedding <=> CAST(:q AS vector) ASC, c.id ASC
            LIMIT :top_k
            """
        )
        rows = self.db.execute(sql, params).mappings().all()
        return [_row_to_chunk(row) for row in rows]

    def _retrieve_python(
        self,
        *,
        tenant_id: UUID,
        query_vec: list[float],
        top_k: int,
        document_ids: list[UUID] | None,
    ) -> list[RAGChunk]:
        stmt = (
            select(DocumentChunk, Document.filename)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.tenant_id == tenant_id,
                Document.tenant_id == tenant_id,
                Document.status == "ready",
                DocumentChunk.embedding.is_not(None),
            )
        )
        if document_ids:
            stmt = stmt.where(DocumentChunk.document_id.in_(document_ids))
        rows = self.db.execute(stmt).all()
        scored: list[tuple[float, DocumentChunk, str]] = []
        for chunk, filename in rows:
            emb = chunk.embedding
            if not emb:
                continue
            score = cosine_similarity(query_vec, emb)
            if score < self.min_similarity:
                continue
            scored.append((score, chunk, filename or ""))
        scored.sort(key=lambda t: (-t[0], str(t[1].id)))
        out: list[RAGChunk] = []
        for score, chunk, filename in scored[:top_k]:
            meta = _parse_meta(chunk.metadata_json)
            meta["document_name"] = filename
            meta["chunk_index"] = chunk.chunk_index
            out.append(
                RAGChunk(
                    document_id=str(chunk.document_id),
                    chunk_id=str(chunk.id),
                    content=chunk.content,
                    score=float(score),
                    metadata=meta,
                )
            )
        return out


class RAGPipeline:
    """RAG retrieval pipeline."""

    def __init__(self, retriever: Any | None = None) -> None:
        self.retriever = retriever or KeywordDocumentRetriever()

    def retrieve(self, request: RAGRetrieveRequest) -> RAGRetrieveResponse:
        return self.retriever.retrieve(request)


def _parse_document_ids(filters: dict[str, Any] | None) -> list[UUID] | None:
    if not filters:
        return None
    raw = filters.get("document_ids") or filters.get("document_id")
    if raw is None:
        return None
    if isinstance(raw, (str, UUID)):
        raw = [raw]
    out: list[UUID] = []
    for item in raw:
        try:
            out.append(UUID(str(item)))
        except ValueError:
            continue
    return out or None


def _parse_meta(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _row_to_chunk(row: Any) -> RAGChunk:
    meta = _parse_meta(row.get("metadata_json"))
    meta["document_name"] = row.get("document_name")
    meta["chunk_index"] = row.get("chunk_index")
    return RAGChunk(
        document_id=str(row["document_id"]),
        chunk_id=str(row["id"]),
        content=str(row["content"]),
        score=float(row["score"] or 0.0),
        metadata=meta,
    )


# Re-export extractor/chunker stubs for import compatibility with AGENT.md names
class DocumentExtractor:  # noqa: D101 — prefer app.services.document_extractor
    def __init__(self) -> None:
        from app.services.document_extractor import DocumentExtractor as Impl

        self._impl = Impl()

    def extract(self, *args: Any, **kwargs: Any) -> Any:
        return self._impl.extract(*args, **kwargs)


class TextChunker:  # noqa: D101
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        from app.services.text_chunker import TextChunker as Impl

        self._impl = Impl(*args, **kwargs)

    def chunk(self, *args: Any, **kwargs: Any) -> Any:
        return self._impl.chunk(*args, **kwargs)


class EmbeddingService:  # noqa: D101
    def __init__(self) -> None:
        self._provider = get_embedding_provider()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self._provider.embed_documents(texts)
