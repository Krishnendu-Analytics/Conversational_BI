"""Tenant-scoped storage for uploaded RAG documents."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from app.config import get_settings
from app.services.file_storage import sanitize_filename


def _root() -> Path:
    root = Path(get_settings().document_storage_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def document_dir(tenant_id: UUID, document_id: UUID) -> Path:
    path = _root() / str(tenant_id) / str(document_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_document_bytes(
    tenant_id: UUID,
    document_id: UUID,
    *,
    filename: str,
    content: bytes,
) -> tuple[str, str]:
    """Persist bytes under tenant/document id. Returns (relative path, sha256)."""
    safe = sanitize_filename(filename)
    dest = document_dir(tenant_id, document_id) / safe
    dest.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    # Store path relative to storage root for portability
    rel = str(dest.relative_to(_root()))
    return rel, checksum


def absolute_storage_path(relative: str) -> Path:
    path = (_root() / relative).resolve()
    root = _root().resolve()
    if not str(path).startswith(str(root)):
        raise ValueError("Invalid storage path")
    return path


def delete_document_storage(tenant_id: UUID, document_id: UUID) -> None:
    import shutil

    path = _root() / str(tenant_id) / str(document_id)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
