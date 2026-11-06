from unittest.mock import MagicMock, patch

from app.tools.customer_db import (
    PostgreSQLProvider,
    ConnectionTestResult,
)


@patch("app.tools.customer_db.psycopg.connect")
def test_postgres_connection_success(mock_connect):
    """
    PostgreSQLProvider.test_connection should return success
    when psycopg.connect succeeds.
    """

    mock_conn = MagicMock()
    mock_connect.return_value.__enter__.return_value = mock_conn

    provider = PostgreSQLProvider(
        "postgresql://reader:reader@localhost:5433/customerdb"
    )

    result = provider.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.ok is True
    assert result.message == "Connection successful"

    mock_connect.assert_called_once()


def test_postgres_list_primary_keys():
    """
    PostgreSQLProvider should discover the primary-key
    columns returned by PostgreSQL.
    """

    mock_cursor = MagicMock()

    # Simulate PostgreSQL returning customer_id as the PK.
    mock_cursor.fetchall.return_value = [
        ("customer_id",),
    ]

    provider = PostgreSQLProvider(
        "postgresql://reader:reader@localhost:5433/customerdb"
    )

    result = provider._pg_list_primary_keys(
        mock_cursor,
        "public",
        "customers",
    )

    assert result == ["customer_id"]

    mock_cursor.execute.assert_called_once()
    
    
def test_postgres_list_foreign_keys():
    """
    PostgreSQLProvider should discover foreign-key relationships
    returned by PostgreSQL.
    """

    mock_cursor = MagicMock()

    # Simulate the foreign keys on the orders table.
    mock_cursor.fetchall.return_value = [
        (
            "customer_id",
            "public",
            "customers",
            "customer_id",
        ),
        (
            "product_id",
            "public",
            "products",
            "product_id",
        ),
    ]

    provider = PostgreSQLProvider(
        "postgresql://reader:reader@localhost:5433/customerdb"
    )

    result = provider._pg_list_foreign_keys(
        mock_cursor,
        "public",
        "orders",
    )

    assert result == [
        {
            "column": "customer_id",
            "referred_table": "customers",
            "referred_column": "customer_id",
        },
        {
            "column": "product_id",
            "referred_table": "products",
            "referred_column": "product_id",
        },
    ]

    mock_cursor.execute.assert_called_once()
    
    
@patch("app.tools.customer_db.psycopg.connect")
def test_postgres_introspect_schema(mock_connect):
            """
            PostgreSQLProvider.introspect_schema should combine
            tables, columns, primary keys, and foreign keys
            into the expected schema structure.
            """

            mock_conn = MagicMock()
            mock_cursor = MagicMock()

            mock_connect.return_value.__enter__.return_value = mock_conn
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

            provider = PostgreSQLProvider(
                "postgresql://reader:reader@localhost:5433/customerdb"
            )

            # Mock the helper methods so this test focuses on
            # introspect_schema() assembling the final result.
            with (
                patch.object(
                    provider,
                    "_pg_list_tables",
                    return_value=["customers", "orders"],
                ),
                patch.object(
                    provider,
                    "_pg_list_columns",
                    side_effect=[
                        # customers
                        [
                            {
                                "name": "customer_id",
                                "data_type": "integer",
                                "nullable": False,
                                "is_pk": False,
                            },
                            {
                                "name": "customer_name",
                                "data_type": "character varying",
                                "nullable": False,
                                "is_pk": False,
                            },
                        ],
                        # orders
                        [
                            {
                                "name": "order_id",
                                "data_type": "integer",
                                "nullable": False,
                                "is_pk": False,
                            },
                            {
                                "name": "customer_id",
                                "data_type": "integer",
                                "nullable": True,
                                "is_pk": False,
                            },
                        ],
                    ],
                ),
                patch.object(
                    provider,
                    "_pg_list_primary_keys",
                    side_effect=[
                        ["customer_id"],
                        ["order_id"],
                    ],
                ),
                patch.object(
                    provider,
                    "_pg_list_foreign_keys",
                    side_effect=[
                        [],
                        [
                            {
                                "column": "customer_id",
                                "referred_table": "customers",
                                "referred_column": "customer_id",
                            }
                        ],
                    ],
                ),
            ):
                result = provider.introspect_schema()

            assert len(result["tables"]) == 2

            customers = result["tables"][0]

            assert customers["table_name"] == "customers"
            assert customers["columns"][0]["name"] == "customer_id"
            assert customers["columns"][0]["is_pk"] is True
            assert customers["columns"][1]["is_pk"] is False
            assert customers["foreign_keys"] == []

            orders = result["tables"][1]

            assert orders["table_name"] == "orders"
            assert orders["columns"][0]["name"] == "order_id"
            assert orders["columns"][0]["is_pk"] is True

            assert orders["foreign_keys"] == [
                {
                    "column": "customer_id",
                    "referred_table": "customers",
                    "referred_column": "customer_id",
                }
            ]
