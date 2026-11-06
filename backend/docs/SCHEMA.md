# Database Schema

## Overview

The schema discovery feature reads the structure of a connected customer
database and makes that structure available to the application.

Schema discovery identifies:

- Tables
- Columns
- Data types
- Nullable information
- Primary keys
- Foreign keys

Supported customer databases:

- PostgreSQL
- MySQL
- MSSQL (SQL Server)

The application stores the discovered schema so that it can be reused by
the application without requiring the customer database structure to be
rediscovered for every request.

---

## Schema Discovery Flow

```text
Customer Database
       |
       v
Saved Connection
       |
       v
Load Connection
       |
       v
Decrypt Connection URL
       |
       v
Select Database Provider
       |
       v
Introspect Database
       |
       +------------------+
       |                  |
       v                  v
     Tables            Columns
       |                  |
       +--------+---------+
                |
                v
        Primary Keys
                |
                v
        Foreign Keys
                |
                v
        Discovered Schema
                |
                v
        Store Schema
                |
                v
          Redis Cache
```

---

# Supported Database Providers

The schema discovery process uses a provider based on the connection's
database type.

| Database | `db_type` | Provider |
|----------|-----------|----------|
| PostgreSQL | `postgres` | PostgreSQL provider |
| MySQL | `mysql` | MySQL provider |
| MSSQL / SQL Server | `mssql` | SQL Server provider |

The provider is responsible for connecting to the customer database and
reading its metadata.

---

# Schema Information

For every discovered table, the application records the table name,
columns, and foreign-key relationships.

A table has the following logical structure:

```json
{
  "table_name": "orders",
  "columns": [],
  "foreign_keys": []
}
```

---

# Tables

A table represents a table discovered in the customer database.

Example:

```json
{
  "table_name": "customers"
}
```

For example, a customer database may contain:

```text
customers
orders
products
```

---

# Columns

Each discovered column contains:

| Field | Description |
|-------|-------------|
| `name` | Column name |
| `data_type` | Database data type |
| `nullable` | Whether the column allows NULL values |
| `is_pk` | Whether the column belongs to a primary key |

Example:

```json
{
  "name": "customer_id",
  "data_type": "integer",
  "nullable": false,
  "is_pk": true
}
```

Another example:

```json
{
  "name": "customer_name",
  "data_type": "character varying",
  "nullable": false,
  "is_pk": false
}
```

---

# Primary Keys

Primary-key information is discovered for each table.

Example database:

```text
customers
----------------
customer_id       PRIMARY KEY
customer_name
email
```

The API represents the primary-key column as:

```json
{
  "name": "customer_id",
  "data_type": "integer",
  "nullable": false,
  "is_pk": true
}
```

Primary-key information is important because it identifies the column that
uniquely identifies records in a table.

---

# Foreign Keys

Foreign-key relationships are discovered between tables.

Example:

```text
customers
----------------
customer_id  ← Primary Key


orders
----------------
order_id
customer_id  ← Foreign Key
```

The relationship is represented as:

```json
{
  "column": "customer_id",
  "referred_table": "customers",
  "referred_column": "customer_id"
}
```

This tells the application that:

```text
orders.customer_id
        |
        v
customers.customer_id
```

Foreign-key information helps the application understand relationships
between tables.

---

# Complete Schema Example

For a customer database containing `customers`, `orders`, and `products`,
the discovered schema can look like:

```json
{
  "connection_id": "5d1b66e8-f9c2-4a68-b97b-5f7c4dd2d1d8",
  "tables": [
    {
      "table_name": "customers",
      "columns": [
        {
          "name": "customer_id",
          "data_type": "integer",
          "nullable": false,
          "is_pk": true
        },
        {
          "name": "customer_name",
          "data_type": "character varying",
          "nullable": false,
          "is_pk": false
        },
        {
          "name": "email",
          "data_type": "character varying",
          "nullable": true,
          "is_pk": false
        }
      ],
      "foreign_keys": []
    },
    {
      "table_name": "orders",
      "columns": [
        {
          "name": "order_id",
          "data_type": "integer",
          "nullable": false,
          "is_pk": true
        },
        {
          "name": "customer_id",
          "data_type": "integer",
          "nullable": true,
          "is_pk": false
        }
      ],
      "foreign_keys": [
        {
          "column": "customer_id",
          "referred_table": "customers",
          "referred_column": "customer_id"
        }
      ]
    },
    {
      "table_name": "products",
      "columns": [
        {
          "name": "product_id",
          "data_type": "integer",
          "nullable": false,
          "is_pk": true
        },
        {
          "name": "product_name",
          "data_type": "character varying",
          "nullable": false,
          "is_pk": false
        },
        {
          "name": "price",
          "data_type": "numeric",
          "nullable": false,
          "is_pk": false
        }
      ],
      "foreign_keys": []
    }
  ]
}
```

---

# Schema Discovery API

## Discover Schema

### Endpoint

```http
POST /api/v1/connections/{connection_id}/discover
```

This endpoint connects to the customer database and discovers its schema.

### Response

```json
{
  "connection_id": "5d1b66e8-f9c2-4a68-b97b-5f7c4dd2d1d8",
  "tables": [
    {
      "table_name": "orders",
      "columns": [],
      "foreign_keys": []
    }
  ]
}
```

---

## Get Schema

### Endpoint

```http
GET /api/v1/connections/{connection_id}/schema
```

Returns the previously discovered schema for the connection.

The schema contains:

- Table information
- Column information
- Primary-key information
- Foreign-key relationships

If schema discovery has not been performed, an empty `tables` list is
returned.

---

# Schema Storage

After schema discovery, the discovered schema is stored in the platform
database.

The stored schema can then be used by the application when it needs to
understand the structure of a customer's database.

The schema represents database metadata rather than the customer's complete
business data.

For example, the application stores information such as:

```text
orders
    order_id
    customer_id
    order_date
```

rather than copying all rows from the `orders` table.

---

# Schema Cache

The discovered schema can also be cached for faster access.

The logical cache key is:

```text
schema:{tenant_id}:{connection_id}
```

The cache is associated with the tenant and connection, which keeps schema
information isolated between tenants and database connections.

The schema retrieval flow is:

```text
GET /connections/{connection_id}/schema
                |
                v
          Check Redis
          /                Found       Not Found
         |             |
         v             v
      Return      Read stored schema
                      |
                      v
                Populate cache
                      |
                      v
                   Return
```

---

# Schema Refresh

When the customer database structure changes, schema discovery can be run
again:

```http
POST /api/v1/connections/{connection_id}/discover
```

The application can then refresh the stored schema representation.

For example, if a new column is added:

```text
Before:

customers
    customer_id
    customer_name


After:

customers
    customer_id
    customer_name
    city
```

Running schema discovery again updates the discovered representation.

---

# Why Schema Discovery Is Needed

The application needs to understand the customer database structure before
it can reliably work with customer data.

For example, a user may ask:

> Show me the number of orders for each customer.

The application needs to know that:

```text
customers
    customer_id

orders
    customer_id
```

are related.

The discovered schema provides the metadata required to understand these
relationships.

---

# Security Considerations

Schema discovery uses the database connection associated with the
authenticated tenant.

The customer database connection should use a read-only database user.

Schema discovery reads database metadata and should not modify customer
data.

Connection credentials are handled by the connection-management layer and
are not included in the discovered schema response.

---

# Database-Specific Discovery

## PostgreSQL

The PostgreSQL provider discovers:

- Tables
- Columns
- Data types
- Nullable information
- Primary keys
- Foreign keys

## MySQL

The MySQL provider discovers:

- Tables
- Columns
- Data types
- Nullable information
- Primary keys
- Foreign keys

## MSSQL

The MSSQL provider discovers:

- Tables
- Columns
- Data types
- Nullable information
- Primary keys
- Foreign keys

The schema returned by each provider follows the same logical structure so
that the rest of the application can work with different database types
consistently.

---

# Multi-Schema Discovery

Discovery never hardcodes `public` or `dbo`. Each connection stores tables
with an explicit `schema_name` (for example `acct_us.orders` and
`billing_eu.orders` on the same connection).

| Field | Description |
|-------|-------------|
| `schema_name` | Physical schema namespace from the customer database |
| `table_name` | Bare table name within that schema |
| `referred_schema` | Target schema for foreign-key joins (always populated) |

When one schema is inaccessible, discovery continues for the rest and
returns status **`partial`** with warnings. No privileges are elevated.

---

# Discovery Job Lifecycle

Connection create returns immediately; discovery runs in a background job
(or inline during tests via sync mode).

| Status | Meaning |
|--------|---------|
| `queued` | Run record created, waiting to start |
| `running` | Introspection in progress |
| `completed` | All target schemas discovered |
| `partial` | Some schemas skipped (permission or error warnings) |
| `failed` | No tables discovered or unrecoverable error |

### Endpoints

```http
POST /api/v1/connections/{connection_id}/discover
GET  /api/v1/connections/{connection_id}/discover/status
GET  /api/v1/connections/{connection_id}/schemas
```

`scope=default` uses the provider default schema. `scope=all` discovers every
schema returned by `list_schemas()`. `scope=selected` accepts an explicit
schema list.

---

# Semantic Layer (Connection-Scoped)

After structural discovery, semantic suggestions are generated per
connection:

```http
POST /api/v1/semantic/discover/{connection_id}
GET  /api/v1/semantic?connection_id={connection_id}
```

Semantic rows include both display fields (`name`, `label`) and physical
refs (`physical_schema`, `physical_table`, `physical_column`,
`source_column_ref`).

**Rediscovery rules:**

- `status=confirmed` — physical metadata may update; `name`, `label`, and
  `expression` are never overwritten automatically.
- `status=suggested` — upserted by `source_column_ref`; missing columns
  become `deprecated`.

The SQL compiler builds JOINs only from discovered foreign keys plus
**confirmed** semantic relationships. Multiple valid paths without a
confirmed relationship raise `JoinAmbiguityError` and the agent asks for
clarification instead of guessing SQL.

---

# Testing

Run PostgreSQL schema discovery tests:

```bash
python -m pytest tests/test_postgres_provider.py -v
```

Run schema discovery service tests:

```bash
python -m pytest tests/test_discover_schema.py -v
```

Run schema discovery API tests:

```bash
python -m pytest tests/test_discover_api.py -v
```

Run multi-schema E2E and isolation tests:

```bash
python -m pytest tests/test_schema_discovery_e2e.py tests/test_semantic_connection_isolation.py -v
```

Run MySQL and MSSQL discovery tests:

```bash
python -m pytest tests/test_mysql_mssql_discover.py -v
```

Run all tests:

```bash
python -m pytest -v
```

---

# Schema Discovery Summary

```text
Customer Database
       ↓
Database Connection
       ↓
Database Provider
       ↓
Schema Introspection
       ↓
┌─────────────────────┐
│ Tables              │
│ Columns             │
│ Primary Keys        │
│ Foreign Keys        │
└─────────────────────┘
       ↓
Store Discovered Schema
       ↓
Cache Schema
       ↓
Application Uses Schema
```

The schema discovery feature provides a common database structure to the
application regardless of whether the customer uses PostgreSQL, MySQL, or
MSSQL.
