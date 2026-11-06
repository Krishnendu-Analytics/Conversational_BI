"""T-30: lock the schema JSON contract so discover work stays consistent."""

from app.schemas import (
    DiscoveredTable,
    SchemaColumn,
    SchemaDiscoverResponse,
    SchemaForeignKey,
    SchemaTablePayload,
)


def test_schema_table_payload_round_trip() -> None:
    # Same shape we will store in schema_tables.columns_json
    raw = {
        "columns": [
            {
                "name": "order_id",
                "data_type": "integer",
                "nullable": False,
                "is_pk": True,
            },
            {
                "name": "customer_id",
                "data_type": "integer",
                "nullable": True,
                "is_pk": False,
            },
        ],
        "foreign_keys": [
            {
                "column": "customer_id",
                "referred_table": "customers",
                "referred_column": "customer_id",
            }
        ],
    }

    payload = SchemaTablePayload.model_validate(raw)
    assert len(payload.columns) == 2
    assert payload.columns[0].is_pk is True
    assert payload.foreign_keys[0].referred_table == "customers"

    # Dumping should stay stable for DB storage.
    dumped = payload.model_dump()
    again = SchemaTablePayload.model_validate(dumped)
    assert again == payload


def test_discovered_table_and_discover_response() -> None:
    table = DiscoveredTable(
        table_name="orders",
        columns=[
            SchemaColumn(
                name="order_id",
                data_type="integer",
                nullable=False,
                is_pk=True,
            )
        ],
        foreign_keys=[
            SchemaForeignKey(
                column="customer_id",
                referred_table="customers",
                referred_column="customer_id",
            )
        ],
    )

    # connection_id is a placeholder UUID string shape for the contract test
    from uuid import uuid4

    response = SchemaDiscoverResponse(
        connection_id=uuid4(),
        tables=[table],
    )
    assert response.tables[0].table_name == "orders"
    assert response.tables[0].foreign_keys[0].column == "customer_id"
