"""SQLAlchemy type for embeddings: pgvector on Postgres, JSON text elsewhere."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.types import Text, TypeDecorator, UserDefinedType


class _PGVector(UserDefinedType):
    """Minimal pgvector bind without requiring the extension at import time on SQLite."""

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **_kw: Any) -> str:
        return f"vector({self.dim})"

    def bind_processor(self, dialect):  # noqa: ANN001
        def process(value: list[float] | None) -> str | None:
            if value is None:
                return None
            return "[" + ",".join(str(float(x)) for x in value) + "]"

        return process

    def result_processor(self, dialect, coltype):  # noqa: ANN001
        def process(value: Any) -> list[float] | None:
            if value is None:
                return None
            if isinstance(value, list):
                return [float(x) for x in value]
            text = str(value).strip()
            if text.startswith("[") and text.endswith("]"):
                inner = text[1:-1].strip()
                if not inner:
                    return []
                return [float(x) for x in inner.split(",")]
            return list(value)

        return process


class EmbeddingVector(TypeDecorator):
    """Store float vectors; Postgres uses pgvector, SQLite/tests use JSON text."""

    impl = Text
    cache_ok = True

    def __init__(self, dim: int = 384) -> None:
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect):  # noqa: ANN001
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector

                return dialect.type_descriptor(Vector(self.dim))
            except Exception:  # noqa: BLE001
                return dialect.type_descriptor(_PGVector(self.dim))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: list[float] | None, dialect) -> Any:  # noqa: ANN001
        if value is None:
            return None
        if dialect.name == "postgresql":
            return [float(x) for x in value]
        return json.dumps([float(x) for x in value])

    def process_result_value(self, value: Any, dialect) -> list[float] | None:  # noqa: ANN001
        if value is None:
            return None
        if dialect.name == "postgresql":
            return [float(x) for x in value]
        if isinstance(value, str):
            return [float(x) for x in json.loads(value)]
        return [float(x) for x in value]
