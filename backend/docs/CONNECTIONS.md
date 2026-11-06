# Database Connections API

## Overview

The Database Connections API allows an authenticated tenant to securely
create and manage connections to external customer databases.

Supported databases:

- PostgreSQL
- MySQL
- MSSQL (SQL Server)

Each connection belongs to one tenant.

Database connection URLs contain sensitive credentials. They are encrypted
before being stored and are never returned in API responses.

---

## Authentication

All connection endpoints require a valid JWT access token.

```http
Authorization: Bearer <access_token>
```

---

## Connection Workflow

```text
Login / Verify OTP
        ↓
Get Access Token
        ↓
Create Connection
        ↓
Test Connection
        ↓
Discover Schema
        ↓
Use Connection
        ↓
 ┌──────────────┬──────────────┐
 ↓              ↓              ↓
Update        Get Schema     Delete
```

---

# Supported Database Types

| Database | db_type |
|----------|---------|
| PostgreSQL | `postgres` |
| MySQL | `mysql` |
| SQL Server | `mssql` |

---

# 1. Create Connection

### Endpoint

```http
POST /api/v1/connections
```

Creates a new database connection for the authenticated tenant.

### Request

```json
{
  "name": "Customer Database",
  "db_type": "postgres",
  "connection_url": "postgresql://reader:reader@localhost:5433/customerdb"
}
```

### Request Fields

| Field | Description |
|-------|-------------|
| `name` | Name of the database connection |
| `db_type` | `postgres`, `mysql`, or `mssql` |
| `connection_url` | Database connection URL |

### Example Connection URLs

PostgreSQL:

```text
postgresql://reader:reader@localhost:5433/customerdb
```

MySQL:

```text
mysql://reader:reader@localhost:3307/customerdb
```

MSSQL:

```text
mssql://reader:Reader1234!@localhost:1433/customerdb
```

These are local development examples.

### Validation

Before saving the connection, the API:

1. Tests database connectivity.
2. Validates the credentials.
3. Checks that the database user is read-only.
4. Encrypts the connection URL.
5. Saves the connection.

If validation fails, the connection is not saved.

### Successful Response

**201 Created**

```json
{
  "id": "5d1b66e8-f9c2-4a68-b97b-5f7c4dd2d1d8",
  "tenant_id": "67d0d9d2-f0db-4c75-b0d3-93387ef8c4a2",
  "name": "Customer Database",
  "db_type": "postgres",
  "last_discover_status": "queued"
}
```

The returned `id` is the `connection_id` used by the other connection
endpoints. Schema discovery is enqueued automatically after create; poll
discover status or fetch schema once discovery completes.

### Failed Response

**400 Bad Request**

```json
{
  "detail": "Connection failed: OperationalError"
}
```

If the database user has write privileges:

```json
{
  "detail": "Database user has write privileges — please use a read-only user"
}
```

---

# 2. List Connections

### Endpoint

```http
GET /api/v1/connections
```

Returns the connections belonging to the authenticated tenant.

### Successful Response

```json
[
  {
    "id": "5d1b66e8-f9c2-4a68-b97b-5f7c4dd2d1d8",
    "tenant_id": "67d0d9d2-f0db-4c75-b0d3-93387ef8c4a2",
    "name": "Customer Database",
    "db_type": "postgres"
  }
]
```

Only connections belonging to the authenticated tenant are returned.

Sensitive credentials are never included.

---

# 3. Test Connection

### Endpoint

```http
POST /api/v1/connections/{connection_id}/test
```

Tests an existing saved connection.

The API checks:

- Database reachability
- Credentials
- Read-only access

### Successful Response

```json
{
  "ok": true,
  "message": "Connection successful (read-only)"
}
```

---

# 4. Discover Schema

### Endpoint

```http
POST /api/v1/connections/{connection_id}/discover
```

Discovers the structure of the connected customer database. Supports
multi-schema targets (`scope=all` or `scope=selected`) without defaulting
to `public`/`dbo`.

Optional body:

```json
{
  "scope": "all",
  "schemas": ["acct_us", "billing_eu"],
  "profile": false,
  "include_views": false
}
```

### Discovery status

```http
GET /api/v1/connections/{connection_id}/discover/status
```

Returns the latest run with lifecycle state: `queued`, `running`,
`completed`, `partial`, or `failed`, plus warnings when applicable.

### List schemas

```http
GET /api/v1/connections/{connection_id}/schemas
```

Returns schemas the provider can see (permission-filtered; inaccessible
schemas are omitted).

The discovery process identifies:

- Tables
- Columns
- Data types
- Nullable fields
- Primary keys
- Foreign keys

### Example

A customer database may contain:

```text
customers
    customer_id
    customer_name
    email

orders
    order_id
    customer_id
    order_date
```

The API can discover the relationship:

```text
orders.customer_id
        ↓
customers.customer_id
```

### Successful Response

**200 OK**

```json
{
  "connection_id": "5d1b66e8-f9c2-4a68-b97b-5f7c4dd2d1d8",
  "tables": [
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
    }
  ]
}
```

---

# 5. Get Schema

### Endpoint

```http
GET /api/v1/connections/{connection_id}/schema
```

Returns the previously discovered schema for the connection.

The returned schema contains:

- Tables
- Columns
- Primary keys
- Foreign keys

If schema discovery has not been performed, an empty table list is returned.

---

# 6. Update Connection

### Endpoint

```http
PATCH /api/v1/connections/{connection_id}
```

Updates an existing database connection.

### Rename Connection

```json
{
  "name": "Analytics Database"
}
```

### Update Database Credentials

```json
{
  "name": "Analytics Database",
  "db_type": "postgres",
  "connection_url": "postgresql://reader:reader@localhost:5433/customerdb"
}
```

When a new connection URL is supplied, the new database connection is
tested before the existing connection is updated.

If validation fails, the existing connection remains unchanged.

---

# 7. Delete Connection

### Endpoint

```http
DELETE /api/v1/connections/{connection_id}
```

Deletes the specified connection.

### Successful Response

**204 No Content**

No response body is returned.

---

# Security

## JWT Authentication

All connection endpoints require a valid JWT access token.

## Tenant Isolation

Each connection belongs to a tenant.

A tenant can only access its own connections.

## Read-Only Database Access

Customer database connections must use a read-only database user.

Write-capable users are rejected.

The application checks for write privileges such as:

```text
INSERT
UPDATE
DELETE
TRUNCATE
```

Database-specific privileged roles are also checked where applicable.

## Credential Protection

Connection URLs contain database credentials.

They are encrypted before being stored.

The API never returns:

- Raw connection URLs
- Database passwords
- Encrypted connection URLs

---

# API Summary

| Operation | Method | Endpoint |
|-----------|--------|----------|
| Create Connection | POST | `/api/v1/connections` |
| List Connections | GET | `/api/v1/connections` |
| Test Connection | POST | `/api/v1/connections/{connection_id}/test` |
| Discover Schema | POST | `/api/v1/connections/{connection_id}/discover` |
| Discovery Status | GET | `/api/v1/connections/{connection_id}/discover/status` |
| List Schemas | GET | `/api/v1/connections/{connection_id}/schemas` |
| Get Schema | GET | `/api/v1/connections/{connection_id}/schema` |
| Update Connection | PATCH | `/api/v1/connections/{connection_id}` |
| Delete Connection | DELETE | `/api/v1/connections/{connection_id}` |

---

# Postman Testing Flow

The recommended order for testing is:

```text
1. Login
      ↓
2. Verify OTP
      ↓
3. Get access token
      ↓
4. Create Connection
      ↓
5. Test Connection
      ↓
6. Discover Schema
      ↓
7. Get Schema
      ↓
8. Update Connection
      ↓
9. Delete Connection
```

The `connection_id` returned by the Create Connection API is used by all
connection-specific endpoints.

---

# Testing

Run connection tests:

```bash
python -m pytest tests/test_connections.py -v
```

Run PostgreSQL schema discovery tests:

```bash
python -m pytest tests/test_discover_schema.py -v
```

Run MySQL and MSSQL schema discovery tests:

```bash
python -m pytest tests/test_mysql_mssql_discover.py -v
```

Run schema discovery API tests:

```bash
python -m pytest tests/test_discover_api.py -v
```

Run all tests:

```bash
python -m pytest -v
```

---

# Summary

The Database Connections API provides a secure way for a tenant to connect
the application to an external customer database.

The main flow is:

```text
Create
  ↓
Validate
  ↓
Save
  ↓
Test
  ↓
Discover Schema
  ↓
Get Schema
  ↓
Update / Delete
```

Customer database credentials are protected, tenant isolation is enforced,
and customer database access is restricted to read-only users.
