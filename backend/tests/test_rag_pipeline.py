"""Tests for the basic RAGPipeline retrieval path (T-65)."""

from app.tools.rag import (
    DocumentRetriever,
    RAGChunk,
    RAGPipeline,
    RAGRetrieveRequest,
)


def test_retrieves_matching_documents() -> None:
    """Relevant chunks should be returned for a matching question."""

    chunks = [
        RAGChunk(
            document_id="doc-1",
            chunk_id="chunk-1",
            content="The refund policy allows returns within 30 days.",
        ),
        RAGChunk(
            document_id="doc-2",
            chunk_id="chunk-2",
            content="Password reset instructions are available in settings.",
        ),
    ]

    retriever = DocumentRetriever(chunks)
    pipeline = RAGPipeline(retriever)

    result = pipeline.retrieve(
        RAGRetrieveRequest(
            tenant_id="tenant-1",
            question="What is the refund policy?",
        )
    )

    assert result.stub is False
    assert len(result.chunks) == 1
    assert result.chunks[0].document_id == "doc-1"
    assert "refund" in result.context.lower()


def test_no_matching_documents_returns_empty_result() -> None:
    """An unrelated question should return no chunks."""

    chunks = [
        RAGChunk(
            document_id="doc-1",
            chunk_id="chunk-1",
            content="The refund policy allows returns within 30 days.",
        )
    ]

    pipeline = RAGPipeline(DocumentRetriever(chunks))

    result = pipeline.retrieve(
        RAGRetrieveRequest(
            tenant_id="tenant-1",
            question="How do I reset my password?",
        )
    )

    assert result.chunks == []
    assert result.context == ""
    assert result.stub is False


def test_top_k_limits_results() -> None:
    """The retriever should return no more than top_k chunks."""

    chunks = [
        RAGChunk(
            document_id="doc-1",
            chunk_id="chunk-1",
            content="Refund policy information for customers.",
        ),
        RAGChunk(
            document_id="doc-2",
            chunk_id="chunk-2",
            content="Refund policy and return information.",
        ),
        RAGChunk(
            document_id="doc-3",
            chunk_id="chunk-3",
            content="Refund policy for damaged products.",
        ),
    ]

    pipeline = RAGPipeline(DocumentRetriever(chunks))

    result = pipeline.retrieve(
        RAGRetrieveRequest(
            tenant_id="tenant-1",
            question="refund policy",
            top_k=2,
        )
    )

    assert len(result.chunks) == 2


def test_pipeline_uses_provided_retriever() -> None:
    """RAGPipeline should delegate retrieval to the provided retriever."""

    chunk = RAGChunk(
        document_id="doc-1",
        chunk_id="chunk-1",
        content="The refund policy is available here.",
    )

    retriever = DocumentRetriever([chunk])
    pipeline = RAGPipeline(retriever)

    assert pipeline.retriever is retriever

    result = pipeline.retrieve(
        RAGRetrieveRequest(
            tenant_id="tenant-1",
            question="refund policy",
        )
    )

    assert len(result.chunks) == 1