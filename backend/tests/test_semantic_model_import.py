import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.join_planner import build_join_clauses
from app.database import get_db
from app.main import app
from app.models import Metric, SchemaTable, SemanticRelationship, User
from app.services import semantic_model_import as smi
from app.services.file_import import SourceDocument
from tests.test_semantic_api import _signup_and_headers
from tests.test_tenant_isolation import _create_connection


def _col(name: str, dtype: str = "numeric") -> dict:
    return {"name": name, "data_type": dtype}


SCHEMA = {
    "tables": [
        {
            "schema_name": "data_mart",
            "table_name": "fact_sales",
            "columns": [
                _col("sales_key", "integer"),
                _col("customer_key", "integer"),
                _col("net_sales_value"),
                _col("gross_profit"),
                _col("quantity_mt"),
                _col("margin_percent"),
                _col("list_price"),
                _col("on_time_flag", "integer"),
            ],
            "foreign_keys": [],
        },
        {
            "schema_name": "data_mart",
            "table_name": "fact_dispatch",
            "columns": [
                _col("dispatch_key", "integer"),
                _col("customer_key", "integer"),
                _col("on_time_flag", "integer"),
            ],
            "foreign_keys": [],
        },
        {
            "schema_name": "data_mart",
            "table_name": "fact_ar",
            "columns": [
                _col("outstanding_value"),
                _col("overdue_days", "integer"),
            ],
            "foreign_keys": [],
        },
        {
            "schema_name": "data_mart",
            "table_name": "dim_customer",
            "columns": [_col("customer_key", "integer"), _col("customer_name", "varchar")],
            "foreign_keys": [],
        },
    ]
}


class FakeLLM:
    """Answers by prompt type; never calls a real model."""

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = responses or {}
        self.prompts: list[str] = []

    def complete(self, prompt: str, max_output_tokens: int | None = None) -> str:
        self.prompts.append(prompt)
        for marker, response in self.responses.items():
            if marker in prompt:
                return response
        return ""


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr("app.llm.client.get_llm", lambda: fake)
    return fake


def _kpi(formula: str, name: str = "KPI") -> smi.KpiRow:
    return smi.KpiRow(name=name, formula=formula)


# --- Parsing -----------------------------------------------------------------------


def test_parse_finds_kpi_and_relationship_sheets_among_others() -> None:
    source = SourceDocument(
        sheets=[
            [["section", "content"], ["Purpose", "Readme text"]],
            [
                ["kpi_id", "kpi_name", "domain", "formula", "business_rule", "aggregation_logic"],
                ["K1", "Net Sales", "Sales", "SUM(fact_sales.net_sales_value)", "After discounts", "SUM"],
                ["K2", "Net Sales", "Sales", "SUM(x)", "duplicate name", "SUM"],
                ["K3", "No formula", "Sales", "", "", ""],
            ],
            [
                ["from_table", "from_column", "to_table", "to_column", "cardinality", "join_rule"],
                ["fact_sales", "customer_key", "dim_customer", "customer_key", "many-to-one", ""],
                ["", "", "", "", "", "fact_dispatch.customer_key = dim_customer.customer_key"],
            ],
        ]
    )
    parsed = smi.parse_semantic_model(source)
    assert [(k.name, k.domain, k.description) for k in parsed.kpis] == [
        ("Net Sales", "Sales", "After discounts")
    ]
    assert [(r.from_table, r.to_table, r.cardinality) for r in parsed.relationships] == [
        ("fact_sales", "dim_customer", "many-to-one"),
        ("fact_dispatch", "dim_customer", ""),
    ]
    assert parsed.skipped == [(4, "KPI row needs both a name and a formula")]


def test_parse_rejects_file_without_kpi_or_relationship_sheet() -> None:
    with pytest.raises(smi.SemanticModelError):
        smi.parse_semantic_model(
            SourceDocument(sheets=[[["table_name", "column_name", "description"], ["t", "c", "d"]]])
        )


# --- KPI formulas --------------------------------------------------------------------


def test_resolve_qualifies_bare_columns_and_keeps_ratio() -> None:
    r = smi.resolve_kpi(
        _kpi("SUM(gross_profit) / NULLIF(SUM(net_sales_value),0) * 100"), SCHEMA
    )
    assert r.error is None
    assert r.expression == (
        "SUM(data_mart.fact_sales.gross_profit) / "
        "NULLIF(SUM(data_mart.fact_sales.net_sales_value),0) * 100"
    )
    assert r.table == "data_mart.fact_sales"
    assert r.source_column_ref is None
    assert r.warnings == []


def test_resolve_plain_aggregate_sets_source_column_and_accepts_dax() -> None:
    r = smi.resolve_kpi(_kpi("SUM(FACT_SALES[net_sales_value])"), SCHEMA)
    assert r.expression == "SUM(data_mart.fact_sales.net_sales_value)"
    assert r.source_column_ref == "data_mart.fact_sales.net_sales_value"


def test_resolve_uses_other_columns_to_disambiguate_table() -> None:
    # on_time_flag exists in two tables; dispatch_key pins fact_dispatch.
    r = smi.resolve_kpi(_kpi("SUM(on_time_flag) / NULLIF(COUNT(dispatch_key),0) * 100"), SCHEMA)
    assert r.error is None
    assert r.table == "data_mart.fact_dispatch"


def test_resolve_rewrites_where_shorthand() -> None:
    r = smi.resolve_kpi(_kpi("SUM(outstanding_value WHERE overdue_days > 0)"), SCHEMA)
    assert r.expression == (
        "SUM(CASE WHEN data_mart.fact_ar.overdue_days > 0 "
        "THEN data_mart.fact_ar.outstanding_value END)"
    )


@pytest.mark.parametrize(
    ("formula", "reason"),
    [
        ("LATEST(fact_sales.quantity_mt)", "point-in-time"),
        ("SUM(actual sales volume) / NULLIF(SUM(quantity_mt),0)", "plain text"),
        ("SUM(on_time_flag)", "several tables"),
        ("SUM(net_sales_value) / SUM(outstanding_value)", "cross-table"),
        ("SUM(fact_sales.no_such_column)", "not found"),
        ("SUM(ghost_column)", "not a column"),
        ("net_sales_value", "aggregate"),
        ("SUM(net_sales_value); DROP TABLE x", "unsupported character"),
        ("SUM(net_sales_value", "parentheses"),
        ("PG_SLEEP(10)", "not a supported function"),
    ],
)
def test_resolve_refuses_what_it_cannot_ground(formula: str, reason: str) -> None:
    r = smi.resolve_kpi(_kpi(formula), SCHEMA)
    assert r.error is not None and reason in r.error, r.error
    assert r.expression == ""


def test_resolve_warns_on_suspicious_aggregation() -> None:
    avg_pct = smi.resolve_kpi(_kpi("AVG(margin_percent)"), SCHEMA)
    sum_price = smi.resolve_kpi(_kpi("SUM(list_price)"), SCHEMA)
    unsafe_div = smi.resolve_kpi(_kpi("SUM(gross_profit) / SUM(net_sales_value)"), SCHEMA)
    assert any("weighted" in w for w in avg_pct.warnings)
    assert any("prices" in w for w in sum_price.warnings)
    assert any("NULLIF" in w for w in unsafe_div.warnings)


def test_validator_errors_become_kpi_errors() -> None:
    resolved = smi.resolve_kpi(_kpi("SUM(net_sales_value)"), SCHEMA)
    seen: list[str] = []

    def reject(sql: str) -> None:
        seen.append(sql)
        raise RuntimeError('column "net_sales_value" is of type text')

    smi.validate_kpi_sql(resolved, reject)
    assert seen == [
        "SELECT SUM(data_mart.fact_sales.net_sales_value) AS kpi_value "
        "FROM data_mart.fact_sales WHERE 1 = 0"
    ]
    assert resolved.error and "database rejected" in resolved.error


def test_unreachable_database_only_warns() -> None:
    from tests.test_helpers_discovery import make_test_db, seed_connection

    db = make_test_db()
    tenant, conn = seed_connection(db)

    def down(sql: str) -> None:
        raise ConnectionError("could not connect to server")

    previews = smi.preview_kpis(
        db,
        tenant_id=tenant.id,
        connection_id=conn.id,
        kpis=[_kpi("SUM(net_sales_value)", "Net Sales")],
        schema=SCHEMA,
        validator=down,
    )
    assert previews[0].action == "create"
    assert previews[0].resolved.warnings == ["could not reach the database to test this formula"]


# --- Relationships ---------------------------------------------------------------------


def test_relationship_resolution_direction_and_refusals() -> None:
    rel = smi.resolve_relationship(
        smi.RelationshipRow("dim_customer", "customer_key", "fact_sales", "customer_key", cardinality="1:N"),
        SCHEMA,
    )
    assert (rel.from_table, rel.to_table) == ("data_mart.fact_sales", "data_mart.dim_customer")

    m2m = smi.resolve_relationship(
        smi.RelationshipRow("fact_sales", "customer_key", "fact_dispatch", "customer_key", cardinality="many-to-many"),
        SCHEMA,
    )
    assert m2m.error and "many-to-many" in m2m.error

    missing = smi.resolve_relationship(
        smi.RelationshipRow("fact_sales", "ghost_key", "dim_customer", "customer_key"), SCHEMA
    )
    assert missing.error and "ghost_key" in missing.error

    mismatch = smi.resolve_relationship(
        smi.RelationshipRow("fact_sales", "customer_key", "dim_customer", "customer_name"), SCHEMA
    )
    assert mismatch.error is None and any("types differ" in w for w in mismatch.warnings)


def test_apply_relationships_adds_fk_once_and_join_planner_stays_unambiguous() -> None:
    schema = json.loads(json.dumps(SCHEMA))
    rel = SemanticRelationship(
        from_entity="x",
        to_entity="y",
        status="confirmed",
        source_fk_json=smi.resolve_relationship(
            smi.RelationshipRow("fact_sales", "customer_key", "dim_customer", "customer_key"), SCHEMA
        ).fk_json(),
    )
    smi.apply_relationships_to_schema(schema, [rel, rel])
    fact = next(t for t in schema["tables"] if t["table_name"] == "fact_sales")
    assert fact["foreign_keys"] == [
        {
            "column": "customer_key",
            "referred_schema": "data_mart",
            "referred_table": "dim_customer",
            "referred_column": "customer_key",
            "source": "semantic",
        }
    ]
    # The same join arriving as both a schema FK and a confirmed relationship
    # must not look like two competing paths.
    from_clause, joins = build_join_clauses(
        {"data_mart.fact_sales", "data_mart.dim_customer"},
        schema,
        [{"status": "confirmed", "source_fk_json": rel.source_fk_json}],
    )
    assert joins == ["data_mart.dim_customer.customer_key = data_mart.fact_sales.customer_key"]


# --- API -------------------------------------------------------------------------------------

WORKBOOK_ROWS = {
    "KPIs": [
        ["kpi_name", "domain", "formula", "business_rule", "synonyms"],
        ["Net Sales", "Sales", "SUM(fact_sales.net_sales_value)", "After discounts", "revenue; turnover"],
        ["Gross Margin %", "Sales", "SUM(gross_profit) / NULLIF(SUM(net_sales_value),0) * 100", "Weighted", ""],
        ["Closing Stock", "Inventory", "LATEST(fact_sales.quantity_mt)", "", ""],
    ],
    "Joins": [
        ["from_table", "from_column", "to_table", "to_column", "cardinality"],
        ["fact_sales", "customer_key", "dim_customer", "customer_key", "many-to-one"],
        ["fact_sales", "customer_key", "fact_dispatch", "customer_key", "many-to-many"],
    ],
}


def _workbook() -> bytes:
    import io

    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in WORKBOOK_ROWS.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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


def test_model_import_preview_save_and_schema_merge(
    client: tuple[TestClient, dict[str, str]], monkeypatch
) -> None:
    test_client, sent = client
    email = "model@acme.com"
    headers = _signup_and_headers(test_client, sent, email)
    conn_id = _create_connection(test_client, headers, monkeypatch)
    tenant_id = _seed_schema(email, conn_id)

    from app.routes import semantic as semantic_routes

    validated: list[str] = []
    monkeypatch.setattr(
        semantic_routes,
        "_readonly_validator",
        lambda _t, _c: lambda sql: validated.append(sql),
    )

    preview = test_client.post(
        f"/api/v1/semantic/model-import/preview?connection_id={conn_id}",
        headers=headers,
        files={"file": ("model.xlsx", _workbook(), "application/octet-stream")},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    kpis = {k["name"]: k for k in body["kpis"]}
    assert kpis["Net Sales"]["action"] == "create"
    assert kpis["Net Sales"]["synonyms"] == ["revenue", "turnover"]
    assert kpis["Gross Margin %"]["expression"].startswith("SUM(data_mart.fact_sales.gross_profit)")
    assert kpis["Closing Stock"]["action"] == "invalid"
    assert len(validated) == 3  # one reachability probe + two formulas
    rels = body["relationships"]
    assert [r["action"] for r in rels] == ["create", "invalid"]
    assert rels[0]["join"] == (
        "data_mart.fact_sales.customer_key = data_mart.dim_customer.customer_key"
    )

    # The client sends back everything, including the invalid rows; the
    # server re-checks and rejects them.
    saved = test_client.post(
        "/api/v1/semantic/model-import",
        headers=headers,
        json={
            "connection_id": str(conn_id),
            "kpis": [
                {k: row[k] for k in ("name", "formula", "description", "domain", "synonyms")}
                for row in body["kpis"]
            ],
            "relationships": [
                {k: row[k] for k in ("from_schema", "from_table", "from_column", "to_schema",
                                     "to_table", "to_column", "cardinality", "description")}
                for row in rels
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["kpis"]["created"] == 2
    assert saved.json()["kpis"]["rejected"] == 1
    assert saved.json()["relationships"] == {
        "created": 1, "confirmed": 0, "unchanged": 0, "rejected": 1,
    }

    db, gen = _db()
    try:
        metric = db.scalar(select(Metric).where(Metric.name == "Net Sales"))
        assert metric is not None
        assert metric.status == "suggested"
        assert metric.expression == "SUM(data_mart.fact_sales.net_sales_value)"
        assert metric.description == "After discounts"
        assert metric.domain == "Sales"
        assert json.loads(metric.synonyms_json or "[]") == ["revenue", "turnover"]
        ratio = db.scalar(select(Metric).where(Metric.name == "Gross Margin %"))
        assert ratio is not None and ratio.format == "percent"
        assert ratio.physical_table == "fact_sales" and ratio.source_column_ref is None

        from app.services.schema_discovery_service import get_stored_schema_for_tenant

        schema = get_stored_schema_for_tenant(db, conn_id, tenant_id)
    finally:
        next(gen, None)
    fact = next(t for t in schema["tables"] if t["table_name"] == "fact_sales")
    assert [fk["referred_table"] for fk in fact["foreign_keys"]] == ["dim_customer"]

    # Re-importing the same file changes nothing.
    again = test_client.post(
        f"/api/v1/semantic/model-import/preview?connection_id={conn_id}",
        headers=headers,
        files={"file": ("model.xlsx", _workbook(), "application/octet-stream")},
    ).json()
    assert {k["name"]: k["action"] for k in again["kpis"]} == {
        "Net Sales": "unchanged",
        "Gross Margin %": "unchanged",
        "Closing Stock": "invalid",
    }
    assert [r["action"] for r in again["relationships"]] == ["unchanged", "invalid"]


def test_model_import_requires_admin(
    client: tuple[TestClient, dict[str, str]], monkeypatch
) -> None:
    from tests.test_rbac import _set_role

    test_client, sent = client
    email = "model2@acme.com"
    headers = _signup_and_headers(test_client, sent, email)
    conn_id = _create_connection(test_client, headers, monkeypatch)
    _set_role(email, "analyst", test_client)
    forbidden = test_client.post(
        "/api/v1/semantic/model-import",
        headers=headers,
        json={"connection_id": str(conn_id), "kpis": [], "relationships": []},
    )
    assert forbidden.status_code == 403


# --- Rules, settings and vocabulary -------------------------------------------------------

from app.services import semantic_rules  # noqa: E402

RULE_SHEETS = [
    [
        ["rule_id", "category", "business_rule", "agent_guardrail"],
        ["BR002", "Sales", "Default sales revenue is net_sales_value.", "Use gross only when asked."],
        ["BR099", "Stock", "Use fact_stock.closing_qty for stock.", ""],
    ],
    [
        ["parameter", "value", "agent_rule"],
        ["default_volume_unit", "MT", "Quantities ending _mt are metric tonnes."],
    ],
    [
        ["concept", "synonyms"],
        ["supplier", "supplier, vendor"],
        ["customer", "account; buyer; client"],
        ["orphan", ""],
    ],
]


def test_rule_sheets_parse_into_rules_settings_and_vocabulary() -> None:
    rows, skipped, used = semantic_rules.parse_rule_sheets(RULE_SHEETS)
    assert used == {0, 1, 2}
    assert [(r.kind, r.name, r.category) for r in rows] == [
        ("rule", "BR002", "Sales"),
        ("rule", "BR099", "Stock"),
        ("setting", "default_volume_unit", ""),
        ("vocabulary", "supplier", ""),
        ("vocabulary", "customer", ""),
    ]
    assert rows[0].guidance == "Use gross only when asked."
    assert rows[2].rule == "default_volume_unit = MT"
    assert rows[3].terms == ["vendor"]  # the concept itself is not its own synonym
    assert skipped == [(4, "vocabulary row needs a term and its synonyms")]


def test_semantic_model_parse_reads_rules_from_leftover_sheets() -> None:
    parsed = smi.parse_semantic_model(SourceDocument(sheets=RULE_SHEETS))
    assert parsed.kpis == [] and parsed.relationships == []
    assert len(parsed.rules) == 5


def test_check_rule_flags_names_missing_from_schema() -> None:
    rows, _, _ = semantic_rules.parse_rule_sheets(RULE_SHEETS)
    by_name = {r.name: r for r in rows}
    assert semantic_rules.check_rule(by_name["BR002"], SCHEMA) == []
    assert semantic_rules.check_rule(by_name["BR099"], SCHEMA) == [
        "mentions fact_stock.closing_qty, which this connection doesn't have"
    ]


def test_rules_reach_the_sql_prompt_and_vocabulary_expands_ranking() -> None:
    from app.agent.workflow import _format_semantic_for_prompt, _ranking_question

    rows, _, _ = semantic_rules.parse_rule_sheets(RULE_SHEETS)
    catalog = {
        "metrics": [
            {
                "name": "Net Sales",
                "expression": "SUM(data_mart.fact_sales.net_sales_value)",
                "description": "Revenue after discounts.",
                "synonyms": ["revenue", "turnover"],
            }
        ],
        "dimensions": [],
        "rules": [
            {"kind": r.kind, "name": r.name, "category": r.category, "rule": r.rule,
             "guidance": r.guidance, "terms": r.terms}
            for r in rows
        ],
    }
    text = _format_semantic_for_prompt(catalog)
    assert (
        "  - Net Sales: SUM(data_mart.fact_sales.net_sales_value) — Revenue after discounts. "
        "(also: revenue, turnover)"
    ) in text
    assert "  - [Sales] Default sales revenue is net_sales_value. (Use gross only when asked.)" in text
    assert "  - default_volume_unit = MT — Quantities ending _mt are metric tonnes." in text
    assert "  - supplier: vendor" in text
    # Rules alone are enough to render a semantic section.
    assert _format_semantic_for_prompt({"rules": catalog["rules"]}) != "None configured."

    assert _ranking_question("Top 5 vendors by spend", catalog) == "Top 5 vendors by spend (supplier)"
    assert _ranking_question("Top suppliers", catalog) == "Top suppliers"


def test_rules_import_list_delete_and_catalog(
    client: tuple[TestClient, dict[str, str]], monkeypatch
) -> None:
    import io

    from openpyxl import Workbook

    test_client, sent = client
    email = "rules@acme.com"
    headers = _signup_and_headers(test_client, sent, email)
    conn_id = _create_connection(test_client, headers, monkeypatch)
    tenant_id = _seed_schema(email, conn_id)

    wb = Workbook()
    wb.remove(wb.active)
    for i, rows in enumerate(RULE_SHEETS):
        ws = wb.create_sheet(f"S{i}")
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)

    preview = test_client.post(
        f"/api/v1/semantic/model-import/preview?connection_id={conn_id}",
        headers=headers,
        files={"file": ("rules.xlsx", buf.getvalue(), "application/octet-stream")},
    )
    assert preview.status_code == 200, preview.text
    rules = preview.json()["rules"]
    assert [r["action"] for r in rules] == ["create"] * 5
    assert rules[1]["warnings"] == ["mentions fact_stock.closing_qty, which this connection doesn't have"]

    payload = [
        {k: r[k] for k in ("kind", "name", "category", "rule", "guidance", "terms")} for r in rules
    ]
    payload.append({"kind": "vocabulary", "rule": "empty", "terms": []})
    saved = test_client.post(
        "/api/v1/semantic/model-import",
        headers=headers,
        json={"connection_id": str(conn_id), "rules": payload},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["rules"] == {"created": 5, "updated": 0, "unchanged": 0, "rejected": 1}

    # Editing one rule in the file and re-importing updates just that row.
    payload[0]["guidance"] = "Gross sales only on request."
    again = test_client.post(
        "/api/v1/semantic/model-import",
        headers=headers,
        json={"connection_id": str(conn_id), "rules": payload[:5]},
    )
    assert again.json()["rules"] == {"created": 0, "updated": 1, "unchanged": 4, "rejected": 0}

    listed = test_client.get(
        f"/api/v1/semantic/rules?connection_id={conn_id}", headers=headers
    ).json()
    assert len(listed) == 5

    from app.redis_client import SemanticCache
    from app.services.semantic_service import load_connection_semantic_catalog

    class NoCache(SemanticCache):
        def get(self, *_a, **_k):
            return None

        def set(self, *_a, **_k):
            return True

    db, gen = _db()
    try:
        catalog = load_connection_semantic_catalog(db, tenant_id, conn_id, semantic_cache=NoCache())
    finally:
        next(gen, None)
    assert {r["kind"] for r in catalog["rules"]} == {"rule", "setting", "vocabulary"}

    deleted = test_client.delete(f"/api/v1/semantic/rules/{listed[0]['id']}", headers=headers)
    assert deleted.status_code == 204
    assert len(
        test_client.get(f"/api/v1/semantic/rules?connection_id={conn_id}", headers=headers).json()
    ) == 4
