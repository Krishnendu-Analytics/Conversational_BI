"""Universal semantic identity: connection scope, physical mappings, query aliases."""

from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import select

from app.agent.query_plan import QueryPlan
from app.agent.query_plan_validation import validate_query_plan
from app.agent.sql_compiler import _catalog_maps
from app.models import Dimension, Metric, SchemaTable
from app.services.semantic_discovery import (
    _apply_llm_suggestions,
    discover_semantic_suggestions,
)
from app.services.semantic_identity import (
    ambiguous_alias_targets,
    expression_map_for_items,
    field_identity_name,
    humanize_identifier,
)
from app.services.semantic_service import load_connection_semantic_catalog
from tests.test_helpers_discovery import disable_semantic_llm, make_test_db, seed_connection


def _schema_payload(columns: list[tuple[str, str]]) -> str:
    return json.dumps(
        {
            "schema_name": "main",
            "columns": [
                {
                    "name": name,
                    "data_type": dtype,
                    "nullable": True,
                    "is_pk": False,
                }
                for name, dtype in columns
            ],
            "foreign_keys": [],
        }
    )


def _add_table(
    db,
    *,
    tenant_id,
    connection_id,
    table_name: str,
    columns: list[tuple[str, str]],
) -> None:
    db.add(
        SchemaTable(
            tenant_id=tenant_id,
            connection_id=connection_id,
            schema_name="main",
            table_name=table_name,
            columns_json=_schema_payload(columns),
        )
    )


def test_identity_name_qualifies_only_on_collision() -> None:
    used: set[str] = set()
    first = field_identity_name("event_at", table_label="Sheet A", used=used)
    second = field_identity_name("event_at", table_label="Sheet B", used=used)
    assert first == humanize_identifier("event_at")
    assert second != first
    assert "sheet b" in second.lower()


def test_overlapping_labels_across_connections_do_not_collide(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn_a = seed_connection(db, conn_name="File A")
    _, conn_b = seed_connection(db, conn_name="File B", tenant=tenant)
    disable_semantic_llm(monkeypatch)

    _add_table(
        db,
        tenant_id=tenant.id,
        connection_id=conn_a.id,
        table_name=f"t_{uuid4().hex[:8]}",
        columns=[("event_at", "timestamp"), ("attr_label", "varchar")],
    )
    _add_table(
        db,
        tenant_id=tenant.id,
        connection_id=conn_b.id,
        table_name=f"t_{uuid4().hex[:8]}",
        columns=[("event_at", "timestamp"), ("attr_label", "varchar")],
    )
    db.commit()

    discover_semantic_suggestions(db, conn_a.id, tenant.id, use_llm=False)
    discover_semantic_suggestions(db, conn_b.id, tenant.id, use_llm=False)

    dims_a = db.scalars(
        select(Dimension).where(Dimension.connection_id == conn_a.id)
    ).all()
    dims_b = db.scalars(
        select(Dimension).where(Dimension.connection_id == conn_b.id)
    ).all()
    assert len(dims_a) == 2
    assert len(dims_b) == 2
    assert {d.source_column_ref for d in dims_a}.isdisjoint(
        {d.source_column_ref for d in dims_b}
    )


def test_two_physical_columns_keep_distinct_semantic_rows(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)
    disable_semantic_llm(monkeypatch)
    table = f"t_{uuid4().hex[:8]}"
    _add_table(
        db,
        tenant_id=tenant.id,
        connection_id=conn.id,
        table_name=table,
        columns=[("event_at", "timestamp"), ("closed_on", "date")],
    )
    db.commit()

    discover_semantic_suggestions(db, conn.id, tenant.id, use_llm=False)
    dims = db.scalars(select(Dimension).where(Dimension.connection_id == conn.id)).all()
    assert {d.physical_column for d in dims} == {"event_at", "closed_on"}
    assert len({d.name.lower() for d in dims}) == 2
    assert all(d.source_column_ref and table in d.source_column_ref for d in dims)


def test_same_column_name_on_two_tables_is_qualified(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)
    disable_semantic_llm(monkeypatch)
    _add_table(
        db,
        tenant_id=tenant.id,
        connection_id=conn.id,
        table_name="customers",
        columns=[("attr_label", "varchar")],
    )
    _add_table(
        db,
        tenant_id=tenant.id,
        connection_id=conn.id,
        table_name="employees",
        columns=[("attr_label", "varchar")],
    )
    db.commit()

    discover_semantic_suggestions(db, conn.id, tenant.id, use_llm=False)
    dims = db.scalars(select(Dimension).where(Dimension.connection_id == conn.id)).all()
    assert len(dims) == 2
    names = {d.name.lower() for d in dims}
    assert len(names) == 2
    refs = {d.source_column_ref for d in dims}
    assert any("customers" in (ref or "") for ref in refs)
    assert any("employees" in (ref or "") for ref in refs)


def test_rediscovery_is_idempotent_for_same_physical_columns(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)
    disable_semantic_llm(monkeypatch)
    _add_table(
        db,
        tenant_id=tenant.id,
        connection_id=conn.id,
        table_name=f"t_{uuid4().hex[:8]}",
        columns=[("event_at", "date"), ("revenue_amt", "numeric")],
    )
    db.commit()

    discover_semantic_suggestions(db, conn.id, tenant.id, use_llm=False)
    dims_1 = db.scalars(select(Dimension).where(Dimension.connection_id == conn.id)).all()
    metrics_1 = db.scalars(select(Metric).where(Metric.connection_id == conn.id)).all()
    ids = {row.id for row in dims_1 + metrics_1}

    discover_semantic_suggestions(db, conn.id, tenant.id, use_llm=False)
    dims_2 = db.scalars(select(Dimension).where(Dimension.connection_id == conn.id)).all()
    metrics_2 = db.scalars(select(Metric).where(Metric.connection_id == conn.id)).all()
    assert {row.id for row in dims_2 + metrics_2} == ids
    assert len(dims_2) == len(dims_1)
    assert len(metrics_2) == len(metrics_1)


def test_llm_does_not_duplicate_existing_physical_expression(monkeypatch) -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)
    disable_semantic_llm(monkeypatch)
    table = f"t_{uuid4().hex[:8]}"
    _add_table(
        db,
        tenant_id=tenant.id,
        connection_id=conn.id,
        table_name=table,
        columns=[("event_at", "date")],
    )
    db.commit()
    discover_semantic_suggestions(db, conn.id, tenant.id, use_llm=False)

    counts = _apply_llm_suggestions(
        db,
        tenant_id=tenant.id,
        connection_id=conn.id,
        suggestions={
            "dimensions": [
                {
                    "name": "Calendar Field",
                    "expression": f"main.{table}.event_at",
                }
            ]
        },
        column_names={"event_at"},
        column_refs={f"main.{table}.event_at"},
    )
    db.commit()
    assert counts["dimensions"] == 0
    dims = db.scalars(select(Dimension).where(Dimension.connection_id == conn.id)).all()
    assert len(dims) == 1


def test_ambiguous_generic_alias_does_not_compile_to_one_column() -> None:
    catalog = {
        "metrics": [],
        "dimensions": [
            {
                "name": "Event At",
                "expression": "main.events.event_at",
                "label": "Date",
                "synonyms": ["Date"],
                "format": "date",
            },
            {
                "name": "Closed On",
                "expression": "main.events.closed_on",
                "label": "Date",
                "synonyms": ["Date"],
                "format": "date",
            },
        ],
    }
    _metrics, dimensions = _catalog_maps(catalog)
    assert dimensions["event at"] == "main.events.event_at"
    assert dimensions["closed on"] == "main.events.closed_on"
    assert "date" not in dimensions
    assert ambiguous_alias_targets(catalog["dimensions"])["date"] == [
        "Event At",
        "Closed On",
    ]

    result = validate_query_plan(
        QueryPlan(intent="aggregation", metrics=["Revenue"], dimensions=["Date"]),
        semantic_catalog={
            "metrics": [{"name": "Revenue", "expression": "SUM(main.events.amt)"}],
            "dimensions": catalog["dimensions"],
        },
    )
    assert result.ok is False
    assert result.clarification is not None
    assert result.clarification.kind == "pick_dimension"
    assert {c.label for c in result.clarification.choices} == {"Event At", "Closed On"}


def test_unambiguous_generic_alias_resolves_to_the_only_physical_column() -> None:
    catalog = {
        "dimensions": [
            {
                "name": "Event At",
                "expression": "main.events.event_at",
                "label": "Date",
                "synonyms": ["Date"],
                "format": "date",
            }
        ]
    }
    mapped = expression_map_for_items(catalog["dimensions"])
    assert mapped["date"] == "main.events.event_at"
    assert mapped["event at"] == "main.events.event_at"


def test_catalog_payload_includes_synonyms_and_stays_connection_scoped(
    monkeypatch,
) -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)
    disable_semantic_llm(monkeypatch)
    _add_table(
        db,
        tenant_id=tenant.id,
        connection_id=conn.id,
        table_name=f"t_{uuid4().hex[:8]}",
        columns=[("event_at", "date")],
    )
    db.commit()
    discover_semantic_suggestions(db, conn.id, tenant.id, use_llm=False)
    catalog = load_connection_semantic_catalog(
        db, tenant.id, conn.id, include_suggested=True
    )
    assert catalog["dimensions"]
    item = catalog["dimensions"][0]
    assert item["physical_column"] == "event_at"
    assert item["source_column_ref"]
    assert "Date" in (item.get("synonyms") or []) or item.get("label") == "Date"
