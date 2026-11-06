"""Tests for Redis schema cache (T-43)."""

from uuid import uuid4

from app.redis_client import SchemaCache, get_redis


class _MemoryRedis:
    """Minimal stand-in for redis.Redis used in unit tests."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> bool:
        self.store[key] = value
        return True

    def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    def ping(self) -> bool:
        return True


def test_schema_cache_roundtrip(monkeypatch) -> None:
    memory = _MemoryRedis()
    client = get_redis()
    client.reset()
    monkeypatch.setattr(client, "_connect", lambda: memory)
    monkeypatch.setattr(client, "_failed", False)

    cache = SchemaCache()
    tenant_id = uuid4()
    connection_id = uuid4()
    payload = {
        "connection_id": str(connection_id),
        "tables": [
            {
                "table_name": "customers",
                "columns": [
                    {
                        "name": "id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    }
                ],
                "foreign_keys": [],
            }
        ],
    }

    assert cache.get(tenant_id, connection_id) is None
    assert cache.set(tenant_id, connection_id, payload) is True

    hit = cache.get(tenant_id, connection_id)
    assert hit is not None
    assert hit["tables"][0]["table_name"] == "customers"
    assert hit["connection_id"] == str(connection_id)

    assert cache.invalidate(tenant_id, connection_id) is True
    assert cache.get(tenant_id, connection_id) is None


def test_schema_cache_key_is_tenant_scoped() -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    connection_id = uuid4()
    assert SchemaCache.key(tenant_a, connection_id) != SchemaCache.key(
        tenant_b, connection_id
    )
    assert SchemaCache.key(tenant_a, connection_id).startswith(f"schema:{tenant_a}:")
