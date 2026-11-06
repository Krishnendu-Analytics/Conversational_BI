"""Connection lifecycle helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import (
    Connection,
    Conversation,
    Dashboard,
    DashboardVersion,
    DashboardWidget,
    DataDictionaryEntry,
    Dataset,
    DepartmentGrant,
    Dimension,
    Metric,
    SchemaDiscoveryRun,
    SchemaTable,
    SemanticEntity,
    SemanticExample,
    SemanticRelationship,
    SemanticRule,
)
from app.redis_client import ConversationMemory, schema_cache, semantic_cache
from app.services.file_storage import delete_connection_storage
from app.source_types import SourceType


def _tenant_connection_filter(model, *, connection_id: UUID, tenant_id: UUID):
    return (model.connection_id == connection_id, model.tenant_id == tenant_id)


def purge_connection_dependents(
    db: Session,
    *,
    connection_id: UUID,
    tenant_id: UUID,
) -> None:
    """Remove or detach rows that reference a connection. Caller commits."""
    dashboard_ids = list(
        db.scalars(
            select(Dashboard.id).where(
                Dashboard.connection_id == connection_id,
                Dashboard.tenant_id == tenant_id,
            )
        ).all()
    )
    if dashboard_ids:
        db.execute(
            update(Conversation)
            .where(Conversation.dashboard_id.in_(dashboard_ids))
            .values(dashboard_id=None)
        )
        db.execute(
            update(Dashboard)
            .where(Dashboard.id.in_(dashboard_ids))
            .values(conversation_id=None)
        )
        db.execute(
            delete(DashboardWidget).where(DashboardWidget.dashboard_id.in_(dashboard_ids))
        )
        db.execute(
            delete(DashboardVersion).where(DashboardVersion.dashboard_id.in_(dashboard_ids))
        )
        db.execute(delete(Dashboard).where(Dashboard.id.in_(dashboard_ids)))

    conversation_ids = list(
        db.scalars(
            select(Conversation.id).where(
                Conversation.connection_id == connection_id,
                Conversation.tenant_id == tenant_id,
            )
        ).all()
    )
    if conversation_ids:
        now = datetime.now(timezone.utc)
        db.execute(
            update(Conversation)
            .where(Conversation.id.in_(conversation_ids))
            .values(connection_id=None, deleted_at=now)
        )
        memory = ConversationMemory()
        for conversation_id in conversation_ids:
            memory.invalidate(tenant_id, conversation_id)

    db.execute(
        delete(SchemaDiscoveryRun).where(
            *_tenant_connection_filter(
                SchemaDiscoveryRun, connection_id=connection_id, tenant_id=tenant_id
            )
        )
    )
    db.execute(
        delete(Metric).where(
            *_tenant_connection_filter(Metric, connection_id=connection_id, tenant_id=tenant_id)
        )
    )
    db.execute(
        delete(DataDictionaryEntry).where(
            *_tenant_connection_filter(
                DataDictionaryEntry, connection_id=connection_id, tenant_id=tenant_id
            )
        )
    )
    db.execute(
        delete(Dimension).where(
            *_tenant_connection_filter(
                Dimension, connection_id=connection_id, tenant_id=tenant_id
            )
        )
    )
    db.execute(
        delete(SemanticExample).where(
            *_tenant_connection_filter(
                SemanticExample, connection_id=connection_id, tenant_id=tenant_id
            )
        )
    )
    db.execute(
        delete(SemanticRule).where(
            *_tenant_connection_filter(
                SemanticRule, connection_id=connection_id, tenant_id=tenant_id
            )
        )
    )
    db.execute(
        delete(SemanticRelationship).where(
            *_tenant_connection_filter(
                SemanticRelationship, connection_id=connection_id, tenant_id=tenant_id
            )
        )
    )
    db.execute(
        delete(SemanticEntity).where(
            *_tenant_connection_filter(
                SemanticEntity, connection_id=connection_id, tenant_id=tenant_id
            )
        )
    )
    db.execute(
        delete(SchemaTable).where(
            *_tenant_connection_filter(
                SchemaTable, connection_id=connection_id, tenant_id=tenant_id
            )
        )
    )
    db.execute(
        delete(Dataset).where(
            *_tenant_connection_filter(Dataset, connection_id=connection_id, tenant_id=tenant_id)
        )
    )
    db.execute(
        delete(DepartmentGrant).where(
            *_tenant_connection_filter(
                DepartmentGrant, connection_id=connection_id, tenant_id=tenant_id
            )
        )
    )


def delete_connection_record(db: Session, connection: Connection) -> None:
    """Delete a connection and every tenant row that still points at it.

    Caller is responsible for audit logging and commit.
    """
    tenant_id = connection.tenant_id
    connection_id = connection.id
    file_backed = SourceType.is_file_backed(connection.db_type)

    purge_connection_dependents(
        db,
        connection_id=connection_id,
        tenant_id=tenant_id,
    )
    if file_backed:
        delete_connection_storage(tenant_id, connection_id)

    db.delete(connection)
    schema_cache.invalidate(tenant_id, connection_id)
    semantic_cache.invalidate(tenant_id, connection_id)
