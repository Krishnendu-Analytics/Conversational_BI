"""Phase 5 verification fixes: migration downgrade, production embedding guards, pgvector path."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.config import get_settings
from app.services.embeddings import (
    EmbeddingError,
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
    get_embedding_provider,
    validate_embedding_configuration,
)
from app.tools.rag import RAGRetrieveRequest, VectorDocumentRetriever


def _load_migration_0014():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0014_rag_documents.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0014_rag_documents", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_0014_downgrade_assigns_bind() -> None:
    """Downgrade must call op.get_bind() before using bind (no NameError)."""
    mod = _load_migration_0014()
    mock_bind = MagicMock()
    mock_bind.dialect.name = "sqlite"

    with (
        patch.object(mod.op, "get_bind", return_value=mock_bind) as get_bind,
        patch.object(mod.op, "drop_index"),
        patch.object(mod.op, "drop_column"),
        patch.object(mod.op, "execute") as execute,
    ):
        mod.downgrade()
        get_bind.assert_called()
        execute.assert_not_called()  # sqlite path skips DROP INDEX IF EXISTS

    mock_bind.dialect.name = "postgresql"
    with (
        patch.object(mod.op, "get_bind", return_value=mock_bind),
        patch.object(mod.op, "drop_index"),
        patch.object(mod.op, "drop_column"),
        patch.object(mod.op, "execute") as execute,
    ):
        mod.downgrade()
        execute.assert_called()


def test_production_rejects_fake_embedding_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    get_settings.cache_clear()
    with pytest.raises(EmbeddingError, match="Production requires EMBEDDING_PROVIDER"):
        validate_embedding_configuration()
    with pytest.raises(EmbeddingError, match="Production requires EMBEDDING_PROVIDER"):
        get_embedding_provider()
    get_settings.cache_clear()


def test_production_rejects_missing_embedding_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "")
    get_settings.cache_clear()
    with pytest.raises(EmbeddingError, match="Production requires EMBEDDING_PROVIDER"):
        validate_embedding_configuration(embedding_provider="")
    get_settings.cache_clear()


def test_explicit_fake_allowed_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    get_settings.cache_clear()
    provider = get_embedding_provider()
    assert isinstance(provider, FakeEmbeddingProvider)
    assert len(provider.embed_query("hello")) == get_settings().embedding_dimension
    get_settings.cache_clear()


def test_openai_failure_does_not_fallback_to_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-test")
    get_settings.cache_clear()

    provider = OpenAIEmbeddingProvider(api_key="sk-test")

    class Boom:
        def embeddings(self):  # noqa: ANN001
            raise RuntimeError("network")

    class Client:
        def __init__(self, *a, **k):  # noqa: ANN001
            self.embeddings = SimpleNamespace(
                create=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("network"))
            )

    with patch("openai.OpenAI", Client):
        with pytest.raises(EmbeddingError, match="Embedding API failed"):
            provider.embed_documents(["refund policy"])

    # Must not silently become FakeEmbeddingProvider
    resolved = get_embedding_provider()
    assert isinstance(resolved, OpenAIEmbeddingProvider)
    get_settings.cache_clear()


def test_postgresql_dialect_uses_pgvector_path() -> None:
    db = MagicMock()
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    db.get_bind.return_value = bind
    db.scalar.return_value = uuid4()

    embedder = FakeEmbeddingProvider(dimension=8)
    retriever = VectorDocumentRetriever(db, embedder=embedder, min_similarity=0.0)
    expected = []

    with (
        patch.object(retriever, "_retrieve_pgvector", return_value=expected) as pg,
        patch.object(retriever, "_retrieve_python", return_value=expected) as py,
    ):
        result = retriever.retrieve(
            RAGRetrieveRequest(tenant_id=str(uuid4()), question="policy", top_k=3)
        )
        pg.assert_called_once()
        py.assert_not_called()
        assert result.insufficient is True


def test_non_postgresql_dialect_uses_python_cosine_path() -> None:
    db = MagicMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    db.get_bind.return_value = bind
    db.scalar.return_value = uuid4()

    embedder = FakeEmbeddingProvider(dimension=8)
    retriever = VectorDocumentRetriever(db, embedder=embedder, min_similarity=0.0)
    expected = []

    with (
        patch.object(retriever, "_retrieve_pgvector", return_value=expected) as pg,
        patch.object(retriever, "_retrieve_python", return_value=expected) as py,
    ):
        retriever.retrieve(
            RAGRetrieveRequest(tenant_id=str(uuid4()), question="policy", top_k=3)
        )
        py.assert_called_once()
        pg.assert_not_called()
