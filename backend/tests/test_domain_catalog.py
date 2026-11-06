"""Tests for the domain registry (app.agent.domain_catalog)."""

from app.agent.domain_catalog import Domain, resolve_domains


def _petroche_like_schema() -> dict:
    return {
        "connection_id": "connection-123",
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "fact_sales",
                "columns": [
                    {"name": "sales_key", "data_type": "integer", "is_pk": True},
                    {"name": "customer_key", "data_type": "integer"},
                    {"name": "date_key", "data_type": "integer"},
                    {"name": "invoice_number", "data_type": "varchar"},
                    {"name": "invoice_value", "data_type": "numeric"},
                    {"name": "gross_profit", "data_type": "numeric"},
                ],
                "foreign_keys": [],
            },
            {
                "schema_name": "public",
                "table_name": "fact_production",
                "columns": [
                    {"name": "production_key", "data_type": "integer", "is_pk": True},
                    {"name": "plant_key", "data_type": "integer"},
                    {"name": "date_key", "data_type": "integer"},
                    {"name": "total_planned_production_mt", "data_type": "numeric"},
                    {"name": "actual_production_mt", "data_type": "numeric"},
                ],
                "foreign_keys": [],
            },
            {
                "schema_name": "public",
                "table_name": "dim_customer",
                "columns": [
                    {"name": "customer_key", "data_type": "integer", "is_pk": True},
                    {"name": "customer_name", "data_type": "varchar"},
                ],
                "foreign_keys": [],
            },
        ],
    }


def test_resolve_domains_auto_derives_names_from_fact_tables() -> None:
    domains = resolve_domains(_petroche_like_schema())

    names = {d.name for d in domains}
    assert "Sales" in names
    assert "Production" in names
    # dim_customer isn't a fact table and shouldn't become its own domain.
    assert "Customer" not in names


def test_resolve_domains_prefers_curated_domain_name() -> None:
    catalog = {
        "metrics": [
            {
                "name": "Total Invoice Value",
                "physical_table": "fact_sales",
                "domain": "Revenue",
            }
        ],
        "dimensions": [],
    }

    domains = resolve_domains(_petroche_like_schema(), catalog)
    sales_domain = next(d for d in domains if d.table.endswith("fact_sales"))

    assert sales_domain.name == "Revenue"


def test_resolve_domains_attaches_confirmed_fields_per_table() -> None:
    catalog = {
        "metrics": [
            {"name": "Total Invoice Value", "physical_table": "fact_sales"},
            {"name": "Total Planned Production", "physical_table": "fact_production"},
        ],
        "dimensions": [
            {"name": "Invoice Number", "physical_table": "fact_sales"},
        ],
    }

    domains = resolve_domains(_petroche_like_schema(), catalog)
    sales_domain = next(d for d in domains if d.table.endswith("fact_sales"))
    production_domain = next(d for d in domains if d.table.endswith("fact_production"))

    assert sales_domain.metrics == ["Total Invoice Value"]
    assert sales_domain.dimensions == ["Invoice Number"]
    assert production_domain.metrics == ["Total Planned Production"]
    assert production_domain.dimensions == []


def test_resolve_domains_respects_max_domains() -> None:
    schema = _petroche_like_schema()
    domains = resolve_domains(schema, max_domains=1)
    assert len(domains) == 1


def test_resolve_domains_empty_schema_returns_empty_list() -> None:
    assert resolve_domains({"tables": []}) == []


def test_resolve_domains_expands_abbreviated_fallback_name() -> None:
    schema = {
        "tables": [
            {
                "schema_name": "public",
                "table_name": "fact_ar_txn",
                "columns": [
                    {"name": "ar_txn_key", "data_type": "integer", "is_pk": True},
                    {"name": "customer_key", "data_type": "integer"},
                    {"name": "date_key", "data_type": "integer"},
                    {"name": "outstanding_value", "data_type": "numeric"},
                    {"name": "overdue_days", "data_type": "numeric"},
                ],
                "foreign_keys": [],
            }
        ]
    }

    domains = resolve_domains(schema)

    assert domains[0].name == "Accounts Receivable Txn"


def test_resolve_domains_curated_name_beats_abbreviation_expansion() -> None:
    schema = {
        "tables": [
            {
                "schema_name": "public",
                "table_name": "fact_ar_txn",
                "columns": [
                    {"name": "ar_txn_key", "data_type": "integer", "is_pk": True},
                    {"name": "customer_key", "data_type": "integer"},
                    {"name": "date_key", "data_type": "integer"},
                    {"name": "outstanding_value", "data_type": "numeric"},
                    {"name": "overdue_days", "data_type": "numeric"},
                ],
                "foreign_keys": [],
            }
        ]
    }
    catalog = {
        "metrics": [
            {
                "name": "Outstanding Value",
                "physical_table": "fact_ar_txn",
                "domain": "Collections",
            }
        ],
        "dimensions": [],
    }

    domains = resolve_domains(schema, catalog)

    assert domains[0].name == "Collections"


def test_domain_model_defaults() -> None:
    domain = Domain(name="Sales", table="public.fact_sales")
    assert domain.metrics == []
    assert domain.dimensions == []
