"""The platform must not assume one industry: suggestions, labels and
parsing work the same for a hospital, a bank or a SaaS database."""

from app.services.semantic_discovery import (
    _aggregation_for,
    _dimension_generic_label,
    _metric_name_for_column,
)


def test_metrics_keep_their_own_names_in_any_industry() -> None:
    # Hospital, bank, SaaS columns: named after themselves, never "Revenue".
    assert _metric_name_for_column("total_beds", "integer") == ("Total Beds", "SUM({expr})", "number")
    assert _metric_name_for_column("account_balance", "numeric") == ("Account Balance", "SUM({expr})", "number")
    assert _metric_name_for_column("length_of_stay_days", "integer")[0] == "Length Of Stay Days"
    assert _metric_name_for_column("mrr", "numeric")[0] == "Mrr"
    # Money only from words that mean money everywhere (or a money type).
    assert _metric_name_for_column("claim_amount", "numeric")[2] == "currency"
    assert _metric_name_for_column("consultation_fee", "numeric")[2] == "currency"
    assert _metric_name_for_column("principal", "money")[2] == "currency"
    assert _metric_name_for_column("total_value", "numeric")[2] == "number"


def test_calendar_parts_are_not_metrics_but_durations_are() -> None:
    assert _metric_name_for_column("fiscal_year", "integer") is None
    assert _metric_name_for_column("month_number", "integer") is None
    assert _metric_name_for_column("day_of_week", "integer") is None
    assert _metric_name_for_column("stay_days", "integer") is not None


def test_averaging_needs_a_rate_or_a_real_lookup_table() -> None:
    assert _aggregation_for("charge_amount", "d_charges", "", "SUM({expr})") == "SUM({expr})"
    assert _aggregation_for("standard_cost", "dim_product", "", "SUM({expr})") == "AVG({expr})"
    assert _aggregation_for("bed_capacity", "wards", "", "SUM({expr})", lookup_table=True) == "AVG({expr})"
    assert _aggregation_for("cost_per_bed_day", "fact_x", "", "SUM({expr})") == "AVG({expr})"
    assert _aggregation_for("x", "t", "average wait per patient", "SUM({expr})") == "AVG({expr})"


def test_dimensions_are_labelled_by_their_own_names() -> None:
    assert _dimension_generic_label("ward_name", "varchar(100)") == "Ward Name"
    assert _dimension_generic_label("branch_code", "character varying") == "Branch Code"
    assert _dimension_generic_label("admitted_at", "timestamp") == "Date"
    assert _dimension_generic_label("discharge_notes", "text") is None  # free text
    assert _dimension_generic_label("patient_email", "varchar(200)") is None
    assert _dimension_generic_label("bed_count", "integer") is None


def test_ambiguous_abbreviations_are_left_alone() -> None:
    from app.agent.domain_taxonomy import expand_domain_label

    # Hospital heart rate, bank investments, energy watt-hours.
    assert expand_domain_label("Hr Vitals") == "Hr Vitals"
    assert expand_domain_label("Inv Positions") == "Inv Positions"
    assert expand_domain_label("Wh Readings") == "Wh Readings"
    assert expand_domain_label("Ar Txn") == "Accounts Receivable Txn"


def test_hints_match_whole_words_only() -> None:
    from app.agent.query_plan import _guess_intent
    from app.agent.time_resolution import infer_comparison_type

    # Bank, oil & gas, and plain English words that contain a hint.
    assert _guess_intent("average exchange rate by currency") == "aggregation"
    assert _guess_intent("total drilling cost by rig") == "aggregation"
    assert _guess_intent("why did revenue drop") == "investigation"
    assert _guess_intent("compare sales vs target") == "comparison"
    assert _guess_intent("how did the rate change") == "comparison"
    assert infer_comparison_type("momentum of sign-ups this month", None) is None
    assert infer_comparison_type("sales mom", None) == "previous_month"
    assert infer_comparison_type("revenue yoy", None) == "previous_year"


def test_dataset_switch_needs_an_explicit_instruction() -> None:
    from app.agent.dataset_context import detect_dataset_switch as switch

    assert switch("what is the energy use by plant?") is None
    assert switch("how do customers use the app") is None
    assert switch("use sales_2024") == "sales_2024"
    assert switch("now use 'Q3 orders'.") == "Q3 orders"
    assert switch("Switch to inventory") == "inventory"
    assert switch("can you use dataset marketing leads?") == "marketing leads"
