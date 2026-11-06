"""Whole-database dashboards: one story per fact table, not a template grid."""

from app.agent.dashboard import build_full_database_dashboard_spec


def _fact(name: str, extra_dim: str) -> dict:
    return {
        "schema_name": "public",
        "table_name": name,
        "columns": [
            {"name": f"{name}_id", "data_type": "integer", "primary_key": True},
            {"name": "product_name", "data_type": "varchar"},
            {"name": extra_dim, "data_type": "varchar"},
            {"name": "txn_date", "data_type": "date"},
            {"name": "quantity_mt", "data_type": "numeric"},
            {"name": "cost_per_mt", "data_type": "numeric"},
        ],
    }


SCHEMA = {
    "db_type": "postgres",
    "tables": [_fact("fact_sales", "customer_name"), _fact("fact_production", "plant_name")],
}


def test_sections_break_down_by_different_dimensions():
    spec = build_full_database_dashboard_spec(SCHEMA)
    dims = {
        w["section"]: (w.get("quality") or {}).get("dimension_column")
        for w in spec["widgets"]
        if w.get("purpose") == "breakdown" and (w.get("quality") or {}).get("dimension_column")
    }
    assert dims == {"Sales": "product_name", "Production": "plant_name"}


def test_same_named_measures_in_different_tables_are_both_kept():
    spec = build_full_database_dashboard_spec(SCHEMA)
    kpis = [(w["section"], w["title"]) for w in spec["widgets"] if w["type"] == "kpi"]
    # Each area's headline leads the Overview, named with its area so two
    # same-named measures from different tables stay distinguishable.
    assert ("Overview", "Sales: Total Quantity Mt") in kpis
    assert ("Overview", "Production: Total Quantity Mt") in kpis
    assert spec["sections"][0] == "Overview"
    assert spec["build_mode"] == "full_database_heuristic"  # no semantic model here


def test_per_unit_cost_is_a_weighted_rate_not_a_sum():
    spec = build_full_database_dashboard_spec(SCHEMA)
    cost = next(w for w in spec["widgets"] if w["title"] == "Cost Per Mt")
    sql = cost["sql"].lower()
    assert "nullif(sum(" in sql
    assert "sum(cost_per_mt)" not in sql.replace(" ", "")


def _metric(name, expr, table, domain, status="suggested", fmt=None):
    return {"name": name, "expression": expr, "physical_table": table, "domain": domain,
            "status": status, "format": fmt}


# A hospital: no fact_/dim_ names, no industry words the code knows.
HOSPITAL = {
    "db_type": "postgres",
    "tables": [
        {"schema_name": "public", "table_name": "admissions", "columns": [
            {"name": "admission_id", "data_type": "integer", "primary_key": True},
            {"name": "ward_name", "data_type": "varchar"},
            {"name": "admitted_on", "data_type": "date"},
            {"name": "length_of_stay_days", "data_type": "integer"},
            {"name": "readmitted", "data_type": "integer"},
        ]},
        {"schema_name": "public", "table_name": "invoices", "columns": [
            {"name": "invoice_id", "data_type": "integer", "primary_key": True},
            {"name": "payer_type", "data_type": "varchar"},
            {"name": "billed_on", "data_type": "date"},
            {"name": "billed_amount", "data_type": "numeric"},
            {"name": "paid_amount", "data_type": "numeric"},
        ]},
    ],
}
HOSPITAL_MODEL = {"metrics": [
    _metric("Average Length of Stay", "AVG(public.admissions.length_of_stay_days)", "admissions", "Patient Flow"),
    _metric("Admissions", "COUNT(public.admissions.admission_id)", "admissions", "Patient Flow"),
    _metric("Readmission Rate", "100.0 * SUM(public.admissions.readmitted) / NULLIF(COUNT(*), 0)",
            "admissions", "Patient Flow", fmt="percent"),
    _metric("Billed Amount", "SUM(public.invoices.billed_amount)", "invoices", "billing", status="confirmed"),
    _metric("Collection Rate", "SUM(public.invoices.paid_amount) / NULLIF(SUM(public.invoices.billed_amount), 0)",
            "invoices", "billing"),
    _metric("Old Metric", "SUM(public.invoices.billed_amount)", "invoices", "billing", status="deprecated"),
]}


def test_areas_and_kpis_come_from_the_semantic_model():
    spec = build_full_database_dashboard_spec(HOSPITAL, semantic=HOSPITAL_MODEL)
    assert spec["build_mode"] == "full_database_semantic"
    # Areas are the model's domains; the one with confirmed metrics leads.
    assert spec["sections"][:3] == ["Overview", "Billing", "Patient Flow"]
    titles = {w["title"] for w in spec["widgets"]}
    assert "Billing: Billed Amount" in titles  # confirmed metric is the headline, own name
    assert "Old Metric" not in " ".join(titles)  # deprecated metrics never show
    kpis = {w["title"]: w for w in spec["widgets"] if w["type"] == "kpi"}
    assert any("Readmission Rate" in t or "Collection Rate" in t for t in kpis)
    # Formulas are used as written: a ratio stays a ratio of sums.
    for w in spec["widgets"]:
        if w["title"].endswith("Collection Rate"):
            assert "nullif(sum(" in w["sql"].lower().replace(" ", "")


def test_unqualified_formulas_belong_to_the_table_with_those_columns():
    from app.agent.dataset_profiler import _metric_belongs_to

    item = {"name": "Stay", "expression": "AVG(length_of_stay_days)"}
    assert _metric_belongs_to(item, "admissions", {"length_of_stay_days", "ward_name"})
    assert not _metric_belongs_to(item, "invoices", {"billed_amount", "paid_amount"})
    other = {"name": "X", "expression": "SUM(public.invoices.billed_amount)"}
    assert not _metric_belongs_to(other, "admissions", {"billed_amount"})


def test_semantic_ratios_divide_as_decimals_and_keep_the_models_order():
    from app.agent.dataset_profiler import DatasetProfile, merge_semantic_into_profile

    model = {"metrics": [
        _metric("Dispatch Value", "SUM(t.dispatch.value)", "dispatch", "Logistics"),
        _metric("On-Time Rate", "SUM(CASE WHEN t.dispatch.on_time = 1 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)",
                "dispatch", "Logistics"),
        _metric("Freight Cost", "SUM(t.dispatch.freight)", "dispatch", "Logistics"),
        _metric("Dispatch Value", "SUM(t.dispatch.other)", "dispatch", "Logistics"),  # same name: first wins
    ]}
    profile = merge_semantic_into_profile(DatasetProfile(source_table="t.dispatch"), model)
    assert [m.name for m in profile.measures] == ["Dispatch Value", "Freight Cost"]
    assert profile.measures[0].expression == "SUM(t.dispatch.value)"
    rate = profile.derived_metrics[0]
    assert rate.name == "On-Time Rate"
    # Integer SUM / COUNT would truncate to 0 on Postgres; decimal division instead.
    assert rate.expression.startswith("1.0 * SUM(")


def test_the_llm_picks_each_areas_headline_from_the_models_names():
    class LLM:
        client = object()

        def complete(self, prompt: str) -> str:
            assert "Area: Billing" in prompt and "Collection Rate" in prompt
            return '{"Billing": "collection rate", "Patient Flow": "Invented Metric"}'

    spec = build_full_database_dashboard_spec(HOSPITAL, semantic=HOSPITAL_MODEL, llm=LLM())
    overview = [w["title"] for w in spec["widgets"] if w.get("section") == "Overview"]
    assert "Billing: Collection Rate" in overview  # chosen by meaning, not alphabet
    # A name the model does not have is ignored: that area keeps its default.
    assert not any("Invented" in t for t in overview)
