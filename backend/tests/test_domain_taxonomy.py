"""Tests for app.agent.domain_taxonomy — business-abbreviation expansion
used to improve auto-derived domain labels."""

from __future__ import annotations

from app.agent.domain_taxonomy import expand_domain_label


def test_expand_domain_label_expands_known_abbreviation() -> None:
    assert expand_domain_label("Ar Txn") == "Accounts Receivable Txn"


def test_expand_domain_label_expands_single_word_abbreviation() -> None:
    assert expand_domain_label("Ar") == "Accounts Receivable"


def test_expand_domain_label_leaves_unrecognized_words_untouched() -> None:
    assert expand_domain_label("Sales") == "Sales"
    assert expand_domain_label("Production") == "Production"


def test_expand_domain_label_expands_multiple_abbreviations() -> None:
    assert expand_domain_label("Mfg Whse Movement") == "Manufacturing Warehouse Movement"


def test_expand_domain_label_is_case_insensitive() -> None:
    assert expand_domain_label("AR Txn") == "Accounts Receivable Txn"


def test_expand_domain_label_empty_string_is_noop() -> None:
    assert expand_domain_label("") == ""
