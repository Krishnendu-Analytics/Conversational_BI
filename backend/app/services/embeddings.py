"""Embedding provider abstraction for document RAG."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from abc import ABC, abstractmethod
from typing import Sequence

from app.config import get_settings, is_production_env
from app.llm.usage import record_llm_usage, usage_tokens


class EmbeddingError(RuntimeError):
    code = "embedding_error"


_FAKE_PROVIDERS = frozenset({"fake", "hash", "test"})
_REAL_PROVIDERS = frozenset({"openai", "openai_compatible"})


def validate_embedding_configuration(
    *,
    app_env: str | None = None,
    embedding_provider: str | None = None,
) -> None:
    """Reject silent fake embeddings when APP_ENV is production/prod."""
    settings = get_settings()
    provider = (
        embedding_provider
        if embedding_provider is not None
        else settings.embedding_provider
    )
    provider = (provider or "").strip().lower()
    if not is_production_env(app_env):
        return
    if not provider or provider in _FAKE_PROVIDERS:
        raise EmbeddingError(
            "Production requires EMBEDDING_PROVIDER to be a real provider "
            f"(e.g. openai); got {provider!r}. Fake embeddings are not allowed."
        )
    if provider not in _REAL_PROVIDERS:
        raise EmbeddingError(f"Unknown EMBEDDING_PROVIDER: {provider}")


class EmbeddingProvider(ABC):
    """Produce fixed-dimension float vectors for text."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        return vectors[0]


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic hash-based embeddings for tests and local MVP."""

    def __init__(self, dimension: int | None = None) -> None:
        self._dimension = dimension or get_settings().embedding_dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [_hash_embed(text, self._dimension) for text in texts]


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embeddings API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        dimension: int | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.embedding_api_key
        self.base_url = base_url or settings.embedding_base_url
        self.model = model or settings.embedding_model
        self._dimension = dimension or settings.embedding_dimension
        if not self.api_key:
            raise EmbeddingError("EMBEDDING_API_KEY / LLM_API_KEY is not configured")

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise EmbeddingError("openai package is required") from exc

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        try:
            response = client.embeddings.create(
                model=self.model,
                input=list(texts),
                dimensions=self._dimension,
            )
        except TypeError:
            # Older models may not accept dimensions=
            response = client.embeddings.create(model=self.model, input=list(texts))
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"Embedding API failed: {type(exc).__name__}") from exc

        prompt_tokens, _ = usage_tokens(response)
        record_llm_usage(self.model, prompt_tokens, kind="embedding")

        by_index = {item.index: item.embedding for item in response.data}
        vectors: list[list[float]] = []
        for i in range(len(texts)):
            vec = list(by_index[i])
            if len(vec) != self._dimension:
                vec = _resize(vec, self._dimension)
            vectors.append(vec)
        return vectors


def get_embedding_provider() -> EmbeddingProvider:
    """Resolve the configured embedding provider.

    Development/tests may use ``fake`` (default). Production must set a real
    provider; there is no silent fallback from OpenAI failures to fake.
    """
    settings = get_settings()
    provider = (settings.embedding_provider or "").strip().lower()
    if not provider:
        provider = "fake"

    validate_embedding_configuration(embedding_provider=provider)

    if provider in _FAKE_PROVIDERS:
        return FakeEmbeddingProvider(settings.embedding_dimension)
    if provider in _REAL_PROVIDERS:
        return OpenAIEmbeddingProvider()
    raise EmbeddingError(f"Unknown EMBEDDING_PROVIDER: {provider}")


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _hash_embed(text: str, dim: int) -> list[float]:
    """Token-hashed bag-of-features vector.

    Whole-text SHA hashes do not correlate for paraphrases, so retrieval tests
    and local MVP need overlapping tokens to produce overlapping dimensions.
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if not tokens:
        tokens = ["_empty_"]
    values = [0.0] * dim
    for tok in tokens:
        digest = hashlib.sha256(tok.encode("utf-8")).digest()
        for j in range(0, min(len(digest) - 3, 16), 4):
            (n,) = struct.unpack_from("!I", digest, j)
            idx = n % dim
            values[idx] += 1.0 if (n & 1) else -1.0
        digest2 = hashlib.md5(tok.encode("utf-8")).digest()
        (n2,) = struct.unpack_from("!I", digest2, 0)
        values[n2 % dim] += 0.5 if (n2 & 1) else -0.5
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _resize(vec: list[float], dim: int) -> list[float]:
    if len(vec) == dim:
        return vec
    if len(vec) > dim:
        return vec[:dim]
    return vec + [0.0] * (dim - len(vec))
