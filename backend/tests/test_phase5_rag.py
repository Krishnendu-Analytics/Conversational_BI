"""Phase 5: document RAG — chunking, embeddings, upload, retrieval, tenant isolation."""

from __future__ import annotations

from io import BytesIO
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import get_db
from app.models import Document, DocumentChunk
from app.services.document_extractor import DocumentExtractor, UnsupportedDocumentTypeError
from app.services.embeddings import FakeEmbeddingProvider, cosine_similarity
from app.services.text_chunker import TextChunker
from app.tools.rag import RAGRetrieveRequest, VectorDocumentRetriever
from tests.test_tenant_isolation import _signup_and_token


@pytest.fixture()
def doc_storage_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("DOCUMENT_STORAGE_ROOT", str(tmp_path / "docs"))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_chunker_empty_short_long_overlap_deterministic_unicode() -> None:
    chunker = TextChunker(chunk_size=20, overlap=5)
    assert chunker.chunk("") == []
    assert chunker.chunk("   ") == []

    short = chunker.chunk("hello world")
    assert len(short) == 1
    assert short[0].text == "hello world"

    long_text = " ".join(f"word{i}" for i in range(50))
    a = chunker.chunk(long_text)
    b = chunker.chunk(long_text)
    assert [c.text for c in a] == [c.text for c in b]
    assert len(a) > 1
    assert all(c.text for c in a)

    uni = chunker.chunk("café résumé 日本語")
    assert len(uni) == 1
    assert "café" in uni[0].text


def test_fake_embeddings_deterministic() -> None:
    provider = FakeEmbeddingProvider(dimension=32)
    a = provider.embed_query("refund policy")
    b = provider.embed_query("refund policy")
    c = provider.embed_query("password reset")
    related = provider.embed_query("What is the refund policy?")
    assert a == b
    assert len(a) == 32
    assert cosine_similarity(a, b) > 0.99
    assert cosine_similarity(a, c) < cosine_similarity(a, related)
    assert cosine_similarity(a, related) >= 0.15


def test_extractor_text_and_unsupported() -> None:
    ex = DocumentExtractor()
    result = ex.extract(b"Hello docs", filename="notes.txt", content_type="text/plain")
    assert "Hello" in result.text
    with pytest.raises(UnsupportedDocumentTypeError):
        ex.extract(b"MZ...", filename="evil.exe")


def test_document_upload_and_retrieve(
    client: tuple[TestClient, dict[str, str]],
    doc_storage_tmp,
) -> None:
    test_client, sent = client
    headers, _token, tenant_id, _user = _signup_and_token(
        test_client, sent, f"rag-{uuid4().hex[:8]}@acme.com"
    )
    content = (
        b"Our refund policy allows returns within 30 days of purchase. "
        b"Contact support for warranty claims."
    )
    resp = test_client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("policy.txt", BytesIO(content), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["chunk_count"] >= 1
    assert body["tenant_id"] == str(tenant_id)

    gen = test_client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        retriever = VectorDocumentRetriever(db, embedder=FakeEmbeddingProvider())
        result = retriever.retrieve(
            RAGRetrieveRequest(
                tenant_id=str(tenant_id),
                question="What is the refund policy?",
                top_k=3,
            )
        )
        assert result.chunks
        assert "refund" in result.context.lower()
        assert result.insufficient is False
    finally:
        gen.close()


def test_document_tenant_isolation(
    client: tuple[TestClient, dict[str, str]],
    doc_storage_tmp,
) -> None:
    test_client, sent = client
    headers_a, _t, tenant_a, _u = _signup_and_token(
        test_client, sent, "doc-a@acme.com", tenant_name="TenantA"
    )
    headers_b, _t, tenant_b, _u = _signup_and_token(
        test_client, sent, "doc-b@other.com", tenant_name="TenantB"
    )
    up_a = test_client.post(
        "/api/v1/documents/upload",
        headers=headers_a,
        files={"file": ("a.txt", BytesIO(b"Secret alpha refund rules"), "text/plain")},
    )
    assert up_a.status_code == 201
    doc_a = up_a.json()["id"]
    up_b = test_client.post(
        "/api/v1/documents/upload",
        headers=headers_b,
        files={"file": ("b.txt", BytesIO(b"Secret beta shipping rules"), "text/plain")},
    )
    assert up_b.status_code == 201

    # B cannot GET A's document
    denied = test_client.get(f"/api/v1/documents/{doc_a}", headers=headers_b)
    assert denied.status_code == 404

    gen = test_client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        retriever = VectorDocumentRetriever(db, embedder=FakeEmbeddingProvider())
        res_a = retriever.retrieve(
            RAGRetrieveRequest(tenant_id=str(tenant_a), question="refund rules", top_k=5)
        )
        res_b = retriever.retrieve(
            RAGRetrieveRequest(tenant_id=str(tenant_b), question="shipping rules", top_k=5)
        )
        assert all(c.document_id == doc_a for c in res_a.chunks)
        assert all(c.document_id == up_b.json()["id"] for c in res_b.chunks)
        assert "beta" not in (res_a.context or "").lower()
        assert "alpha" not in (res_b.context or "").lower()
    finally:
        gen.close()


def test_document_upload_rejects_bad_type_and_oversize(
    client: tuple[TestClient, dict[str, str]],
    doc_storage_tmp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("DOCUMENT_MAX_SIZE_BYTES", "32")
    get_settings.cache_clear()
    test_client, sent = client
    headers, *_ = _signup_and_token(test_client, sent, f"bad-{uuid4().hex[:6]}@acme.com")
    bad = test_client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("x.exe", BytesIO(b"MZ"), "application/octet-stream")},
    )
    assert bad.status_code == 400
    big = test_client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("big.txt", BytesIO(b"x" * 100), "text/plain")},
    )
    assert big.status_code == 400
    get_settings.cache_clear()


def test_malicious_filename_sanitized(
    client: tuple[TestClient, dict[str, str]],
    doc_storage_tmp,
) -> None:
    test_client, sent = client
    headers, *_ = _signup_and_token(test_client, sent, f"mal-{uuid4().hex[:6]}@acme.com")
    resp = test_client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={
            "file": (
                "../../etc/passwd.txt",
                BytesIO(b"safe content about policies"),
                "text/plain",
            )
        },
    )
    assert resp.status_code == 201, resp.text
    assert ".." not in resp.json()["filename"]
    assert "/" not in resp.json()["filename"]


def test_rag_workflow_with_document(
    client: tuple[TestClient, dict[str, str]],
    doc_storage_tmp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent.workflow as workflow_mod
    from app.agent.workflow import BIWorkflow
    from app.tools.customer_db import ConnectionTestResult

    test_client, sent = client
    headers, _token, tenant_id, user_id = _signup_and_token(
        test_client, sent, f"wf-{uuid4().hex[:6]}@acme.com"
    )
    monkeypatch.setattr(
        "app.routes.connections.ProviderFactory.create",
        lambda *_a, **_k: type(
            "S",
            (),
            {"test_connection": staticmethod(lambda: ConnectionTestResult(ok=True, message="ok", read_only_verified=True))},
        )(),
    )
    conn = test_client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "name": "DB",
            "db_type": "postgres",
            "connection_url": "postgresql://u:p@localhost/db",
        },
    )
    assert conn.status_code == 201
    connection_id = conn.json()["id"]

    up = test_client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={
            "file": (
                "policy.txt",
                BytesIO(b"Employees may work remote on Fridays with manager approval."),
                "text/plain",
            )
        },
    )
    assert up.status_code == 201
    doc_id = up.json()["id"]

    # Point workflow SessionLocal at the same in-memory engine as the TestClient.
    from sqlalchemy.orm import sessionmaker

    probe_gen = test_client.app.dependency_overrides[get_db]()
    probe = next(probe_gen)
    engine = probe.get_bind()
    probe.close()
    probe_gen.close()
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("app.database.SessionLocal", TestSessionLocal)
    monkeypatch.setattr(workflow_mod, "SessionLocal", TestSessionLocal)

    class Stub:
        client = object()

        def complete(self, prompt: str) -> str:
            lower = prompt.lower()
            if "intent classifier" in lower:
                return "rag"
            if "retrieved document" in lower:
                return "Remote work is allowed on Fridays with manager approval. [Source 1]"
            return "ok"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: Stub())
    result = BIWorkflow().run(
        "Can employees work remotely on Fridays?",
        tenant_id=tenant_id,
        user_id=user_id,
        connection_id=connection_id,
        document_ids=[UUID(doc_id)],
    )
    assert result.get("rag_chunks")
    assert "friday" in (result.get("answer") or "").lower() or "remote" in (
        result.get("answer") or ""
    ).lower()
