"""Curated vocabulary for turning a fact table's auto-derived label into a
business-recognizable domain name.

``domain_catalog.resolve_domains`` already prefers a human-confirmed
``Metric``/``Dimension.domain`` value when one exists, and otherwise falls
back to title-casing the bare table name (``table_domain_label``) — which
reads fine for a table like ``fact_sales`` ("Sales") but produces a
confusing label for anything built on a common business abbreviation, e.g.
``fact_ar_txn`` -> "Ar Txn" instead of "Accounts Receivable Txn".

This module only replaces known abbreviation tokens inside that fallback
label; it never overrides an already-curated domain name, and it leaves
any label with no recognized abbreviation untouched (so "Sales",
"Production", "Inventory" etc. are unaffected).
"""

from __future__ import annotations

# Word (case-insensitive) -> expansion. Deliberately conservative: only
# business abbreviations that are unambiguous out of context, so this never
# "corrects" a token that was actually a real word (e.g. no "hr" -> ... if
# "hr" could plausibly be someone's real table-name word choice elsewhere;
# in practice these are standard ERP/data-warehouse abbreviations).
#
# Only abbreviations with one meaning in practice. Dropped because they mean
# different things in different industries: hr (human resources / heart
# rate), inv (inventory / investments / invoices), wh (warehouse /
# watt-hours), po (purchase order / post office / per os), proc (procurement
# / procedure), crm (a system, not a domain). Tenants name their own domains
# in the semantic layer, which always wins over this fallback.
_ABBREVIATION_EXPANSIONS: dict[str, str] = {
    "ar": "Accounts Receivable",
    "ap": "Accounts Payable",
    "gl": "General Ledger",
    "mfg": "Manufacturing",
    "whse": "Warehouse",
    "qc": "Quality Control",
    "mro": "Maintenance",
    "cogs": "Cost Of Goods Sold",
}


def expand_domain_label(label: str) -> str:
    """Replace any whole word in ``label`` that is a known business
    abbreviation with its expansion, preserving word order and leaving
    unrecognized words untouched. A no-op when nothing matches."""
    words = label.split()
    if not words:
        return label
    return " ".join(_ABBREVIATION_EXPANSIONS.get(w.lower(), w) for w in words)
