"""Dashboards built from attached documents, not the connected database."""

from __future__ import annotations

import json

from app.agent.analysis_quality import evaluate_executed_widgets
from app.agent.document_dashboard import (
    build_document_dashboard,
    build_widgets,
    grounded_summary,
    is_document_widget,
    wants_document_dashboard,
)

CHALLAN = """WARIS ALI FISH COMPANY
1, I.C. Bose Road, H.I.T Fish Market, Stall No. 33, Howrah-711101
Challan No. 51   B S SGRL 25-06-2026   FIVE TRAIN
Item            Qty    Rate    Amount
AAR             40     310     12,400
CHITAL THIN     25     420     10,500
KATLA EARCUT    60     280     16,800
SMALL DHAI      30     142      4,260
ROTTEN DHAI     12     167      2,002
Gross Wt 194.000   TOTAL 45,962
Total Expenditure 4,260   Balance 41,702
24-09-2026 SBI: PHONEPE 20,000   Balance 21,702
"""

PLAN = {
    "title": "Waris Ali Fish Co. - Challan 51",
    "summary": "Fish challan totalling 45,962 with 21,702 still due.",
    "kpis": [
        {"label": "Total Amount", "value": 45962, "unit": "INR"},
        {"label": "Balance Due", "value": "21,702", "unit": "INR"},
        {"label": "Profit", "value": 99999},  # not in the document
    ],
    "charts": [
        {
            "title": "Amount by Item",
            "chart": "pie",
            "label_name": "Item",
            "value_name": "Amount",
            "points": [
                {"label": "AAR", "value": 12400},
                {"label": "CHITAL THIN", "value": 10500},
                {"label": "KATLA EARCUT", "value": 16800},
            ],
        },
        {
            "title": "Padded",
            "chart": "bar",
            "points": [
                {"label": "AAR", "value": 12400},
                {"label": "MYSTERY FISH", "value": 7777},  # invented
            ],
        },
        {"title": "Lonely", "chart": "bar", "points": [{"label": "AAR", "value": 40}]},
    ],
    "tables": [
        {
            "title": "Line Items",
            "columns": ["Item", "Qty", "Rate", "Amount"],
            "rows": [["AAR", 40, 310, 12400], ["FAKE", 1, 2, 123456]],
        }
    ],
}


class _FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str, max_output_tokens: int | None = None) -> str:
        self.prompts.append(prompt)
        return self.reply


def test_document_dashboard_follows_the_files_not_the_wording():
    # Attached, @-picked or supervisor-chosen files all arrive as attached_ids.
    assert wants_document_dashboard(attached_ids=["d1"])
    # No files in play: the database, whatever words the request uses.
    assert not wants_document_dashboard(attached_ids=[])
    assert not wants_document_dashboard(attached_ids=None)


def test_only_figures_written_in_the_document_survive():
    widgets, dropped = build_widgets(PLAN, CHALLAN)
    kpis = {w["title"]: w["value"] for w in widgets if w["type"] == "kpi"}
    assert kpis == {"Total Amount": 45962, "Balance Due": 21702}

    charts = [w for w in widgets if w["type"] == "chart"]
    assert len(charts) == 1  # one-point chart dropped
    labels = [r["Item"] for r in charts[0]["rows"]]
    assert labels == ["KATLA EARCUT", "AAR", "CHITAL THIN"]  # largest first
    assert charts[0]["chart"] == "pie"

    table = next(w for w in widgets if w["type"] == "table")
    assert table["rows"] == [{"Item": "AAR", "Qty": 40, "Rate": 310, "Amount": 12400}]

    # Profit KPI, mystery fish point (its whole chart goes), fake table row.
    assert dropped == 3
    assert "Padded" not in {w["title"] for w in widgets}
    assert all(is_document_widget(w) and w["sql"] is None for w in widgets)


def test_build_document_dashboard_end_to_end():
    llm = _FakeLLM("```json\n" + json.dumps(PLAN) + "\n```")
    dash = build_document_dashboard(
        question="make dashboard of the attached pdf",
        document_text=CHALLAN,
        sources=[{"id": "d1", "filename": "challan51.pdf"}],
        llm=llm,
    )
    assert "45,962" in llm.prompts[0]  # the document text reached the LLM
    assert dash["title"] == "Waris Ali Fish Co. - Challan 51"
    assert dash["build_mode"] == "document"
    types = [w["type"] for w in dash["widgets"]]
    assert types[0] == "text" and "ai_insight" in types
    assert "kpi" in types and "chart" in types and "table" in types


def test_no_numbers_explains_instead_of_falling_back_to_database():
    llm = _FakeLLM(json.dumps({"title": "Letter", "summary": "A cover letter.", "kpis": []}))
    dash = build_document_dashboard(
        question="dashboard of it",
        document_text="Dear sir, please find attached.",
        sources=[{"id": "d1", "filename": "letter.pdf"}],
        llm=llm,
    )
    assert dash["widgets"] == []
    assert "letter.pdf" in dash["fallback_reason"]


def test_unreadable_llm_reply_is_reported():
    dash = build_document_dashboard(
        question="dashboard", document_text=CHALLAN,
        sources=[{"id": "d1", "filename": "c.pdf"}], llm=_FakeLLM("sorry, no"),
    )
    assert dash["widgets"] == [] and "try again" in dash["fallback_reason"]


def test_quality_filter_keeps_document_widgets_on_reopen():
    table_rows = [{"Item": f"Fish {i}", "Amount": 100 + i} for i in range(20)]
    dashboard = {
        "widgets": [
            {"id": "t", "type": "table", "title": "Line Items", "chart": "table",
             "rows": table_rows, "source": "document"},
            {"id": "c", "type": "chart", "title": "Flat", "chart": "bar",
             "rows": [{"Item": "A", "Amount": 5}, {"Item": "B", "Amount": 5}],
             "source": "document"},
        ]
    }
    kept = evaluate_executed_widgets(dashboard)
    assert [w["id"] for w in kept["widgets"]] == ["t", "c"]


LOTS = """No. FISH Kg RATE Amount
7* AAR 15 400 6000
15* AAR 14 300 4200
5* KATLA 41 200 8200
Gross 45962
"""


def test_totals_of_the_documents_line_items_count_as_verified():
    plan = {
        "kpis": [{"label": "Total Kg", "value": 70}],  # 15 + 14 + 41
        "charts": [{
            "title": "Amount by Fish", "label_name": "Fish", "value_name": "Amount",
            # AAR = 6000 + 4200, never written in the document.
            "points": [{"label": "AAR", "value": 10200}, {"label": "KATLA (5*)", "value": 8200}],
        }],
        "tables": [{
            "title": "Lots", "columns": ["No.", "Fish", "Kg", "Rate", "Amount"],
            "rows": [["7*", "AAR", 15, 400, 6000], ["15*", "AAR", 14, 300, 4200],
                     ["5*", "KATLA", 41, 200, 8200]],
        }],
    }
    widgets, dropped = build_widgets(plan, LOTS)
    assert dropped == 0
    assert next(w for w in widgets if w["type"] == "kpi")["value"] == 70
    chart = next(w for w in widgets if w["type"] == "chart")
    assert chart["rows"] == [{"Fish": "AAR", "Amount": 10200}, {"Fish": "KATLA (5*)", "Amount": 8200}]


def test_summary_sentences_with_unverified_numbers_are_removed():
    summary = "A fish challan totalling 45,962. Gross weight is 194000 kg. Balance due is 21,702."
    assert grounded_summary(summary, CHALLAN) == (
        "A fish challan totalling 45,962. Balance due is 21,702."
    )


def test_repeated_items_are_summed_into_one_bar():
    plan = {"charts": [{
        "title": "By Fish", "label_name": "Fish", "value_name": "Amount",
        "points": [{"label": "AAR", "value": 6000}, {"label": "KATLA", "value": 8200},
                   {"label": "AAR (15*)", "value": 4200}],
    }]}
    widgets, _ = build_widgets(plan, LOTS)
    assert widgets[0]["rows"] == [{"Fish": "AAR", "Amount": 10200}, {"Fish": "KATLA", "Amount": 8200}]


_LOTS_TABLE = {
    "title": "Lots", "columns": ["No.", "Fish", "Kg", "Rate", "Amount"],
    "rows": [["7*", "AAR", 15, 400, 6000], ["15*", "AAR", 14, 300, 4200],
             ["5*", "KATLA", 41, 200, 8200]],
}


def test_chart_over_a_table_is_computed_from_the_table():
    plan = {"tables": [_LOTS_TABLE], "charts": [{
        "title": "Amount by Fish", "table": "lots", "label_column": "fish", "value_column": "AMOUNT",
    }]}
    widgets, dropped = build_widgets(plan, LOTS)
    chart = next(w for w in widgets if w["type"] == "chart")
    assert chart["rows"] == [{"Fish": "AAR", "Amount": 10200}, {"Fish": "KATLA", "Amount": 8200}]
    assert dropped == 0


def test_rates_of_repeated_items_are_never_summed():
    plan = {"tables": [_LOTS_TABLE], "charts": [
        {"title": "Rate by Fish", "table": "Lots", "label_column": "Fish", "value_column": "Rate"},
        {"title": "Rate pts", "label_name": "Fish", "value_name": "Rate Rs.",
         "points": [{"label": "AAR", "value": 400}, {"label": "AAR", "value": 300},
                    {"label": "KATLA", "value": 200}]},
    ]}
    widgets, _ = build_widgets(plan, LOTS)
    assert not [w for w in widgets if w["type"] == "chart"]


def test_chart_columns_resolve_when_the_llm_misnames_them():
    table = {
        "title": "Line Items",
        "columns": ["No.", "Description", "Qty Kg", "Rate Rs.", "Expenditure Rs."],
        "rows": [["7*", "AAR", 15, 400, 6000], ["15*", "AAR", 14, 300, 4200],
                 ["5*", "KATLA", 41, 200, 8200]],
    }
    plan = {"tables": [table], "charts": [{
        "title": "Amount by Item", "table": "Line Items", "label_column": "Item", "value_column": "Amount",
    }]}
    widgets, _ = build_widgets(plan, LOTS)
    chart = next(w for w in widgets if w["type"] == "chart")
    assert chart["rows"] == [
        {"Description": "AAR", "Expenditure Rs.": 10200},
        {"Description": "KATLA", "Expenditure Rs.": 8200},
    ]


def test_line_repeated_by_pdf_extraction_is_removed_when_totals_reconcile():
    text = "AAR 6000\nKATLA 8200\nKATLA 8200\nTOTAL 14200"
    plan = {"tables": [{"title": "Items", "columns": ["Fish", "Amount"],
                        "rows": [["AAR", 6000], ["KATLA", 8200], ["KATLA", 8200]]}]}
    widgets, _ = build_widgets(plan, text)
    assert len(widgets[0]["rows"]) == 2


def test_genuinely_repeated_lines_are_kept():
    # Two identical lots and the document's total counts both.
    text = "AAR 6000\nKATLA 8200\nKATLA 8200\nTOTAL 22400"
    plan = {"tables": [{"title": "Items", "columns": ["Fish", "Amount"],
                        "rows": [["AAR", 6000], ["KATLA", 8200], ["KATLA", 8200]]}]}
    widgets, _ = build_widgets(plan, text)
    assert len(widgets[0]["rows"]) == 3


def test_blank_expense_lines_are_not_charted():
    plan = {"charts": [{"title": "Expenses", "label_name": "Expense", "value_name": "Amount",
                        "points": [{"label": "Commission", "value": 2760}, {"label": "Samity", "value": 0},
                                   {"label": "Ice", "value": 200}]}]}
    widgets, _ = build_widgets(plan, "Commission 2760 Ice 200 Samity 0")
    assert [r["Expense"] for r in widgets[0]["rows"]] == ["Commission", "Ice"]
