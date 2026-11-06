"""T-57: RAG retrieval contract and SQL/RAG/hybrid merge rules."""

from app.tools.rag import (
    DocumentRetriever,
    RAGChunk,
    RAGPipeline,
    RAGRetrieveRequest,
    RAGRetrieveResponse,
    merge_sql_and_rag_for_insight,
)


def test_retrieve_response_maps_to_workflow_state() -> None:
    response = RAGRetrieveResponse(
        query="refund policy",
        chunks=[
            RAGChunk(
                document_id="doc-1",
                chunk_id="c-1",
                content="Refunds within 30 days.",
                score=0.91,
                metadata={"page": 2},
            )
        ],
        context="Refunds within 30 days.",
        stub=False,
    )
    update = response.to_state_update()
    assert update["rag_query"] == "refund policy"
    assert update["rag_context"] == "Refunds within 30 days."
    assert update["rag_chunks"][0]["document_id"] == "doc-1"
    assert update["rag_chunks"][0]["score"] == 0.91


def test_document_retriever_empty_corpus_is_insufficient() -> None:
    pipeline = RAGPipeline(DocumentRetriever())
    result = pipeline.retrieve(
        RAGRetrieveRequest(tenant_id="tenant-1", question="What is the policy?")
    )
    assert result.stub is False
    assert result.insufficient is True
    assert result.query == "What is the policy?"
    assert result.chunks == []
    assert result.context == ""
    state = result.to_state_update()
    assert "rag_chunks" in state
    assert "rag_context" in state


def test_merge_rules_sql_rag_hybrid() -> None:
    sql_merge = merge_sql_and_rag_for_insight(
        intent="sql",
        question="revenue?",
        sql="SELECT 1",
        rows=[{"n": 1}],
        rag_context="ignored",
    )
    assert sql_merge["source"] == "sql"
    assert sql_merge["rag_context"] == ""
    assert sql_merge["sql"] == "SELECT 1"

    rag_merge = merge_sql_and_rag_for_insight(
        intent="rag",
        question="policy?",
        sql=None,
        rows=None,
        rag_context="doc text",
    )
    assert rag_merge["source"] == "rag"
    assert rag_merge["rag_context"] == "doc text"
    assert rag_merge["sql"] == ""

    hybrid = merge_sql_and_rag_for_insight(
        intent="hybrid",
        question="both?",
        sql="SELECT 1",
        rows=[{"n": 1}],
        rag_context="doc text",
    )
    assert hybrid["source"] == "hybrid"
    assert hybrid["sql"] == "SELECT 1"
    assert hybrid["rag_context"] == "doc text"
