"""Excel/CSV header-row detection and grounded semantic suggestions."""

from __future__ import annotations

import io
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import Dimension, Metric, SchemaTable
from app.services.excel_ingestion import parse_excel_sheet
from app.services.semantic_discovery import (
    _apply_llm_suggestions,
    _heuristic_suggestions,
)
from app.services.tabular_headers import (
    choose_header_row_index,
    expression_references_schema_column,
    is_placeholder_column_name,
    split_header_and_data,
)
from tests.test_helpers_discovery import make_test_db, seed_connection


def test_title_row_is_not_chosen_as_header() -> None:
    rows = [
        ["Agent Wise Flexi Report", None, None, None, None],
        ["Agent Name", "Campaign", "Client", "Calls", "Date"],
        ["Ravi", "BL", "Acme", 21, "2024-01-15"],
    ]
    assert choose_header_row_index(rows) == 1
    headers, data = split_header_and_data(rows)
    assert headers[:3] == ["Agent Name", "Campaign", "Client"]
    assert data[0][0] == "Ravi"


def test_standard_first_row_still_wins() -> None:
    rows = [
        ["order_date", "revenue", "region"],
        ["2024-01-01", 100, "North"],
        ["2024-01-02", 80, "South"],
    ]
    assert choose_header_row_index(rows) == 0


def test_placeholder_column_names() -> None:
    assert is_placeholder_column_name("column_2")
    assert is_placeholder_column_name("Column 10")
    assert is_placeholder_column_name("unnamed")
    assert not is_placeholder_column_name("campaign")
    assert not is_placeholder_column_name("dates")


def test_ungrounded_count_distinct_table_rejected() -> None:
    columns = {"agent_name", "dates"}
    refs = {"main.t_abc.agent_name", "main.t_abc.dates"}
    assert not expression_references_schema_column(
        "COUNT(DISTINCT campaigns)",
        column_names=columns,
        column_refs=refs,
    )
    assert expression_references_schema_column(
        "COUNT(*)",
        column_names=columns,
        column_refs=refs,
    )
    assert expression_references_schema_column(
        "COUNT(DISTINCT agent_name)",
        column_names=columns,
        column_refs=refs,
    )


def test_excel_parse_skips_title_and_blank_header_cells() -> None:
    pytest.importorskip("openpyxl")
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Agent Wise"
    ws.append(["Agent Wise Flexi Report", None, None, None, None])
    ws.append(["Agent Name", "Campaign", "Client", None, "Date"])
    ws.append(["Ravi", "BL", "Acme", None, "2024-01-15"])
    buf = io.BytesIO()
    wb.save(buf)

    spec = parse_excel_sheet(buf.getvalue(), "Agent Wise")
    names = [c["internal_name"] for c in spec["columns"]]
    assert names == ["agent_name", "campaign", "client", "date"]
    assert spec["row_count"] == 1
    assert spec["rows"][0][0] == "Ravi"


def test_heuristic_skips_placeholder_columns() -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db, db_type="excel")
    payload = {
        "schema_name": "main",
        "columns": [
            {"name": "column_2", "data_type": "varchar", "nullable": True, "is_pk": False},
            {"name": "column_10", "data_type": "varchar", "nullable": True, "is_pk": False},
            {"name": "dates", "data_type": "date", "nullable": True, "is_pk": False},
            {"name": "agent_name", "data_type": "varchar", "nullable": True, "is_pk": False},
        ],
        "foreign_keys": [],
    }
    db.add(
        SchemaTable(
            tenant_id=tenant.id,
            connection_id=conn.id,
            schema_name="main",
            table_name=f"t_{uuid4().hex[:8]}",
            columns_json=json.dumps(payload),
        )
    )
    db.commit()
    tables = list(db.scalars(select(SchemaTable)).all())
    _heuristic_suggestions(
        db, tenant_id=tenant.id, connection_id=conn.id, tables=tables
    )
    db.commit()

    dims = list(
        db.scalars(
            select(Dimension).where(
                Dimension.tenant_id == tenant.id,
                Dimension.connection_id == conn.id,
            )
        ).all()
    )
    names = {d.name.lower() for d in dims}
    assert "column 2" not in names
    assert "column 10" not in names
    assert any("date" in n for n in names)


def test_llm_suggestions_require_real_columns() -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db, db_type="excel")
    counts = _apply_llm_suggestions(
        db,
        tenant_id=tenant.id,
        connection_id=conn.id,
        suggestions={
            "metrics": [
                {"name": "Campaign Count", "expression": "COUNT(DISTINCT campaigns)"},
                {"name": "Row Count", "expression": "COUNT(*)"},
            ],
            "dimensions": [
                {"name": "Column 2", "expression": "column_2"},
                {"name": "Agent", "expression": "agent_name"},
            ],
        },
        column_names={"agent_name", "dates"},
        column_refs={"main.t_abc.agent_name", "main.t_abc.dates"},
    )
    db.commit()
    assert counts["metrics"] == 1
    assert counts["dimensions"] == 1
    metric_names = {
        m.name
        for m in db.scalars(
            select(Metric).where(Metric.connection_id == conn.id)
        ).all()
    }
    dim_names = {
        d.name
        for d in db.scalars(
            select(Dimension).where(Dimension.connection_id == conn.id)
        ).all()
    }
    assert "Campaign Count" not in metric_names
    assert "Row Count" in metric_names
    assert "Column 2" not in dim_names
    assert "Agent" in dim_names
