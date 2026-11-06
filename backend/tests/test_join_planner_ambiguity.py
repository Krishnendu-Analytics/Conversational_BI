"""Join planner ambiguity detection."""

from __future__ import annotations

import pytest

from app.agent.join_planner import JoinAmbiguityError, build_join_clauses


def _diamond_schema() -> dict:
    return {
        "tables": [
            {
                "schema_name": "sales",
                "table_name": "orders",
                "columns": [],
                "foreign_keys": [
                    {
                        "column": "customer_id",
                        "referred_schema": "sales",
                        "referred_table": "customers",
                        "referred_column": "customer_id",
                    },
                    {
                        "column": "store_id",
                        "referred_schema": "sales",
                        "referred_table": "stores",
                        "referred_column": "store_id",
                    },
                ],
            },
            {
                "schema_name": "sales",
                "table_name": "customers",
                "columns": [],
                "foreign_keys": [
                    {
                        "column": "region_id",
                        "referred_schema": "sales",
                        "referred_table": "regions",
                        "referred_column": "region_id",
                    }
                ],
            },
            {
                "schema_name": "sales",
                "table_name": "stores",
                "columns": [],
                "foreign_keys": [
                    {
                        "column": "region_id",
                        "referred_schema": "sales",
                        "referred_table": "regions",
                        "referred_column": "region_id",
                    }
                ],
            },
            {
                "schema_name": "sales",
                "table_name": "regions",
                "columns": [],
                "foreign_keys": [],
            },
        ]
    }


def test_two_fk_paths_raise_join_ambiguity() -> None:
    required = {"sales.orders", "sales.regions"}
    with pytest.raises(JoinAmbiguityError) as exc_info:
        build_join_clauses(required, _diamond_schema(), confirmed_relationships=[])
    assert exc_info.value.candidates
