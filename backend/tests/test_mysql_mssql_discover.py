from unittest.mock import MagicMock, patch

from app.tools.customer_db import (
    MySQLProvider,
    SQLServerProvider,
)


# ---------------------------------------------------------------------------
# MySQL schema discovery smoke test
# ---------------------------------------------------------------------------

@patch.object(MySQLProvider, "_connect")
def test_mysql_introspect_schema(mock_connect):
    """
    Smoke test: MySQL schema discovery should return
    tables, columns, primary keys, and foreign keys.
    """

    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    mock_connect.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    provider = MySQLProvider(
        "mysql://reader:reader@localhost:3307/customerdb"
    )

    with (
        patch.object(
            provider,
            "_mysql_list_tables",
            return_value=["customers", "orders"],
        ),
        patch.object(
            provider,
            "_mysql_list_columns",
            side_effect=[
                # customers table
                [
                    {
                        "name": "customer_id",
                        "data_type": "int",
                        "nullable": False,
                        "is_pk": False,
                    },
                    {
                        "name": "customer_name",
                        "data_type": "varchar",
                        "nullable": False,
                        "is_pk": False,
                    },
                ],

                # orders table
                [
                    {
                        "name": "order_id",
                        "data_type": "int",
                        "nullable": False,
                        "is_pk": False,
                    },
                    {
                        "name": "customer_id",
                        "data_type": "int",
                        "nullable": True,
                        "is_pk": False,
                    },
                ],
            ],
        ),
        patch.object(
            provider,
            "_mysql_list_primary_keys",
            side_effect=[
                ["customer_id"],
                ["order_id"],
            ],
        ),
        patch.object(
            provider,
            "_mysql_list_foreign_keys",
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

    # Basic schema response
    assert "tables" in result
    assert len(result["tables"]) == 2

    # customers
    customers = result["tables"][0]

    assert customers["table_name"] == "customers"

    assert customers["columns"][0]["name"] == "customer_id"
    assert customers["columns"][0]["is_pk"] is True

    assert customers["columns"][1]["name"] == "customer_name"
    assert customers["columns"][1]["is_pk"] is False

    assert customers["foreign_keys"] == []

    # orders
    orders = result["tables"][1]

    assert orders["table_name"] == "orders"

    assert orders["columns"][0]["name"] == "order_id"
    assert orders["columns"][0]["is_pk"] is True

    assert orders["columns"][1]["name"] == "customer_id"
    assert orders["columns"][1]["is_pk"] is False

    assert orders["foreign_keys"] == [
        {
            "column": "customer_id",
            "referred_table": "customers",
            "referred_column": "customer_id",
        }
    ]


# ---------------------------------------------------------------------------
# MSSQL schema discovery smoke test
# ---------------------------------------------------------------------------

@patch.object(SQLServerProvider, "_connect")
def test_mssql_introspect_schema(mock_connect):
    """
    Smoke test: MSSQL schema discovery should return
    tables, columns, primary keys, and foreign keys.
    """

    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    mock_connect.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    provider = SQLServerProvider(
        "mssql://reader:reader@localhost:1433/customerdb"
    )

    with (
        patch.object(
            provider,
            "_mssql_list_tables",
            return_value=["customers", "orders"],
        ),
        patch.object(
            provider,
            "_mssql_list_columns",
            side_effect=[
                # customers table
                [
                    {
                        "name": "customer_id",
                        "data_type": "int",
                        "nullable": False,
                        "is_pk": False,
                    },
                    {
                        "name": "customer_name",
                        "data_type": "varchar",
                        "nullable": False,
                        "is_pk": False,
                    },
                ],

                # orders table
                [
                    {
                        "name": "order_id",
                        "data_type": "int",
                        "nullable": False,
                        "is_pk": False,
                    },
                    {
                        "name": "customer_id",
                        "data_type": "int",
                        "nullable": True,
                        "is_pk": False,
                    },
                ],
            ],
        ),
        patch.object(
            provider,
            "_mssql_list_primary_keys",
            side_effect=[
                ["customer_id"],
                ["order_id"],
            ],
        ),
        patch.object(
            provider,
            "_mssql_list_foreign_keys",
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

    # Basic schema response
    assert "tables" in result
    assert len(result["tables"]) == 2

    # customers
    customers = result["tables"][0]

    assert customers["table_name"] == "customers"

    assert customers["columns"][0]["name"] == "customer_id"
    assert customers["columns"][0]["is_pk"] is True

    assert customers["columns"][1]["name"] == "customer_name"
    assert customers["columns"][1]["is_pk"] is False

    assert customers["foreign_keys"] == []

    # orders
    orders = result["tables"][1]

    assert orders["table_name"] == "orders"

    assert orders["columns"][0]["name"] == "order_id"
    assert orders["columns"][0]["is_pk"] is True

    assert orders["columns"][1]["name"] == "customer_id"
    assert orders["columns"][1]["is_pk"] is False

    assert orders["foreign_keys"] == [
        {
            "column": "customer_id",
            "referred_table": "customers",
            "referred_column": "customer_id",
        }
    ]
