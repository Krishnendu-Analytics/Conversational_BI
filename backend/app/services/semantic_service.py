"""Read-only semantic catalog helpers — connection-scoped."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Dimension, Metric, SemanticRelationship
from app.redis_client import SemanticCache
from app.services.semantic_identity import parse_synonyms
from app.services.semantic_rules import load_rules, rule_dict


def _metric_dict(m: Metric) -> dict:
    return {
        "name": m.name,
        "expression": m.expression,
        "label": m.label,
        "description": m.description,
        "format": m.format,
        "synonyms": parse_synonyms(m.synonyms_json),
        "status": m.status,
        "connection_id": str(m.connection_id) if m.connection_id else None,
        "physical_schema": m.physical_schema,
        "physical_table": m.physical_table,
        "physical_column": m.physical_column,
        "source_column_ref": m.source_column_ref,
        "domain": m.domain,
    }


def _dimension_dict(d: Dimension) -> dict:
    return {
        "name": d.name,
        "expression": d.expression,
        "label": d.label,
        "format": d.format,
        "synonyms": parse_synonyms(d.synonyms_json),
        "status": d.status,
        "connection_id": str(d.connection_id) if d.connection_id else None,
        "physical_schema": d.physical_schema,
        "physical_table": d.physical_table,
        "physical_column": d.physical_column,
        "source_column_ref": d.source_column_ref,
        "domain": d.domain,
    }


def load_connection_semantic_catalog(
    db: Session,
    tenant_id: UUID,
    connection_id: UUID,
    *,
    semantic_cache: SemanticCache | None = None,
    include_suggested: bool = False,
) -> dict:
    """Load semantic catalog scoped to one connection."""
    cache = semantic_cache or SemanticCache()
    cached = cache.get(tenant_id, connection_id)
    if cached is not None and cached.get("_include_suggested") == include_suggested:
        return cached

    metric_q = select(Metric).where(
        Metric.tenant_id == tenant_id,
        Metric.connection_id == connection_id,
    )
    dim_q = select(Dimension).where(
        Dimension.tenant_id == tenant_id,
        Dimension.connection_id == connection_id,
    )
    if not include_suggested:
        metric_q = metric_q.where(Metric.status == "confirmed")
        dim_q = dim_q.where(Dimension.status == "confirmed")

    metrics = db.scalars(metric_q.order_by(Metric.name)).all()
    dimensions = db.scalars(dim_q.order_by(Dimension.name)).all()
    rel_q = select(SemanticRelationship).where(
        SemanticRelationship.tenant_id == tenant_id,
        SemanticRelationship.connection_id == connection_id,
        SemanticRelationship.status == "confirmed",
    )
    relationships = db.scalars(rel_q).all()

    catalog: dict = {
        "metrics": [_metric_dict(m) for m in metrics],
        "dimensions": [_dimension_dict(d) for d in dimensions],
        "relationships": [
            {
                "from_entity": r.from_entity,
                "to_entity": r.to_entity,
                "relationship_type": r.relationship_type,
                "source_fk_json": r.source_fk_json,
                "status": r.status,
            }
            for r in relationships
        ],
        # Admin-approved business rules, defaults and vocabulary.
        "rules": [rule_dict(r) for r in load_rules(db, tenant_id, connection_id)],
        "_include_suggested": include_suggested,
    }
    try:
        cache.set(tenant_id, connection_id, catalog)
    except Exception:  # noqa: BLE001
        pass
    return catalog


def load_tenant_semantic_catalog(
    db: Session,
    tenant_id: UUID,
    connection_id: UUID | None = None,
    *,
    semantic_cache: SemanticCache | None = None,
    include_suggested: bool = False,
) -> dict:
    """Load catalog; requires connection_id for connection-scoped semantics."""
    if connection_id is None:
        return {"metrics": [], "dimensions": [], "relationships": []}
    return load_connection_semantic_catalog(
        db,
        tenant_id,
        connection_id,
        semantic_cache=semantic_cache,
        include_suggested=include_suggested,
    )
