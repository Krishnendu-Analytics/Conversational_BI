"""app.audit.log_agent_audit — audit logging for app.agent.* code paths
that only have raw tenant/user id strings, not a loaded User ORM object."""

from __future__ import annotations

from uuid import uuid4

from app.audit import log_agent_audit


class _FakeDB:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


def test_log_agent_audit_inserts_row_with_valid_ids() -> None:
    db = _FakeDB()
    tenant_id = uuid4()
    user_id = uuid4()

    log_agent_audit(
        db,
        tenant_id=str(tenant_id),
        user_id=str(user_id),
        action="agent.investigation",
        resource_type="conversation",
        resource_id="conv1",
        metadata={"question": "why did sales decline"},
    )

    assert len(db.added) == 1
    row = db.added[0]
    assert row.tenant_id == tenant_id
    assert row.user_id == user_id
    assert row.action == "agent.investigation"
    assert row.resource_type == "conversation"
    assert row.resource_id == "conv1"
    assert "why did sales decline" in row.metadata_json


def test_log_agent_audit_skips_insert_for_invalid_tenant_id() -> None:
    """Non-UUID tenant ids (e.g. from a test/graph state stub) must be
    treated as absent, not raise — this runs inside a best-effort path."""
    db = _FakeDB()

    log_agent_audit(
        db,
        tenant_id="not-a-uuid",
        user_id=None,
        action="agent.investigation",
        resource_type="conversation",
    )

    assert db.added == []


def test_log_agent_audit_allows_missing_user_id() -> None:
    db = _FakeDB()
    tenant_id = uuid4()

    log_agent_audit(
        db,
        tenant_id=str(tenant_id),
        user_id=None,
        action="agent.investigation",
        resource_type="conversation",
    )

    assert len(db.added) == 1
    assert db.added[0].user_id is None
