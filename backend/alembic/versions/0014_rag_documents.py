"""Phase 5 RAG: document schema with pgvector embeddings.

Revision ID: 0014_rag_documents
Revises: 0013_file_datasets
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_rag_documents"
down_revision: Union[str, None] = "0013_file_datasets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must match Settings.embedding_dimension default
_EMBEDDING_DIM = 384


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column("documents", sa.Column("original_filename", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("content_type", sa.String(length=120), nullable=True))
    op.add_column("documents", sa.Column("storage_path", sa.String(length=500), nullable=True))
    op.add_column("documents", sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("documents", sa.Column("checksum_sha256", sa.String(length=64), nullable=True))
    op.add_column(
        "documents",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
    )
    op.add_column("documents", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "documents",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "documents",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_documents_status", "documents", ["status"])

    op.add_column(
        "document_chunks",
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("document_chunks", sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("document_chunks", sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"))
    op.add_column(
        "document_chunks",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    if bind.dialect.name == "postgresql":
        op.execute(
            f"ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding vector({_EMBEDDING_DIM})"
        )
    else:
        op.add_column("document_chunks", sa.Column("embedding", sa.Text(), nullable=True))

    op.create_index(
        "ix_document_chunks_document_id",
        "document_chunks",
        ["document_id"],
    )
    op.create_index(
        "ix_document_chunks_tenant_document",
        "document_chunks",
        ["tenant_id", "document_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index("ix_document_chunks_tenant_document", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding")
    op.drop_column("document_chunks", "embedding")
    op.drop_column("document_chunks", "created_at")
    op.drop_column("document_chunks", "metadata_json")
    op.drop_column("document_chunks", "token_count")
    op.drop_column("document_chunks", "chunk_index")

    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_column("documents", "updated_at")
    op.drop_column("documents", "created_at")
    op.drop_column("documents", "chunk_count")
    op.drop_column("documents", "error_message")
    op.drop_column("documents", "status")
    op.drop_column("documents", "checksum_sha256")
    op.drop_column("documents", "file_size_bytes")
    op.drop_column("documents", "storage_path")
    op.drop_column("documents", "content_type")
    op.drop_column("documents", "original_filename")
