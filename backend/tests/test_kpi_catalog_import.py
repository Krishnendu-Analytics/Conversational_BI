"""Tests for the KPI-catalog importer (app.services.kpi_catalog_import).

get_stored_schema / run_readonly_query are monkeypatched at the module
level (kpi_catalog_import imports them by name) so these tests exercise the
formula-resolution logic deterministically, without a real DB connection —
the safety property under test (never guess an ambiguous operand) has to
hold regardless of what a real connection returns.
"""

from __future__ import annotations

from uuid import uuid4

from app.models import Metric
from app.services import kpi_catalog_import as kci
from tests.test_helpers_discovery import make_test_db, seed_connection


def _col(name: str, dtype: str = "real") -> dict:
    return {"name": name, "data_type": dtype, "nullable": True, "is_pk": False}


def _table(name: str, columns: list[dict], *, schema_name: str = "data_mart") -> dict:
    return {"table_name": name, "schema_name": schema_name, "columns": columns, "foreign_keys": []}


def _cff_like_schema() -> dict:
    """A miniature version of the real schema this importer was built
    against — enough tables/columns to exercise same-table ratios,
    cross-table ratios, and genuine ambiguity."""
    return {
        "db_type": "postgres",
        "tables": [
            _table(
                "fact_sales",
                [
                    _col("sales_key", "integer"),
                    _col("product_key", "integer"),
                    _col("gross_sales_value"),
                    _col("net_sales_value"),
                    _col("cogs_value"),
                    _col("freight_value"),
                ],
            ),
            _table(
                "fact_production",
                [
                    _col("production_key", "integer"),
                    _col("planned_production_mt"),
                    _col("actual_production_mt"),
                    _col("rejected_mt"),
                ],
            ),
            _table(
                "fact_monthly_target",
                [
                    _col("target_key", "integer"),
                    _col("sales_target_value"),
                    _col("production_target_mt"),
                    _col("dispatch_target_mt"),
                ],
            ),
            _table(
                "fact_dispatch",
                [
                    _col("dispatch_key", "integer"),
                    _col("dispatch_volume_mt"),
                ],
            ),
        ],
    }


def _patch(monkeypatch, schema=None, rows=None, *, raise_on_query=False):
    monkeypatch.setattr(kci, "get_stored_schema", lambda *_a, **_k: schema)
    if raise_on_query:
        def _raise(*_a, **_k):
            raise Exception("relation \"kpi_meta_data\" does not exist")
        monkeypatch.setattr(kci, "run_readonly_query", _raise)
    else:
        monkeypatch.setattr(kci, "run_readonly_query", lambda *_a, **_k: rows or [])


def _setup_db_and_connection():
    db = make_test_db()
    tenant, conn = seed_connection(db)
    return db, tenant.id, conn.id


def test_missing_kpi_table_is_found_false(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(monkeypatch, schema=_cff_like_schema(), raise_on_query=True)

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.found is False
    assert result.imported == []
    assert result.skipped == []


def test_missing_discovered_schema_is_found_false(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(monkeypatch, schema=None, rows=[])

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.found is False


def test_empty_kpi_table_is_found_true_with_nothing_imported(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(monkeypatch, schema=_cff_like_schema(), rows=[])

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.found is True
    assert result.imported == []
    assert result.skipped == []


def test_direct_aggregate_formula_imports_a_metric(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {
                "kpi_name": "Net Sales Value",
                "formula": "SUM(FACT_SALES[net_sales_value])",
                "domain": "Commercial",
            }
        ],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.found is True
    assert result.imported == ["Net Sales Value"]
    assert result.skipped == []

    metric = db.query(Metric).filter_by(tenant_id=tenant_id, connection_id=connection_id).one()
    assert metric.expression == "SUM(data_mart.fact_sales.net_sales_value)"
    assert metric.domain == "Commercial"
    assert metric.status == "suggested"
    assert metric.physical_table == "fact_sales"
    assert metric.physical_column == "net_sales_value"


def test_direct_aggregate_unknown_table_is_skipped(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[{"kpi_name": "Ghost", "formula": "SUM(FACT_GHOST[value])", "domain": "Commercial"}],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.imported == []
    assert len(result.skipped) == 1
    assert "not found" in result.skipped[0].reason


def test_direct_aggregate_unknown_column_is_skipped(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[{"kpi_name": "Ghost Column", "formula": "SUM(FACT_SALES[nope])", "domain": "Commercial"}],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.imported == []
    assert "not found" in result.skipped[0].reason


def test_same_table_ratio_resolves_both_operands(monkeypatch) -> None:
    """"Actual Production / Planned Production" — both columns live in
    fact_production, an unambiguous same-table pair."""
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {
                "kpi_name": "Production Achievement %",
                "formula": "Actual Production / Planned Production",
                "domain": "Operations",
            }
        ],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert set(result.imported) == {"Actual Production", "Planned Production"}
    assert result.resolved_comparisons == [
        ("Production Achievement %", "Actual Production", "Planned Production", "/")
    ]
    metrics = {m.name: m for m in db.query(Metric).filter_by(tenant_id=tenant_id).all()}
    assert metrics["Actual Production"].physical_column == "actual_production_mt"
    assert metrics["Planned Production"].physical_column == "planned_production_mt"
    assert metrics["Actual Production"].domain == "Operations"
    assert metrics["Planned Production"].domain == "Operations"


def test_cross_table_ratio_fully_resolves_when_both_sides_are_unambiguous(monkeypatch) -> None:
    """"Dispatch Volume / Dispatch Target" — no single table has both, but
    each side is individually unambiguous, so the full pair resolves and a
    comparison is recorded."""
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {
                "kpi_name": "Dispatch Achievement %",
                "formula": "Dispatch Volume / Dispatch Target",
                "domain": "Logistics",
            }
        ],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.skipped == []
    assert len(result.resolved_comparisons) == 1
    kpi_name, left_name, right_name, op = result.resolved_comparisons[0]
    assert op == "/"
    metrics = {m.name: m for m in db.query(Metric).filter_by(tenant_id=tenant_id).all()}
    assert metrics[left_name].physical_table == "fact_dispatch"
    assert metrics[left_name].physical_column == "dispatch_volume_mt"
    assert metrics[right_name].physical_table == "fact_monthly_target"
    assert metrics[right_name].physical_column == "dispatch_target_mt"
    assert metrics[left_name].domain == "Logistics"
    assert metrics[right_name].domain == "Logistics"


def test_cross_table_ambiguous_operand_still_imports_the_unambiguous_side(monkeypatch) -> None:
    """"Actual Sales / Sales Target" — the real motivating case, and an
    honest result: "Actual Sales" is genuinely ambiguous even with a
    correct implementation (gross_sales_value, net_sales_value, and
    sales_target_value all equally match the bare word "sales" once
    "actual" is stripped as a filler qualifier) — there is no schema
    signal to prefer one over the others, so it must NOT resolve. "Sales
    Target" has no such ambiguity (only one column matches both "sales"
    AND "target") and is imported on its own, tagged with the KPI's
    domain — still useful: an investigation can surface it standing next
    to whatever the Sales domain's own actual-sales metrics report.
    """
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {
                "kpi_name": "Sales Target Achievement %",
                "formula": "Actual Sales / Sales Target",
                "domain": "Commercial",
            }
        ],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.resolved_comparisons == []  # the ratio itself isn't completed
    assert result.imported == ["Sales Target"]
    assert len(result.skipped) == 1
    assert "Actual Sales" in result.skipped[0].reason
    assert "Sales Target" not in result.skipped[0].reason

    metric = db.query(Metric).filter_by(tenant_id=tenant_id, name="Sales Target").one()
    assert metric.physical_table == "fact_monthly_target"
    assert metric.physical_column == "sales_target_value"
    assert metric.domain == "Commercial"


def test_ambiguous_operand_is_skipped_not_guessed(monkeypatch) -> None:
    """"Actual Sales" alone (no "Target" to disambiguate against) is
    ambiguous between gross_sales_value and net_sales_value — both share
    the "sales" token equally. Must refuse, not silently pick one. The
    other operand ("Freight") is unambiguous and still gets imported."""
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {
                "kpi_name": "Sales Efficiency",
                "formula": "Actual Sales / Freight",
                "domain": "Commercial",
            }
        ],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.imported == ["Freight"]
    assert result.resolved_comparisons == []
    assert len(result.skipped) == 1
    assert "Actual Sales" in result.skipped[0].reason
    assert "Actual Sales" in result.skipped[0].reason


def test_operand_with_no_match_is_skipped(monkeypatch) -> None:
    """"Wombat Count" matches nothing in the schema; "Freight" is
    unambiguous and still gets imported on its own."""
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {
                "kpi_name": "Nonsense Ratio",
                "formula": "Wombat Count / Freight",
                "domain": "Commercial",
            }
        ],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.imported == ["Freight"]
    assert result.resolved_comparisons == []
    assert "Wombat Count" in result.skipped[0].reason


def test_three_operand_formula_is_skipped_as_unsupported(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {
                "kpi_name": "Gross Profit",
                "formula": "Net Sales - COGS - Freight",
                "domain": "Commercial",
            }
        ],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.imported == []
    assert "not supported" in result.skipped[0].reason


def test_unparseable_formula_is_skipped(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[{"kpi_name": "Weird", "formula": "???totally not a formula???", "domain": None}],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.imported == []
    assert len(result.skipped) == 1


def test_row_missing_name_or_formula_is_silently_skipped_not_errored(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {"kpi_name": "", "formula": "SUM(FACT_SALES[net_sales_value])", "domain": "Commercial"},
            {"kpi_name": "No Formula", "formula": "", "domain": "Commercial"},
            {"kpi_name": None, "formula": None, "domain": None},
        ],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.found is True
    assert result.imported == []
    assert result.skipped == []


def test_reimport_is_idempotent(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    rows = [
        {"kpi_name": "Net Sales Value", "formula": "SUM(FACT_SALES[net_sales_value])", "domain": "Commercial"},
        {"kpi_name": "Production Achievement %", "formula": "Actual Production / Planned Production", "domain": "Operations"},
    ]
    _patch(monkeypatch, schema=_cff_like_schema(), rows=rows)

    first = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)
    second = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert set(first.imported) == set(second.imported)
    total_metrics = db.query(Metric).filter_by(tenant_id=tenant_id, connection_id=connection_id).count()
    assert total_metrics == 3  # Net Sales Value, Actual Production, Planned Production — no duplicates


def test_backfills_domain_on_a_metric_created_by_prior_heuristic_discovery(monkeypatch) -> None:
    """app.services.semantic_discovery's schema-only heuristic pass may
    already have created a Metric for this column with no domain — the
    KPI catalog's own domain is strictly better information and should be
    backfilled onto that existing row, not skipped just because a row
    already exists for the column."""
    db, tenant_id, connection_id = _setup_db_and_connection()
    pre_existing = Metric(
        tenant_id=tenant_id,
        connection_id=connection_id,
        name="Net Sales Value",
        expression="SUM(data_mart.fact_sales.net_sales_value)",
        status="suggested",
        source_column_ref="data_mart.fact_sales.net_sales_value",
        physical_schema="data_mart",
        physical_table="fact_sales",
        physical_column="net_sales_value",
    )
    db.add(pre_existing)
    db.commit()

    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {
                "kpi_name": "Net Sales Value",
                "formula": "SUM(FACT_SALES[net_sales_value])",
                "domain": "Commercial",
            }
        ],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.imported == ["Net Sales Value"]
    total = db.query(Metric).filter_by(tenant_id=tenant_id, connection_id=connection_id).count()
    assert total == 1  # reused the existing row, no duplicate
    db.refresh(pre_existing)
    assert pre_existing.domain == "Commercial"


def test_never_overwrites_an_already_set_domain(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    pre_existing = Metric(
        tenant_id=tenant_id,
        connection_id=connection_id,
        name="Net Sales Value",
        expression="SUM(data_mart.fact_sales.net_sales_value)",
        status="confirmed",
        domain="Custom Domain",
        source_column_ref="data_mart.fact_sales.net_sales_value",
        physical_schema="data_mart",
        physical_table="fact_sales",
        physical_column="net_sales_value",
    )
    db.add(pre_existing)
    db.commit()

    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {
                "kpi_name": "Net Sales Value",
                "formula": "SUM(FACT_SALES[net_sales_value])",
                "domain": "Commercial",
            }
        ],
    )

    kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    db.refresh(pre_existing)
    assert pre_existing.domain == "Custom Domain"


def test_import_is_scoped_to_its_own_tenant_and_connection(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    other_tenant, other_conn = seed_connection(db, tenant_name="Other", conn_name="Other DB")
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[{"kpi_name": "Net Sales Value", "formula": "SUM(FACT_SALES[net_sales_value])", "domain": "Commercial"}],
    )

    kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert db.query(Metric).filter_by(tenant_id=other_tenant.id).count() == 0
    metric = db.query(Metric).filter_by(tenant_id=tenant_id).one()
    assert metric.connection_id == connection_id


def test_key_columns_are_never_matched(monkeypatch) -> None:
    """A formula operand that happens to share a token with a *_key column
    must never bind to it — keys are identifiers, not measures."""
    db, tenant_id, connection_id = _setup_db_and_connection()
    schema = _cff_like_schema()
    # "Production" alone (no qualifier) could ambiguously match multiple
    # *_mt columns in fact_production; assert none of them is a key column.
    result_tables = schema["tables"]
    production_table = next(t for t in result_tables if t["table_name"] == "fact_production")
    assert all(
        not kci._is_key_column(c["name"]) for c in kci._numeric_columns(production_table)
    ), "numeric-column filter must exclude every *_key column"


def test_table_and_column_matching_is_case_insensitive(monkeypatch) -> None:
    db, tenant_id, connection_id = _setup_db_and_connection()
    _patch(
        monkeypatch,
        schema=_cff_like_schema(),
        rows=[
            {
                "kpi_name": "Net Sales Value",
                "formula": "sum(fact_sales[NET_SALES_VALUE])",
                "domain": "Commercial",
            }
        ],
    )

    result = kci.import_kpi_catalog(db, tenant_id=tenant_id, connection_id=connection_id)

    assert result.imported == ["Net Sales Value"]
