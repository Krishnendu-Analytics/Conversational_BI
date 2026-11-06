"""Generic dataset profiler — no industry-specific column hard-coding."""

from app.agent.dataset_profiler import profile_dataset


CALL_CENTER_SCHEMA = {
    "db_type": "excel",
    "tables": [
        {
            "table_name": "campaign_performance",
            "schema_name": "main",
            "columns": [
                {"name": "campaign_name", "data_type": "VARCHAR"},
                {"name": "total_calls", "data_type": "BIGINT"},
                {"name": "talk_time", "data_type": "VARCHAR"},
                {"name": "idle_time", "data_type": "TIME"},
                {"name": "talk_pct", "data_type": "DOUBLE"},
            ],
        }
    ],
}

SALES_SCHEMA = {
    "db_type": "postgres",
    "tables": [
        {
            "table_name": "orders",
            "schema_name": "public",
            "columns": [
                {"name": "id", "data_type": "integer", "is_pk": True},
                {"name": "customer_name", "data_type": "varchar"},
                {"name": "product_name", "data_type": "varchar"},
                {"name": "region", "data_type": "varchar"},
                {"name": "revenue", "data_type": "numeric"},
                {"name": "quantity", "data_type": "integer"},
                {"name": "order_date", "data_type": "date"},
            ],
        }
    ],
}

MARKETING_SCHEMA = {
    "db_type": "postgres",
    "tables": [
        {
            "table_name": "ad_stats",
            "schema_name": "public",
            "columns": [
                {"name": "campaign", "data_type": "varchar"},
                {"name": "impressions", "data_type": "integer"},
                {"name": "clicks", "data_type": "integer"},
                {"name": "conversions", "data_type": "integer"},
                {"name": "spend", "data_type": "numeric"},
                {"name": "date", "data_type": "date"},
            ],
        }
    ],
}


def test_call_center_profile_classifies_rates_durations_and_calls() -> None:
    profile = profile_dataset(CALL_CENTER_SCHEMA, db_type="excel")

    dim_cols = {d.column.lower() for d in profile.dimensions}
    assert "campaign_name" in dim_cols

    measure_cols = {m.column.lower() for m in [*profile.counts, *profile.measures] if m.column}
    assert "total_calls" in measure_cols
    assert all(m.aggregation == "sum" for m in profile.counts if m.column == "total_calls")

    rate_cols = {m.column.lower() for m in profile.rates if m.column}
    assert "talk_pct" in rate_cols
    assert all(m.aggregation == "avg" for m in profile.rates)

    duration_cols = {d.column.lower() for d in profile.durations}
    assert "talk_time" in duration_cols
    assert "idle_time" in duration_cols
    assert "talk_time" not in measure_cols
    assert "idle_time" not in measure_cols

    distinct_names = [m.name.lower() for m in profile.derived_metrics if m.kind == "distinct"]
    assert any("campaign" in n for n in distinct_names)


def test_sales_profile_finds_dimensions_measures_dates_and_ratio() -> None:
    profile = profile_dataset(SALES_SCHEMA, db_type="postgres")

    dim_cols = {d.column.lower() for d in profile.dimensions}
    assert {"customer_name", "product_name", "region"} <= dim_cols

    measure_cols = {m.column.lower() for m in [*profile.counts, *profile.measures] if m.column}
    assert "revenue" in measure_cols
    assert "quantity" in measure_cols
    assert "id" not in measure_cols

    date_cols = {d.column.lower() for d in profile.dates}
    assert "order_date" in date_cols

    derived = [m.name.lower() for m in profile.derived_metrics]
    assert any("revenue per unit" in n or "per unit" in n for n in derived)
    assert any("customer" in n for n in derived if "active" in n)


def test_marketing_profile_derives_click_through_rate() -> None:
    profile = profile_dataset(MARKETING_SCHEMA, db_type="postgres")

    measure_cols = {m.column.lower() for m in [*profile.counts, *profile.measures] if m.column}
    assert {"impressions", "clicks", "conversions", "spend"} <= measure_cols

    dim_cols = {d.column.lower() for d in profile.dimensions}
    assert "campaign" in dim_cols

    date_cols = {d.column.lower() for d in profile.dates}
    assert "date" in date_cols

    derived_names = " ".join(m.name.lower() for m in profile.derived_metrics)
    assert "click-through" in derived_names or "click through" in derived_names

    for metric in profile.all_metrics():
        if metric.kind == "rate":
            assert metric.aggregation == "avg"
            assert metric.expression.upper().startswith("AVG(")


def test_profiler_does_not_treat_percent_as_additive() -> None:
    schema = {
        "tables": [
            {
                "table_name": "ops",
                "columns": [
                    {"name": "idle_pct", "data_type": "numeric"},
                    {"name": "wrap_percent", "data_type": "float"},
                    {"name": "utilization_rate", "data_type": "numeric"},
                ],
            }
        ]
    }
    profile = profile_dataset(schema, db_type="postgres")
    additive_cols = {m.column.lower() for m in [*profile.counts, *profile.measures] if m.column}
    assert "idle_pct" not in additive_cols
    assert {m.column.lower() for m in profile.rates if m.column} >= {
        "idle_pct",
        "wrap_percent",
        "utilization_rate",
    }


PRODUCTION_SCHEMA = {
    "db_type": "postgres",
    "tables": [
        {
            "schema_name": "public",
            "table_name": "fact_production",
            "columns": [
                {"name": "production_id", "data_type": "integer", "primary_key": True},
                {"name": "product_name", "data_type": "varchar"},
                {"name": "production_date", "data_type": "date"},
                {"name": "quantity_mt", "data_type": "numeric"},
                {"name": "standard_cost_per_mt", "data_type": "numeric"},
                {"name": "unit_price", "data_type": "numeric"},
            ],
        }
    ],
}


def test_per_unit_values_are_weighted_rates_not_sums():
    profile = profile_dataset(PRODUCTION_SCHEMA, db_type="postgres")
    rates = {m.column: m for m in profile.rates}
    measures = {m.column for m in [*profile.measures, *profile.counts]}

    assert "standard_cost_per_mt" in rates and "standard_cost_per_mt" not in measures
    assert "unit_price" in rates and "unit_price" not in measures
    # Weighted by the table's quantity, not a plain AVG over rows.
    expr = rates["standard_cost_per_mt"].expression.lower()
    assert "sum(" in expr and "quantity_mt" in expr and "nullif" in expr
    assert "quantity_mt" in measures


AR_SCHEMA = {
    "db_type": "postgres",
    "tables": [
        {
            "table_name": "fact_ar",
            "schema_name": "data_mart",
            "columns": [
                {"name": "ar_transaction_key", "data_type": "integer", "is_pk": True},
                {"name": "customer_key", "data_type": "integer"},
                {"name": "invoice_date", "data_type": "character varying"},
                {"name": "ageing_bucket", "data_type": "character varying"},
                {"name": "invoice_value", "data_type": "real"},
                {"name": "payment_value", "data_type": "real"},
            ],
            "foreign_keys": [
                {
                    "constrained_columns": ["customer_key"],
                    "referred_schema": "data_mart",
                    "referred_table": "dim_customer",
                    "referred_columns": ["customer_key"],
                }
            ],
        },
        {
            "table_name": "dim_customer",
            "schema_name": "data_mart",
            "columns": [
                {"name": "customer_key", "data_type": "integer", "is_pk": True},
                {"name": "customer_name", "data_type": "character varying"},
                {"name": "customer_segment", "data_type": "character varying"},
            ],
        },
    ],
}


def test_text_typed_date_is_not_a_category_breakdown():
    profile = profile_dataset(AR_SCHEMA, db_type="postgres", primary_table=AR_SCHEMA["tables"][0])
    dim_cols = {d.column.lower() for d in profile.dimensions}
    assert "invoice_date" not in dim_cols
    assert "ageing_bucket" in dim_cols


def test_entity_count_from_dimension_table_is_not_called_active():
    profile = profile_dataset(AR_SCHEMA, db_type="postgres", primary_table=AR_SCHEMA["tables"][0])
    names = [m.name for m in profile.derived_metrics if m.kind == "distinct"]
    assert "Total Customer Names" in names
    assert not any(n.startswith("Active Customer") for n in names)


def test_phone_stored_as_bigint_is_an_identifier_not_a_measure() -> None:
    schema = {
        "db_type": "csv",
        "tables": [
            {
                "table_name": "patients",
                "schema_name": "main",
                "columns": [
                    {"name": "patient_id", "data_type": "BIGINT"},
                    {"name": "patient_name", "data_type": "VARCHAR"},
                    {"name": "blood_group", "data_type": "VARCHAR"},
                    {"name": "phone", "data_type": "BIGINT"},
                    {"name": "registration_date", "data_type": "DATE"},
                ],
            }
        ],
    }
    profile = profile_dataset(schema)
    measure_cols = {m.column.lower() for m in profile.measures if m.column}
    assert "phone" not in measure_cols
    assert "phone" in {f.column.lower() for f in profile.identifiers if f.column}
