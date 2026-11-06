from uuid import UUID

from fastapi.testclient import TestClient

from tests.test_tenant_isolation import _create_connection


def _signup_and_headers(
    test_client: TestClient,
    sent: dict[str, str],
    email: str,
) -> dict[str, str]:
    signup = test_client.post(
        "/api/v1/auth/signup",
        json={
            "tenant_name": "Acme",
            "email": email,
            "password": "Password@123",
        },
    )
    assert signup.status_code == 201

    verify = test_client.post(
        "/api/v1/auth/verify-otp",
        json={
            "email": email,
            "otp_code": sent["otp"],
        },
    )
    assert verify.status_code == 200

    return {"Authorization": f"Bearer {verify.json()['access_token']}"}


def test_metric_crud(client: tuple[TestClient, dict[str, str]], monkeypatch) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "metric@acme.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)

    create = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Revenue",
            "expression": "SUM(orders.ord_amt)",
        },
    )
    assert create.status_code == 201
    body = create.json()
    metric_id = UUID(body["id"])
    assert body["name"] == "Revenue"
    assert body["expression"] == "SUM(orders.ord_amt)"

    listed = test_client.get(
        f"/api/v1/semantic/metrics?connection_id={conn_id}",
        headers=headers,
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    got = test_client.get(f"/api/v1/semantic/metrics/{metric_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["name"] == "Revenue"

    patched = test_client.patch(
        f"/api/v1/semantic/metrics/{metric_id}",
        headers=headers,
        json={"expression": "SUM(orders.amount)"},
    )
    assert patched.status_code == 200
    assert patched.json()["expression"] == "SUM(orders.amount)"

    deleted = test_client.delete(
        f"/api/v1/semantic/metrics/{metric_id}",
        headers=headers,
    )
    assert deleted.status_code == 204
    assert (
        test_client.get(f"/api/v1/semantic/metrics/{metric_id}", headers=headers).status_code
        == 404
    )


def test_metric_duplicate_name_conflict(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "dup@acme.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)

    first = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Revenue",
            "expression": "SUM(orders.ord_amt)",
        },
    )
    assert first.status_code == 201

    second = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Revenue",
            "expression": "SUM(billing.charges)",
        },
    )
    assert second.status_code == 409


def test_dimension_crud(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "dim@acme.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)

    dim = test_client.post(
        "/api/v1/semantic/dimensions",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Region",
            "expression": "customers.region",
        },
    )
    assert dim.status_code == 201
    dim_id = dim.json()["id"]

    dims = test_client.get(
        f"/api/v1/semantic/dimensions?connection_id={conn_id}",
        headers=headers,
    )
    assert dims.status_code == 200
    assert dims.json()[0]["name"] == "Region"

    catalog = test_client.get(
        f"/api/v1/semantic?connection_id={conn_id}",
        headers=headers,
    )
    assert catalog.status_code == 200
    payload = catalog.json()
    assert len(payload["dimensions"]) == 1
    assert "glossary" not in payload
    assert payload["metrics"] == []

    assert (
        test_client.delete(
            f"/api/v1/semantic/dimensions/{dim_id}", headers=headers
        ).status_code
        == 204
    )


def test_semantic_tenant_isolation(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers_a = _signup_and_headers(test_client, sent, "a@acme.com")
    conn_a = _create_connection(test_client, headers_a, monkeypatch)
    create = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers_a,
        json={
            "connection_id": str(conn_a),
            "name": "Revenue",
            "expression": "SUM(orders.ord_amt)",
        },
    )
    assert create.status_code == 201
    metric_id = create.json()["id"]

    headers_b = _signup_and_headers(test_client, sent, "b@other.com")
    conn_b = _create_connection(test_client, headers_b, monkeypatch)
    listed = test_client.get(
        f"/api/v1/semantic/metrics?connection_id={conn_b}",
        headers=headers_b,
    )
    assert listed.status_code == 200
    assert listed.json() == []

    missing = test_client.get(
        f"/api/v1/semantic/metrics/{metric_id}",
        headers=headers_b,
    )
    assert missing.status_code == 404


def test_semantic_catalog_route_reads_db_and_edits_clear_agent_cache(
    client: tuple[TestClient, dict[str, str]],
    monkeypatch,
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "cache@acme.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)

    store: dict[str, str] = {}

    class FakeRedis:
        def get(self, key: str):
            return store.get(key)

        def setex(self, key: str, _ttl: int, value: str) -> bool:
            store[key] = value
            return True

        def delete(self, key: str) -> bool:
            store.pop(key, None)
            return True

        def reset(self) -> None:
            pass

    from app import redis_client as rc

    monkeypatch.setattr(rc, "_redis", FakeRedis())
    monkeypatch.setattr(rc, "get_redis", lambda: rc._redis)

    test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Revenue",
            "expression": "SUM(orders.ord_amt)",
        },
    )

    first = test_client.get(
        f"/api/v1/semantic?connection_id={conn_id}",
        headers=headers,
    )
    assert first.status_code == 200
    assert len(first.json()["metrics"]) == 1
    # The route never writes the agent's cache entry (a different shape).
    assert not any(k.startswith("semantic:") for k in store)

    from app.redis_client import semantic_cache

    semantic_cache.set(
        first.json()["metrics"][0]["tenant_id"], conn_id, {"metrics": [], "_include_suggested": False}
    )
    assert any(k.startswith("semantic:") for k in store)

    test_client.post(
        "/api/v1/semantic/dimensions",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Region",
            "expression": "customers.region",
        },
    )
    assert not any(k.startswith("semantic:") for k in store)

    second = test_client.get(
        f"/api/v1/semantic?connection_id={conn_id}",
        headers=headers,
    )
    assert second.status_code == 200
    assert len(second.json()["metrics"]) == 1
    assert len(second.json()["dimensions"]) == 1


def test_metric_domain_curation(
    client: tuple[TestClient, dict[str, str]], monkeypatch
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "domain@acme.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)

    create = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Revenue",
            "expression": "SUM(orders.ord_amt)",
            "domain": "  Sales  ",
        },
    )
    assert create.status_code == 201
    metric_id = create.json()["id"]
    assert create.json()["domain"] == "Sales"
    url = f"/api/v1/semantic/metrics/{metric_id}"

    # Domain alone is a valid patch.
    renamed = test_client.patch(url, headers=headers, json={"domain": "Sales & Margin"})
    assert renamed.status_code == 200
    assert renamed.json()["domain"] == "Sales & Margin"

    # Omitting domain leaves the curated value untouched.
    confirmed = test_client.patch(url, headers=headers, json={"status": "confirmed"})
    assert confirmed.status_code == 200
    assert confirmed.json()["domain"] == "Sales & Margin"
    assert confirmed.json()["status"] == "confirmed"

    # Blank and null both clear back to auto-derived.
    blank = test_client.patch(url, headers=headers, json={"domain": "   "})
    assert blank.status_code == 200
    assert blank.json()["domain"] is None
    test_client.patch(url, headers=headers, json={"domain": "Sales"})
    cleared = test_client.patch(url, headers=headers, json={"domain": None})
    assert cleared.status_code == 200
    assert cleared.json()["domain"] is None

    listed = test_client.get(
        f"/api/v1/semantic/metrics?connection_id={conn_id}", headers=headers
    )
    assert listed.json()[0]["domain"] is None


def test_metric_domain_validation(
    client: tuple[TestClient, dict[str, str]], monkeypatch
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "domainval@acme.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)

    too_long = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Revenue",
            "expression": "SUM(orders.ord_amt)",
            "domain": "x" * 121,
        },
    )
    assert too_long.status_code == 422

    create = test_client.post(
        "/api/v1/semantic/metrics",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Revenue",
            "expression": "SUM(orders.ord_amt)",
        },
    )
    assert create.status_code == 201
    assert create.json()["domain"] is None

    empty = test_client.patch(
        f"/api/v1/semantic/metrics/{create.json()['id']}", headers=headers, json={}
    )
    assert empty.status_code == 422


def test_dimension_domain_curation(
    client: tuple[TestClient, dict[str, str]], monkeypatch
) -> None:
    test_client, sent = client
    headers = _signup_and_headers(test_client, sent, "dimdomain@acme.com")
    conn_id = _create_connection(test_client, headers, monkeypatch)

    create = test_client.post(
        "/api/v1/semantic/dimensions",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "name": "Warehouse",
            "expression": "stock.wh_code",
        },
    )
    assert create.status_code == 201
    assert create.json()["domain"] is None
    url = f"/api/v1/semantic/dimensions/{create.json()['id']}"

    patched = test_client.patch(url, headers=headers, json={"domain": "Inventory"})
    assert patched.status_code == 200
    assert patched.json()["domain"] == "Inventory"
    assert test_client.get(url, headers=headers).json()["domain"] == "Inventory"

    catalog = test_client.get(
        f"/api/v1/semantic?connection_id={conn_id}", headers=headers
    )
    assert catalog.status_code == 200
    assert catalog.json()["dimensions"][0]["domain"] == "Inventory"
