import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.workflow import _format_schema_answer, _format_schema_for_prompt
from app.database import get_db
from app.main import app
from app.models import Metric, SchemaTable, User
from app.services.data_dictionary import (
    DataDictionaryError,
    DictRow,
    apply_to_schema,
    match_to_schema,
    parse_dictionary_source,
    summarize_tables_with_llm,
)
from app.services.file_import import SourceDocument, load_source
from app.services.semantic_discovery import _aggregation_for, _is_identifier_column
from tests.test_rbac import _set_role
from tests.test_semantic_api import _signup_and_headers
from tests.test_tenant_isolation import _create_connection


def _xlsx(rows: list[list[object]]) -> bytes:
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _FakeLLM:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str, max_output_tokens: int | None = None) -> str:
        self.prompts.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


SCHEMA = {
    "tables": [
        {
            "schema_name": "data_mart",
            "table_name": "fact_ar",
            "columns": [
                {"name": "invoice_key", "data_type": "integer"},
                {"name": "outstanding_value", "data_type": "numeric"},
            ],
        },
        {
            "schema_name": "data_mart",
            "table_name": "dim_customer",
            "columns": [{"name": "City", "data_type": "varchar"}],
        },
    ]
}


def _rows(source_rows: list[list[object]]) -> list[DictRow]:
    return parse_dictionary_source(SourceDocument(sheets=[source_rows])).rows


# --- Parsing & matching ---------------------------------------------------------


def test_parse_user_style_dictionary_with_extra_columns() -> None:
    rows = _rows(
        [
            ["table_schema", "table_name", "ordinal_position", "column_name", "Column Description", "data_type"],
            ["data_mart", "fact_ar", 1, "invoice_key", "Surrogate key", "integer"],
            ["data_mart", "fact_ar", 2, "outstanding_value", "Amount still unpaid", "numeric"],
            ["data_mart", "fact_ar", 3, "notes", "", "text"],  # no description: ignored
        ]
    )
    assert [(r.schema_name, r.table_name, r.column_name, r.description) for r in rows] == [
        ("data_mart", "fact_ar", "invoice_key", "Surrogate key"),
        ("data_mart", "fact_ar", "outstanding_value", "Amount still unpaid"),
    ]


def test_parse_table_description_column_and_dotted_column_cell() -> None:
    rows = _rows(
        [
            ["Field", "Table Description", "Description"],
            ["fact_ar.outstanding_value", "Receivables per invoice", "Amount still unpaid"],
            ["fact_ar.invoice_key", "Receivables per invoice", "Key"],
        ]
    )
    assert [(r.table_name, r.column_name, r.description) for r in rows] == [
        ("fact_ar", "", "Receivables per invoice"),  # repeated per row, kept once
        ("fact_ar", "outstanding_value", "Amount still unpaid"),
        ("fact_ar", "invoice_key", "Key"),
    ]


def test_parse_rejects_non_tabular_and_headerless_files() -> None:
    from app.services.file_import import ImportFileError

    with pytest.raises(ImportFileError, match=r"\.csv or \.xlsx"):
        load_source("dictionary.pdf", b"%PDF-1.4")
    with pytest.raises(DataDictionaryError, match="table, column and description"):
        parse_dictionary_source(load_source("d.csv", b"a,b\nc,d\n"))


def test_match_to_schema_uses_canonical_names_and_reports_misses() -> None:
    rows = [
        DictRow("", "FACT_AR", "Outstanding_Value", "Amount still unpaid"),
        DictRow("", "dim_customer", "city", "Customer city"),
        DictRow("", "fact_ar", "missing_col", "x"),
        DictRow("", "fact_ghost", "a", "x"),
        DictRow("", "fact_ghost", "b", "x"),
    ]
    matched = match_to_schema(rows, SCHEMA)
    assert [(r.schema_name, r.table_name, r.column_name) for r in matched.rows] == [
        ("data_mart", "fact_ar", "outstanding_value"),
        ("data_mart", "dim_customer", "City"),
    ]
    assert matched.skipped == [
        (None, 'table "fact_ghost" not found in this connection (2 rows)'),
        (None, 'column "missing_col" not found in table "fact_ar"'),
    ]


def test_match_to_schema_ambiguous_table_needs_schema() -> None:
    schema = {
        "tables": [
            {"schema_name": "a", "table_name": "t", "columns": [{"name": "c"}]},
            {"schema_name": "b", "table_name": "t", "columns": [{"name": "c"}]},
        ]
    }
    assert "several schemas" in match_to_schema([DictRow("", "t", "c", "x")], schema).skipped[0][1]
    assert match_to_schema([DictRow("B", "t", "c", "x")], schema).rows[0].schema_name == "b"


# --- AI summaries -----------------------------------------------------------------------


def test_summaries_only_for_undescribed_tables_and_grounded_to_batch() -> None:
    rows = [
        DictRow("data_mart", "fact_ar", "outstanding_value", "Amount still unpaid"),
        DictRow("data_mart", "dim_customer", "City", "Customer city"),
    ]
    existing = {("data_mart", "dim_customer", "")}  # already has a table description
    llm = _FakeLLM(
        '{"tables": ['
        '{"table": "fact_ar", "summary": "Receivables, one row per invoice."},'
        '{"table": "fact_invented", "summary": "Not a real table."}'
        "]}"
    )
    summaries, skipped = summarize_tables_with_llm(SCHEMA, rows, existing, llm)
    assert [(s.table_name, s.column_name, s.description, s.origin) for s in summaries] == [
        ("fact_ar", "", "Receivables, one row per invoice.", "ai"),
    ]
    assert skipped == []
    assert len(llm.prompts) == 1
    assert "- outstanding_value (numeric): Amount still unpaid" in llm.prompts[0]
    assert "dim_customer" not in llm.prompts[0]


def test_summaries_tolerate_llm_failure() -> None:
    rows = [DictRow("data_mart", "fact_ar", "outstanding_value", "Amount still unpaid")]
    summaries, skipped = summarize_tables_with_llm(SCHEMA, rows, set(), _FakeLLM(RuntimeError("down")))
    assert summaries == []
    assert skipped == [(None, "AI could not summarise: fact_ar")]


# --- Feeding the agent ------------------------------------------------------------------


class _Entry:
    def __init__(self, table: str, column: str, description: str) -> None:
        self.schema_name, self.table_name, self.column_name = "data_mart", table, column
        self.description = description


def _described_schema() -> dict:
    schema = json.loads(json.dumps(SCHEMA))
    return apply_to_schema(
        schema,
        [
            _Entry("fact_ar", "", "Receivables, one row per invoice."),
            _Entry("fact_ar", "outstanding_value", "Amount still unpaid on the invoice"),
            _Entry("dim_customer", "city", "Customer city"),
        ],
    )


def test_schema_listing_is_identical_for_every_question() -> None:
    """The listing is the cached prompt prefix: no per-question order or detail."""
    schema = _described_schema()
    a = _format_schema_for_prompt(schema, question="outstanding receivables")
    b = _format_schema_for_prompt(schema, question="customers by city")
    assert a == b == _format_schema_for_prompt(schema)
    assert "# data_mart.fact_ar: Receivables, one row per invoice." in a  # summaries stay
    assert a.index("fact_ar(") < a.index("dim_customer(")  # connection's own order


def test_focus_names_relevant_tables_with_their_column_notes() -> None:
    from app.agent.workflow import _schema_focus_for_prompt

    focus = _schema_focus_for_prompt(_described_schema(), "outstanding receivables")
    assert focus.startswith("Tables most relevant to this question: data_mart.fact_ar")
    assert "- data_mart.fact_ar.outstanding_value: Amount still unpaid on the invoice" in focus
    assert "Customer city" not in focus  # dim_customer isn't relevant here
    assert _schema_focus_for_prompt(_described_schema(), "") == ""


def test_focus_limits_described_tables(monkeypatch) -> None:
    from app.agent import workflow

    monkeypatch.setattr(workflow, "_SCHEMA_PROMPT_DESCRIBED_TABLES", 1)
    focus = workflow._schema_focus_for_prompt(_described_schema(), "outstanding city receivables")
    assert "Amount still unpaid on the invoice" in focus  # fact_ar ranks first
    assert "Customer city" not in focus


def test_prompt_unchanged_without_descriptions() -> None:
    assert _format_schema_for_prompt(SCHEMA) == (
        "data_mart.fact_ar(invoice_key integer, outstanding_value numeric)\n"
        "data_mart.dim_customer(City varchar)"
    )


def test_schema_answer_lists_table_descriptions() -> None:
    answer = _format_schema_answer(_described_schema(), "list the tables")
    assert "- fact_ar — Receivables, one row per invoice." in answer
    assert "- dim_customer" in answer


# --- Discovery heuristics -----------------------------------------------------------


def test_discovery_skips_identifiers_and_averages_rates() -> None:
    assert _is_identifier_column("sales_key", "")
    assert _is_identifier_column("rep_code_num", "Unique internal ID assigned to each rep")
    assert not _is_identifier_column("net_sales_value", "Net sales after discounts")
    assert _aggregation_for("standard_cost_per_mt", "fact_production", "", "SUM({expr})") == "AVG({expr})"
    assert _aggregation_for("standard_cost", "dim_product", "", "SUM({expr})") == "AVG({expr})"
    assert _aggregation_for("net_sales_value", "fact_sales", "", "SUM({expr})") == "SUM({expr})"


# --- API ------------------------------------------------------------------------------


def _db() -> tuple[Session, object]:
    gen = app.dependency_overrides[get_db]()
    return next(gen), gen


def _seed_schema(email: str, connection_id: UUID) -> UUID:
    db, gen = _db()
    try:
        tenant_id = db.scalar(select(User.tenant_id).where(User.email == email))
        for table in SCHEMA["tables"]:
            db.add(
                SchemaTable(
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    schema_name=table["schema_name"],
                    table_name=table["table_name"],
                    columns_json=json.dumps(
                        {
                            "schema_name": table["schema_name"],
                            "columns": [
                                {"name": c["name"], "data_type": c["data_type"], "nullable": True}
                                for c in table["columns"]
                            ],
                            "foreign_keys": [],
                        }
                    ),
                )
            )
        db.commit()
        return tenant_id
    finally:
        next(gen, None)


DICT_XLSX = _xlsx(
    [
        ["table_name", "column_name", "Column Description"],
        ["fact_ar", "outstanding_value", "Amount still unpaid"],
        ["fact_ar", "invoice_key", "Surrogate key"],
        ["dim_customer", "city", "Customer city"],
        ["fact_ghost", "x", "Not in this connection"],
    ]
)


def test_data_dictionary_preview_save_edit_and_schema_merge(
    client: tuple[TestClient, dict[str, str]], monkeypatch
) -> None:
    test_client, sent = client
    email = "dict@acme.com"
    headers = _signup_and_headers(test_client, sent, email)
    conn_id = _create_connection(test_client, headers, monkeypatch)
    tenant_id = _seed_schema(email, conn_id)

    from app.services import data_dictionary

    monkeypatch.setattr(
        data_dictionary,
        "get_llm",
        lambda: _FakeLLM('{"tables": [{"table": "fact_ar", "summary": "Receivables per invoice."}]}'),
    )
    invalidated: list[str] = []
    from app.routes import semantic as semantic_routes

    monkeypatch.setattr(
        semantic_routes.schema_cache,
        "invalidate",
        lambda _t, c: invalidated.append(str(c)) or True,
    )

    preview = test_client.post(
        f"/api/v1/semantic/data-dictionary/import/preview?connection_id={conn_id}",
        headers=headers,
        files={"file": ("dict.xlsx", DICT_XLSX, "application/octet-stream")},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    keys = [(e["table_name"], e["column_name"], e["origin"]) for e in body["entries"]]
    assert ("fact_ar", "", "ai") in keys  # AI summary for fact_ar
    assert ("dim_customer", "City", "import") in keys  # canonical column name
    assert ("dim_customer", "", "ai") not in keys  # fake LLM only summarised fact_ar
    assert body["skipped"] == [
        {"row": None, "reason": 'table "fact_ghost" not found in this connection'}
    ]

    # A tampered client row naming a non-existent column is rejected on save.
    entries = [
        {k: e[k] for k in ("schema_name", "table_name", "column_name", "description", "origin")}
        for e in body["entries"]
    ] + [{"table_name": "fact_ar", "column_name": "hacked", "description": "x"}]
    saved = test_client.post(
        "/api/v1/semantic/data-dictionary/bulk",
        headers=headers,
        json={"connection_id": str(conn_id), "entries": entries},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "created": 4,
        "updated": 0,
        "unchanged": 0,
        "kept_existing": 0,
        "rejected": 1,
    }
    assert invalidated == [str(conn_id)]

    listed = test_client.get(
        f"/api/v1/semantic/data-dictionary?connection_id={conn_id}", headers=headers
    ).json()
    assert len(listed) == 4
    city = next(e for e in listed if e["column_name"] == "City")
    edited = test_client.patch(
        f"/api/v1/semantic/data-dictionary/{city['id']}",
        headers=headers,
        json={"description": "City where the customer is located"},
    )
    assert edited.json()["origin"] == "manual"

    # Stored descriptions reach the schema the agent loads.
    from app.services.schema_discovery_service import get_stored_schema_for_tenant

    db, gen = _db()
    try:
        schema = get_stored_schema_for_tenant(db, conn_id, tenant_id)
    finally:
        next(gen, None)
    fact_ar = next(t for t in schema["tables"] if t["table_name"] == "fact_ar")
    assert fact_ar["description"] == "Receivables per invoice."
    assert {c["name"]: c.get("description") for c in fact_ar["columns"]} == {
        "invoice_key": "Surrogate key",
        "outstanding_value": "Amount still unpaid",
    }

    deleted = test_client.delete(
        f"/api/v1/semantic/data-dictionary/{city['id']}", headers=headers
    )
    assert deleted.status_code == 204
    assert invalidated == [str(conn_id)] * 3


def test_data_dictionary_requires_discovered_schema_and_admin(
    client: tuple[TestClient, dict[str, str]], monkeypatch
) -> None:
    test_client, sent = client
    email = "dict2@acme.com"
    headers = _signup_and_headers(test_client, sent, email)
    conn_id = _create_connection(test_client, headers, monkeypatch)

    no_schema = test_client.post(
        f"/api/v1/semantic/data-dictionary/import/preview?connection_id={conn_id}",
        headers=headers,
        files={"file": ("dict.xlsx", DICT_XLSX, "application/octet-stream")},
    )
    assert no_schema.status_code == 422
    assert "schema discovery" in no_schema.json()["detail"]

    _set_role(email, "analyst", test_client)
    forbidden = test_client.post(
        "/api/v1/semantic/data-dictionary/bulk",
        headers=headers,
        json={"connection_id": str(conn_id), "entries": [{"table_name": "t", "description": "d"}]},
    )
    assert forbidden.status_code == 403
    # Analysts can still read.
    assert (
        test_client.get(
            f"/api/v1/semantic/data-dictionary?connection_id={conn_id}", headers=headers
        ).status_code
        == 200
    )


def test_discovery_uses_dictionary_to_skip_identifier_metrics(
    client: tuple[TestClient, dict[str, str]], monkeypatch
) -> None:
    test_client, sent = client
    email = "dict3@acme.com"
    headers = _signup_and_headers(test_client, sent, email)
    conn_id = _create_connection(test_client, headers, monkeypatch)
    tenant_id = _seed_schema(email, conn_id)

    db, gen = _db()
    try:
        from app.models import DataDictionaryEntry
        from app.models import SchemaTable as ST
        from app.services.semantic_discovery import _heuristic_suggestions

        # "sales_total" would normally become SUM(...) — the dictionary says it's an ID.
        db.add(
            ST(
                tenant_id=tenant_id,
                connection_id=conn_id,
                schema_name="data_mart",
                table_name="fact_sales",
                columns_json=json.dumps(
                    {
                        "columns": [
                            {"name": "sales_total", "data_type": "integer", "nullable": True},
                            {"name": "net_sales_value", "data_type": "numeric", "nullable": True},
                        ],
                        "foreign_keys": [],
                    }
                ),
            )
        )
        db.add(
            DataDictionaryEntry(
                tenant_id=tenant_id,
                connection_id=conn_id,
                schema_name="data_mart",
                table_name="fact_sales",
                column_name="sales_total",
                description="Surrogate key of the sales header",
            )
        )
        db.commit()
        tables = db.scalars(select(ST).where(ST.connection_id == conn_id)).all()
        _heuristic_suggestions(db, tenant_id=tenant_id, connection_id=conn_id, tables=list(tables))
        db.commit()
        expressions = set(
            db.scalars(select(Metric.expression).where(Metric.connection_id == conn_id)).all()
        )
    finally:
        next(gen, None)

    assert "SUM(data_mart.fact_sales.net_sales_value)" in expressions
    assert not any("sales_total" in e for e in expressions)
    assert not any("invoice_key" in e for e in expressions)
