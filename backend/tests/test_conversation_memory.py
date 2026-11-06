from uuid import uuid4

from app.redis_client import ConversationMemory


class FakeRedis:
    """In-memory Redis replacement for unit tests."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def setex(self, key: str, ttl: int, value: str) -> bool:
        self.data[key] = value
        self.ttls[key] = ttl
        return True

    def delete(self, key: str) -> int:
        existed = key in self.data

        self.data.pop(key, None)
        self.ttls.pop(key, None)

        return 1 if existed else 0


def test_conversation_memory_key() -> None:
    memory = ConversationMemory()
    tenant_id = uuid4()
    conversation_id = uuid4()

    assert memory.key(tenant_id, conversation_id) == (
        f"conversation:{tenant_id}:{conversation_id}"
    )


def test_conversation_memory_set_and_get(monkeypatch) -> None:
    fake_redis = FakeRedis()

    monkeypatch.setattr(
        "app.redis_client.get_redis",
        lambda: fake_redis,
    )

    memory = ConversationMemory()
    tenant_id = uuid4()
    conversation_id = uuid4()

    result = memory.set(
        tenant_id,
        conversation_id,
        last_sql="SELECT COUNT(*) FROM customers",
        filters=["active"],
        tables=["customers"],
        metrics=["customer_count"],
    )

    assert result is True

    stored = memory.get(tenant_id, conversation_id)

    assert stored == {
        "last_sql": "SELECT COUNT(*) FROM customers",
        "filters": ["active"],
        "tables": ["customers"],
        "metrics": ["customer_count"],
        "messages": [],
        "dashboard": None,
        "analysis_state": None,
    }


def test_conversation_memory_update_preserves_existing_values(monkeypatch) -> None:
    fake_redis = FakeRedis()

    monkeypatch.setattr(
        "app.redis_client.get_redis",
        lambda: fake_redis,
    )

    memory = ConversationMemory()
    tenant_id = uuid4()
    conversation_id = uuid4()

    memory.set(
        tenant_id,
        conversation_id,
        last_sql="SELECT * FROM orders",
        filters=["2026"],
        tables=["orders"],
        metrics=["sales"],
    )

    result = memory.update(
        tenant_id,
        conversation_id,
        last_sql="SELECT SUM(amount) FROM orders",
    )

    assert result is True

    stored = memory.get(tenant_id, conversation_id)

    assert stored is not None
    assert stored["last_sql"] == "SELECT SUM(amount) FROM orders"
    assert stored["filters"] == ["2026"]
    assert stored["tables"] == ["orders"]
    assert stored["metrics"] == ["sales"]


def test_conversation_memory_ttl(monkeypatch) -> None:
    fake_redis = FakeRedis()

    monkeypatch.setattr(
        "app.redis_client.get_redis",
        lambda: fake_redis,
    )

    memory = ConversationMemory()
    tenant_id = uuid4()
    conversation_id = uuid4()

    memory.set(
        tenant_id,
        conversation_id,
        last_sql="SELECT 1",
        ttl_seconds=600,
    )

    key = memory.key(tenant_id, conversation_id)

    assert fake_redis.ttls[key] == 600


def test_conversation_memory_invalidate(monkeypatch) -> None:
    fake_redis = FakeRedis()

    monkeypatch.setattr(
        "app.redis_client.get_redis",
        lambda: fake_redis,
    )

    memory = ConversationMemory()
    tenant_id = uuid4()
    conversation_id = uuid4()

    memory.set(
        tenant_id,
        conversation_id,
        last_sql="SELECT 1",
    )

    assert memory.get(tenant_id, conversation_id) is not None

    result = memory.invalidate(tenant_id, conversation_id)

    assert result is True
    assert memory.get(tenant_id, conversation_id) is None


def test_conversation_memory_missing_returns_none(monkeypatch) -> None:
    fake_redis = FakeRedis()

    monkeypatch.setattr(
        "app.redis_client.get_redis",
        lambda: fake_redis,
    )

    memory = ConversationMemory()
    tenant_id = uuid4()
    conversation_id = uuid4()

    assert memory.get(tenant_id, conversation_id) is None


def test_conversation_memory_tenant_isolation(monkeypatch) -> None:
    fake_redis = FakeRedis()

    monkeypatch.setattr(
        "app.redis_client.get_redis",
        lambda: fake_redis,
    )

    memory = ConversationMemory()
    tenant_a = uuid4()
    tenant_b = uuid4()
    conversation_id = uuid4()

    memory.set(
        tenant_a,
        conversation_id,
        last_sql="SELECT secret FROM tenant_a",
    )

    assert memory.get(tenant_a, conversation_id) is not None
    assert memory.get(tenant_b, conversation_id) is None
