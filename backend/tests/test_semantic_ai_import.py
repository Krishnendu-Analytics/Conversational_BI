"""AI sheet mapping, AI drafting of a semantic model, and example evaluation.

The LLM is always a fake here: the property under test is that whatever
the model says is checked (headers must exist, formulas must compile on
one real table) before it can become a preview row.
"""

import io
import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

from app.models import SemanticExample
from app.services import semantic_examples, semantic_generation, sheet_mapping
from app.services.file_import import SourceDocument
from app.services.semantic_import_flow import build_preview
from tests.test_helpers_discovery import make_test_db, seed_connection
from tests.test_semantic_api import _signup_and_headers
from tests.test_semantic_model_import import SCHEMA, FakeLLM, _db, _no_real_llm, _seed_schema  # noqa: F401
from tests.test_tenant_isolation import _create_connection

STAR = {
    "tables": [
        {
            "schema_name": "dm",
            "table_name": "fact_sales",
            "columns": [
                {"name": "sales_key", "data_type": "integer", "is_pk": True},
                {"name": "customer_key", "data_type": "integer"},
                {"name": "net_sales_value", "data_type": "numeric"},
            ],
        },
        {
            "schema_name": "dm",
            "table_name": "dim_customer",
            "columns": [
                {"name": "customer_key", "data_type": "integer"},
                {"name": "customer_name", "data_type": "varchar"},
            ],
        },
        {
            "schema_name": "app",
            "table_name": "orders",
            "columns": [
                {"name": "id", "data_type": "integer", "is_pk": True},
                {"name": "account_id", "data_type": "integer"},
            ],
        },
        {
            "schema_name": "app",
            "table_name": "accounts",
            "columns": [{"name": "id", "data_type": "integer", "is_pk": True}],
        },
    ]
}


# --- Sheet mapping -------------------------------------------------------------------------


def _mapping_reply(items: list[dict]) -> str:
    return json.dumps({"sheets": items})


def test_ai_maps_unfamiliar_headers_then_normal_parser_reads_them() -> None:
    source = SourceDocument(
        sheets=[
            [["Measure", "Calc", "Area"], ["Net Sales", "SUM(fact_sales.net_sales_value)", "Sales"]],
            [["Order", "Amount"], ["A-1", 10]],
            [["Thing", "Stuff"], ["x", "y"]],
        ],
        names=["Metrics", "Orders", "Misc"],
    )
    llm = FakeLLM(
        {
            "map its header cells": _mapping_reply(
                [
                    {"sheet": 1, "kind": "kpi", "header_row": 0,
                     "columns": {"name": "Measure", "formula": "Calc", "domain": "Area"}},
                    {"sheet": 2, "kind": "data", "header_row": 0, "columns": {}},
                    {"sheet": 3, "kind": "kpi", "header_row": 0,
                     "columns": {"name": "Thing", "formula": "Invented"}},
                ]
            )
        }
    )
    result = sheet_mapping.map_unrecognised_sheets(source, llm)
    assert result.source.sheets[0][0] == ["kpi_name", "formula", "domain"]
    assert sheet_mapping.recognised(result.source.sheets[0])
    assert result.source.sheets[2] == source.sheets[2]  # untouched
    notes = {n.sheet: n.message for n in result.notes}
    assert notes["Metrics"].startswith("read as KPI catalog by AI")
    assert notes["Orders"] == "looks like business data, not metadata; skipped"
    assert "not in the sheet" in notes["Misc"]
    # Only the header rows and a few sample rows were shown to the model.
    assert "Sheet 1: 'Metrics'" in llm.prompts[0]


def test_recognised_sheets_are_never_sent_to_the_llm() -> None:
    llm = FakeLLM()
    source = SourceDocument(sheets=[[["kpi_name", "formula"], ["A", "SUM(x)"]]])
    sheet_mapping.map_unrecognised_sheets(source, llm)
    assert llm.prompts == []


# --- Drafting --------------------------------------------------------------------------------


def test_infer_relationships_from_column_names() -> None:
    rels = semantic_generation.infer_relationships(STAR)
    assert {(r.from_table, r.from_column, r.to_table, r.to_column) for r in rels} == {
        ("fact_sales", "customer_key", "dim_customer", "customer_key"),
        ("orders", "account_id", "accounts", "id"),
    }
    assert all(r.origin == "inferred" for r in rels)


def test_generate_model_parses_llm_drafts() -> None:
    llm = FakeLLM(
        {
            "defining KPIs": json.dumps(
                {"kpis": [{"name": "Net Sales", "formula": "SUM(fact_sales.net_sales_value)",
                           "description": "Revenue", "domain": "Sales"}]}
            ),
            "documenting a company database": json.dumps(
                {
                    "rules": [{"category": "Sales", "rule": "Revenue is net_sales_value."}],
                    "settings": [{"name": "default_revenue", "value": "fact_sales.net_sales_value"}],
                    "vocabulary": [{"concept": "customer", "terms": ["client", "customer"]}],
                    "examples": [{"question": "Top customers by sales", "tables": ["fact_sales", "dim_customer"]}],
                }
            ),
            "documenting a business database": json.dumps(
                {"tables": [{"table": "dim_customer", "summary": "One row per customer."}]}
            ),
        }
    )
    drafted = semantic_generation.generate_model(STAR, set(semantic_generation.KINDS), llm)
    assert [(k.name, k.origin) for k in drafted.kpis] == [("Net Sales", "ai")]
    assert [(r.kind, r.origin) for r in drafted.rules] == [
        ("rule", "ai"), ("setting", "ai"), ("vocabulary", "ai"),
    ]
    assert drafted.rules[2].terms == ["client"]
    assert drafted.examples[0].tables == ["fact_sales", "dim_customer"]
    assert ("dim_customer", "One row per customer.") in [
        (d.table_name, d.description) for d in drafted.descriptions
    ]
    assert len(drafted.relationships) == 2
    assert drafted.notes == []


def test_generate_model_turns_llm_failure_into_notes() -> None:
    drafted = semantic_generation.generate_model(STAR, {"kpis", "rules"}, FakeLLM())
    assert drafted.kpis == [] and drafted.rules == []
    assert "AI could not draft KPIs" in drafted.notes


def test_flow_drafts_everything_without_a_file_and_checks_it() -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)
    llm = FakeLLM(
        {
            "defining KPIs": json.dumps(
                {"kpis": [
                    {"name": "Net Sales", "formula": "SUM(fact_sales.net_sales_value)"},
                    {"name": "Invented", "formula": "SUM(fact_sales.made_up)"},
                ]}
            ),
        }
    )
    preview = build_preview(
        db, tenant_id=tenant.id, connection_id=conn.id, schema=SCHEMA,
        filename=None, content=None, generate_missing=False, llm=llm,
    )
    by_name = {k.resolved.kpi.name: k for k in preview.kpis}
    assert by_name["Net Sales"].action == "create"
    assert by_name["Invented"].action == "invalid"  # the AI's guess is checked, not trusted
    assert preview.notes[0].startswith("Drafted by AI")


def test_flow_only_drafts_what_the_file_lacks() -> None:
    db = make_test_db()
    tenant, conn = seed_connection(db)
    wb = Workbook()
    ws = wb.active
    ws.append(["kpi_name", "formula"])
    ws.append(["Net Sales", "SUM(fact_sales.net_sales_value)"])
    buf = io.BytesIO()
    wb.save(buf)
    llm = FakeLLM()
    build_preview(
        db, tenant_id=tenant.id, connection_id=conn.id, schema=SCHEMA,
        filename="k.xlsx", content=buf.getvalue(), generate_missing=True, llm=llm,
    )
    assert not any("defining KPIs" in p for p in llm.prompts)
    assert any("documenting a company database" in p for p in llm.prompts)


# --- Examples & evaluation -------------------------------------------------------------------


def test_example_sheet_parse_and_table_resolution() -> None:
    rows, skipped, used = semantic_examples.parse_example_sheets(
        [[
            ["domain", "example_question", "semantic_sources", "key_concepts"],
            ["Sales", "Net sales by customer?", "fact_sales + dim_customer + dimensions", "net sales"],
            ["Sales", "Net sales by customer?", "", ""],
        ]]
    )
    assert used == {0} and skipped == []
    assert len(rows) == 1
    assert rows[0].tables == ["fact_sales", "dim_customer", "dimensions"]
    known, unknown = semantic_examples.resolve_tables(rows[0].tables, SCHEMA)
    assert known == ["data_mart.fact_sales", "data_mart.dim_customer"]
    assert unknown == []  # "dimensions" is a word, not a missing table


@pytest.mark.parametrize(
    ("reply", "rows", "status"),
    [
        (semantic_examples.Answer("SELECT * FROM data_mart.fact_sales JOIN data_mart.dim_customer"), [{}], "pass"),
        (semantic_examples.Answer("SELECT * FROM data_mart.fact_sales"), [{}], "wrong_tables"),
        (semantic_examples.Answer("SELECT * FROM data_mart.fact_sales JOIN data_mart.dim_customer", fallback=True), [], "fallback"),
        (semantic_examples.Answer(None, "no SQL"), [], "no_answer"),
        (semantic_examples.Answer("SELECT broken"), RuntimeError("syntax error"), "error"),
    ],
)
def test_evaluate_one_statuses(reply, rows, status) -> None:
    def execute(_sql):
        if isinstance(rows, Exception):
            raise rows
        return rows

    result = semantic_examples.evaluate_one(
        "Net sales by customer?",
        ["data_mart.fact_sales", "data_mart.dim_customer"],
        SCHEMA,
        answer=lambda _q: reply,
        execute=execute,
    )
    assert result.status == status


def test_generate_preview_save_examples_and_evaluate(
    client: tuple[TestClient, dict[str, str]], monkeypatch, _no_real_llm
) -> None:
    test_client, sent = client
    email = "ai@acme.com"
    headers = _signup_and_headers(test_client, sent, email)
    conn_id = _create_connection(test_client, headers, monkeypatch)
    _seed_schema(email, conn_id)
    _no_real_llm.responses.update(
        {
            "documenting a company database": json.dumps(
                {"examples": [{"question": "Net sales by customer",
                               "tables": ["fact_sales", "dim_customer"], "domain": "Sales"}]}
            ),
        }
    )
    from app.routes import semantic as semantic_routes

    monkeypatch.setattr(semantic_routes, "_readonly_validator", lambda _t, _c: lambda sql: None)

    # No file: everything is drafted from the schema.
    preview = test_client.post(
        f"/api/v1/semantic/model-import/preview?connection_id={conn_id}", headers=headers
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["filename"] == "AI draft"
    assert [e["question"] for e in body["examples"]] == ["Net sales by customer"]
    assert body["examples"][0]["origin"] == "ai"
    # Joins inferred from column names (fact_* -> dim_customer via customer_key).
    assert {r["origin"] for r in body["relationships"]} == {"inferred"}

    saved = test_client.post(
        "/api/v1/semantic/model-import",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "examples": [
                {k: e[k] for k in ("question", "tables", "domain", "notes", "origin")}
                for e in body["examples"]
            ],
            "dictionary": [
                {"table_name": "fact_sales", "description": "One row per sales line.", "origin": "ai"},
                {"table_name": "ghost", "description": "x"},
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["examples"] == {"created": 1, "unchanged": 0, "rejected": 0}
    assert saved.json()["dictionary"]["created"] == 1
    assert saved.json()["dictionary"]["rejected"] == 1

    listed = test_client.get(
        f"/api/v1/semantic/examples?connection_id={conn_id}", headers=headers
    ).json()
    assert listed[0]["tables"] == ["data_mart.fact_sales", "data_mart.dim_customer"]

    def fake_runner(*_args):
        return (
            lambda q: semantic_examples.Answer(
                "SELECT 1 FROM data_mart.fact_sales JOIN data_mart.dim_customer ON 1=1"
            ),
            lambda sql: [{"v": 1}],
        )

    monkeypatch.setattr(semantic_routes, "_example_runner", fake_runner)
    evaluated = test_client.post(
        f"/api/v1/semantic/examples/evaluate?connection_id={conn_id}", headers=headers
    )
    assert evaluated.status_code == 200, evaluated.text
    assert evaluated.json()["passed"] == 1
    result = evaluated.json()["results"][0]
    assert result["status"] == "pass" and "sql" not in result

    deleted = test_client.delete(f"/api/v1/semantic/examples/{listed[0]['id']}", headers=headers)
    assert deleted.status_code == 204
    db, gen = _db()
    try:
        assert db.scalar(select(SemanticExample)) is None
    finally:
        next(gen, None)
