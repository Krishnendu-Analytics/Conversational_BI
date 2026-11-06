"""P1-5: File dataset upload, ingestion, security, and query integration."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import get_db
from app.encryption import CredentialEncryption
from app.models import Connection, Metric
from app.services.dataset_service import resolve_active_dataset
from app.tools.customer_db import ProviderFactory
from app.tools.sql_safety import SQLSafetyPipeline
from tests.test_tenant_isolation import _signup_and_token


def _query_file_connection(
    test_client: TestClient,
    connection_id: str,
    sql: str,
) -> list[dict]:
    gen = test_client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        conn = db.scalar(
            select(Connection).where(Connection.id == UUID(str(connection_id)))
        )
        assert conn is not None
        encryptor = CredentialEncryption()
        provider = ProviderFactory.create(conn.db_type, encryptor.decrypt(conn.encrypted_url))
        return provider.execute_read_only_query(sql)
    finally:
        gen.close()

ELECTIONS_CSV = b"""candidate,votes,constituency,election_date
Alice,1200,North,2024-01-15
Bob,980,South,2024-01-15
Carol,1500,North,2024-01-15
"""

SALES_CSV = b"""order_date,revenue,region,product
2024-03-01,100.50,North,Widget
2024-03-02,200.00,South,Gadget
2024-03-03,150.25,North,Widget
"""

COMPARISON_SALES_CSV = b"""order_date,revenue,region
2025-06-01,100,North
2025-12-01,120,North
2026-06-01,150,North
2026-08-01,180,North
2025-06-01,80,South
2026-06-01,90,South
"""

HEALTHCARE_CSV = b"""hospital,admissions,department,admission_date
General,42,Emergency,2024-02-01
City,35,Surgery,2024-02-02
"""


@pytest.fixture()
def file_storage_tmp(monkeypatch: pytest.MonkeyPatch) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="p15_datasets_"))
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp))
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    yield tmp
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    get_settings.cache_clear()


def _upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    filename: str,
    content: bytes,
) -> dict:
    resp = client.post(
        "/api/v1/datasets/upload",
        headers=headers,
        data={"name": name},
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_csv_upload_schema_and_query(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp: Path,
) -> None:
    test_client, sent = client
    headers, _token, tenant_id, _user = _signup_and_token(
        test_client, sent, "csv-uploader@acme.com"
    )

    body = _upload(
        test_client,
        headers,
        name="Elections",
        filename="elections.csv",
        content=ELECTIONS_CSV,
    )
    assert body["discovery_status"] in {"completed", "partial"}
    assert len(body["datasets"]) == 1
    connection_id = body["connection_id"]
    dataset = body["datasets"][0]
    assert dataset["status"] == "ready"
    assert dataset["row_count"] == 3

    rows = test_client.get(
        f"/api/v1/datasets?connection_id={connection_id}",
        headers=headers,
    )
    assert rows.status_code == 200
    assert len(rows.json()["datasets"]) == 1

    physical = dataset["physical_table"]
    sql = f'SELECT candidate, SUM(votes) AS total_votes FROM "{physical}" GROUP BY candidate ORDER BY 2 DESC'
    safe_sql = SQLSafetyPipeline(db_type="csv").validate(sql)
    result = _query_file_connection(test_client, connection_id, safe_sql)
    assert len(result) == 3
    assert result[0]["total_votes"] == 1500


def test_cross_tenant_dataset_access_denied(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp: Path,
) -> None:
    test_client, sent = client
    headers_a, _t, _tenant_a, _user_a = _signup_and_token(
        test_client, sent, "tenant-a-ds@acme.com", tenant_name="TenantA"
    )
    headers_b, _t, _tenant_b, _user_b = _signup_and_token(
        test_client, sent, "tenant-b-ds@other.com", tenant_name="TenantB"
    )

    body = _upload(
        test_client,
        headers_a,
        name="Sales",
        filename="sales.csv",
        content=SALES_CSV,
    )
    dataset_id = body["datasets"][0]["id"]

    denied = test_client.get(f"/api/v1/datasets/{dataset_id}", headers=headers_b)
    assert denied.status_code == 404

    delete_attempt = test_client.delete(
        f"/api/v1/datasets/{dataset_id}",
        headers=headers_b,
    )
    assert delete_attempt.status_code == 404


def test_malformed_and_oversized_rejected(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client, sent = client
    headers, _t, _tenant, _user = _signup_and_token(
        test_client, sent, "reject@acme.com"
    )

    bad = test_client.post(
        "/api/v1/datasets/upload",
        headers=headers,
        data={"name": "Bad"},
        files={"file": ("bad.txt", io.BytesIO(b"x"), "text/plain")},
    )
    assert bad.status_code == 400

    monkeypatch.setenv("FILE_MAX_SIZE_BYTES", "10")
    get_settings.cache_clear()
    big = test_client.post(
        "/api/v1/datasets/upload",
        headers=headers,
        data={"name": "Big"},
        files={"file": ("big.csv", io.BytesIO(b"a,b\n" + b"1,2\n" * 20), "text/csv")},
    )
    assert big.status_code == 400


def test_excel_multi_sheet_and_dataset_ambiguity(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp: Path,
) -> None:
    pytest.importorskip("openpyxl")
    from openpyxl import Workbook

    test_client, sent = client
    headers, _t, tenant_id, _user = _signup_and_token(
        test_client, sent, "excel@acme.com"
    )

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Sales"
    ws1.append(["order_date", "revenue", "region"])
    ws1.append(["2024-01-01", 100, "North"])
    ws2 = wb.create_sheet("Inventory")
    ws2.append(["sku", "quantity"])
    ws2.append(["A1", 5])

    buf = io.BytesIO()
    wb.save(buf)
    content = buf.getvalue()

    body = _upload(
        test_client,
        headers,
        name="Workbook",
        filename="multi.xlsx",
        content=content,
    )
    assert len(body["datasets"]) == 2
    connection_id = UUID(body["connection_id"])

    from app.database import get_db

    gen = test_client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        ds, clarify = resolve_active_dataset(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
        )
        assert ds is None
        assert clarify is not None
        assert "multiple datasets" in clarify.lower()

        ds2, clarify2 = resolve_active_dataset(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            dataset_hint="Inventory",
        )
        assert ds2 is not None
        assert ds2.sheet_name == "Inventory"
        assert clarify2 is None
    finally:
        gen.close()


@pytest.mark.parametrize(
    ("csv_bytes", "filename"),
    [
        (SALES_CSV, "sales.csv"),
        (HEALTHCARE_CSV, "healthcare.csv"),
    ],
)
def test_domain_agnostic_csv_ingestion(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp: Path,
    csv_bytes: bytes,
    filename: str,
) -> None:
    test_client, sent = client
    headers, _t, _tenant_id, _user = _signup_and_token(
        test_client, sent, f"domain-{filename}@acme.com"
    )
    body = _upload(test_client, headers, name=filename, filename=filename, content=csv_bytes)
    assert body["datasets"][0]["status"] == "ready"
    assert body["semantic_counts"]["metrics"] >= 0


def test_sql_injection_in_column_names_safe(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp: Path,
) -> None:
    test_client, sent = client
    headers, _t, tenant_id, _user = _signup_and_token(
        test_client, sent, "sqli@acme.com"
    )
    malicious = b'"select * from secrets; --",amount\n1,10\n'
    body = _upload(
        test_client,
        headers,
        name="Malicious",
        filename="bad.csv",
        content=malicious,
    )
    physical = body["datasets"][0]["physical_table"]
    sql = f'SELECT COUNT(*) AS c FROM "{physical}"'
    safe = SQLSafetyPipeline(db_type="csv").validate(sql)
    rows = _query_file_connection(test_client, body["connection_id"], safe)
    assert rows[0]["c"] == 1


def test_semantic_suggestions_grounded_in_physical_columns(
    client: tuple[TestClient, dict[str, str]],
    file_storage_tmp: Path,
) -> None:
    test_client, sent = client
    headers, _t, tenant_id, _user = _signup_and_token(
        test_client, sent, "semantic@acme.com"
    )
    body = _upload(
        test_client,
        headers,
        name="Elections Sem",
        filename="elections.csv",
        content=ELECTIONS_CSV,
    )
    connection_id = UUID(body["connection_id"])

    from app.database import get_db

    gen = test_client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        metrics = db.scalars(
            select(Metric).where(
                Metric.tenant_id == tenant_id,
                Metric.connection_id == connection_id,
            )
        ).all()
        vote_metrics = [m for m in metrics if "vote" in m.name.lower() and m.source_column_ref]
        assert vote_metrics
        for metric in vote_metrics:
            assert "votes" in metric.source_column_ref.lower()
    finally:
        gen.close()
