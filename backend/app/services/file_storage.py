"""Tenant-scoped file storage for uploaded datasets."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import UUID

from app.config import get_settings


def _storage_root() -> Path:
    root = Path(get_settings().file_storage_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def sanitize_filename(name: str) -> str:
    """Strip path components and unsafe characters from display names."""
    base = Path(name).name
    cleaned = re.sub(r"[^\w\s\-.]", "_", base).strip()
    return cleaned[:200] or "upload"


def tenant_dir(tenant_id: UUID) -> Path:
    path = _storage_root() / str(tenant_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def connection_dir(tenant_id: UUID, connection_id: UUID) -> Path:
    path = tenant_dir(tenant_id) / str(connection_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_upload(
    tenant_id: UUID,
    connection_id: UUID,
    *,
    filename: str,
    content: bytes,
) -> tuple[Path, str]:
    """Persist raw upload; returns path and sha256 checksum."""
    safe = sanitize_filename(filename)
    dest = connection_dir(tenant_id, connection_id) / safe
    dest.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    return dest, checksum


def duckdb_path_for_connection(tenant_id: UUID, connection_id: UUID) -> Path:
    return connection_dir(tenant_id, connection_id) / "dataset.duckdb"


def delete_connection_storage(tenant_id: UUID, connection_id: UUID) -> None:
    import shutil

    path = tenant_dir(tenant_id) / str(connection_id)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
