# BI Agent Runbook

## 1. Purpose

The BI Agent converts a natural-language business question into a safe database query and returns a business-friendly answer.

The current implementation uses LangGraph to orchestrate the agent stages. The workflow supports SQL, greeting, RAG, and hybrid intent routing. SQL is the end-to-end data path; RAG retrieval is contract-wired via `retrieve_docs` (stub chunks until embeddings are live). Hybrid runs SQL then RAG and merges both into insight generation. Progress events are published to Redis pub/sub for the WebSocket layer.

This document explains how to run one question locally and how the agent nodes work.

---

## 2. Current Workflow

High-level flow:

```text
User Question
     |
     v
run_question()
     |
     v
BIWorkflow.run()
     |
     v
classify_intent
     |
     +-------------------- greeting --------------------+
     |                                                   |
     |                                                   v
     |                                            generate_insight
     |                                                   |
     |                                                   v
     |                                                  END
     |
     +-------------------- rag -------------------------+
     |                                                   |
     |                                                   v
     |                                            retrieve_docs
     |                                                   |
     |                                                   v
     |                                            generate_insight
     |                                                   |
     |                                                   v
     |                                                  END
     |
     +-------------------- hybrid / sql ----------------+
                                                         |
                                                         v
                                                    generate_sql
                                                         |
                                                         v
                                                    execute_query
                                                         |
                         hybrid --> retrieve_docs -------+
                                                         |
                                                         v
                                                   recommend_chart
                                                         |
                                                         v
                                                  generate_insight
                                                         |
                                                         v
                                                        END
```

The LangGraph nodes currently defined are:

- `classify_intent`
- `generate_sql`
- `execute_query`
- `retrieve_docs`
- `recommend_chart`
- `generate_insight`

---

## 3. Main Entry Point

The application-facing entry point is:

```python
from app.agent.service import run_question
```

`run_question()` accepts an `AgentQuestion`, creates a `BIWorkflow`, runs it, and maps the workflow output into an `AgentResult`.

Conceptually:

```text
AgentQuestion
     |
     v
run_question()
     |
     v
BIWorkflow.run()
     |
     v
AgentResult
```

The agent is intentionally kept behind `run_question()`. If the agent is later moved to a separate service, this boundary can be changed to make an HTTP call without requiring FastAPI routes to know the internal workflow implementation.

---

## 4. Agent Input

`AgentQuestion` contains:

```text
tenant_id
user_id
question
connection_id
conversation_id (optional)
```

Example:

```python
AgentQuestion(
    tenant_id=tenant_id,
    user_id=user_id,
    question="How many orders were placed?",
    connection_id=connection_id,
    conversation_id=conversation_id,
)
```

### Why these identifiers matter

- `tenant_id`: identifies the tenant requesting the analysis.
- `user_id`: identifies the user making the request.
- `connection_id`: identifies the customer database to query.
- `conversation_id`: optionally identifies the conversation context.
- `question`: contains the natural-language business question.

---

## 5. Running One Question Locally

### Prerequisites

Make sure:

1. The Python virtual environment is activated.
2. Project dependencies are installed.
3. The platform database is available.
4. Redis is available.
5. The customer database connection exists if the question requires SQL execution.
6. The customer's schema has been discovered and is available to the schema cache.
7. LLM configuration is available if using the real LLM path.

### Run through the application

The normal application path should construct an `AgentQuestion` and call:

```python
from app.agent.service import run_question

result = run_question(payload)
```

For an SQL question, use a question that can be answered from the discovered customer schema, for example:

```text
How many orders are there?
```

The exact question should match the tables and columns available in the selected customer connection.

### What the result contains

`AgentResult` contains:

```text
answer
sql
rows
chart_suggestion
steps
error
```

For example:

```python
{
    "answer": "...",
    "sql": "SELECT ...",
    "rows": [...],
    "chart_suggestion": None,
    "steps": [
        "classify_intent",
        "generate_sql",
        "execute_query",
        "recommend_chart",
        "generate_insight",
    ],
    "error": None,
}
```

---

## 6. Workflow State

The LangGraph workflow shares a typed `WorkflowState`.

The state contains:

```text
tenant_id
user_id
connection_id
conversation_id
question
intent
sql
rows
chart_suggestion
answer
error
steps
```

`steps` is accumulated as the graph executes. Each node appends its stage name so the final result can expose the workflow path.

---

## 7. Node 1 — Intent Classification

### Node

```text
classify_intent
```

Implementation:

```text
IntentClassifier.run()
```

The classifier first asks the configured LLM to classify the question using the versioned `intent_classifier` prompt.

Supported intents:

```text
sql
rag
hybrid
greeting
```

### LLM classification

The prompt instructs the LLM to return exactly one category.

The implementation then checks the returned text and maps it to one of the supported intents.

### Deterministic fallback

If the LLM is unavailable, incorrectly configured, or returns an unexpected value, the classifier falls back to a simple heuristic:

- greeting keywords → `greeting`
- otherwise → `sql`

Greeting keywords currently include:

```text
hello
hi
hey
good morning
good afternoon
good evening
thanks
thank you
```

The node records:

```text
classify_intent
```

in `steps`.

---

## 8. Intent Routing

After `classify_intent`, the graph chooses the next node.

### Greeting

```text
greeting
   ↓
generate_insight
```

### RAG

```text
rag
   ↓
retrieve_docs
   ↓
generate_insight
```

### Hybrid

```text
hybrid
   ↓
generate_sql → execute_query → retrieve_docs → recommend_chart → generate_insight
```

### SQL

```text
sql
   ↓
generate_sql
```

### Current limitation

RAG retrieval is contract-complete but still stubbed (no embeddings / pgvector yet).
`retrieve_docs` writes `rag_query` / `rag_chunks` / `rag_context` into workflow state
so hybrid merge works; chunk lists are empty until real retrieval is wired.


---

## 9. Node 2 — SQL Generation

### Node

```text
generate_sql
```

Implementation:

```text
SQLGenerator.run()
```

The SQL generator uses:

1. Tenant ID
2. Connection ID
3. Discovered schema
4. Semantic context
5. User question
6. Database type
7. Versioned SQL-generation prompt
8. LLM

### Schema retrieval

The generator retrieves the discovered schema from:

```text
SchemaCache
```

using:

```text
tenant_id
connection_id
```

If the schema is unavailable, SQL generation is skipped and the workflow continues with no SQL.

### Semantic context

Semantic context is retrieved from:

```text
SemanticCache
```

using the tenant ID.

### Database type

The SQL prompt receives the database type so the generated SQL can target the correct database engine.

If the discovered schema does not contain a database type, the workflow returns:

```text
Database type is missing from the discovered schema.
```

### Prompt

The versioned prompt is:

```text
sql_generation
```

Current prompt version:

```text
v1
```

The prompt requires:

- SELECT-only SQL
- Existing tables only
- Existing columns only
- No INSERT
- No UPDATE
- No DELETE
- No DROP
- No ALTER
- No TRUNCATE
- No invented tables or columns

---

## 10. Node 3 — SQL Safety

### Node

SQL safety is applied inside:

```text
execute_query
```

through:

```python
SQLSafetyPipeline(schema=schema).validate(sql)
```

The safety pipeline contains four stages:

```text
SyntaxValidator
       ↓
StatementValidator
       ↓
SchemaValidator
       ↓
ExecutionGuard
```

### 10.1 SyntaxValidator

Uses SQLGlot to parse the generated SQL.

Invalid SQL is rejected with:

```text
Invalid SQL syntax
```

### 10.2 StatementValidator

Only SELECT statements are allowed.

A `WITH ... SELECT ...` statement is also supported by the current validator.

Write operations such as:

```text
INSERT
UPDATE
DELETE
DROP
ALTER
TRUNCATE
```

are rejected.

### 10.3 SchemaValidator

The generated SQL is checked against the discovered schema.

The validator builds a whitelist of:

```text
table -> columns
```

It checks:

- referenced tables
- qualified columns
- unqualified columns

If the schema is missing or invalid, validation fails closed rather than allowing arbitrary SQL.

### 10.4 ExecutionGuard

The execution guard provides:

- maximum row limit
- statement timeout configuration

The current default row limit is:

```text
10000 rows
```

The current timeout configuration is:

```text
15 seconds
```

If a SELECT does not already have a LIMIT, the guard adds one.

---

## 11. Node 4 — Query Execution

### Node

```text
execute_query
```

Implementation:

```text
QueryExecutor.run()
```

Before execution, it verifies:

1. A connection ID exists.
2. SQL exists.
3. A discovered schema exists.

Then it:

```text
Generated SQL
     ↓
SQLSafetyPipeline
     ↓
Validated SQL
     ↓
run_readonly_query()
     ↓
Customer Database
```

`run_readonly_query()` loads the saved connection, decrypts the stored connection URL in memory, creates the appropriate database provider, and executes the read-only query.

The workflow returns database rows as:

```text
list[dict]
```

If execution fails, the workflow records an error such as:

```text
SQL execution blocked: <ExceptionType>: <message>
```

and returns an empty row list.

---

## 12. Customer Database Providers

Customer database access is abstracted through `DatabaseProvider`.

Supported provider types are:

```text
PostgreSQL
MySQL
MSSQL
```

The provider factory selects the implementation based on the database type.

### PostgreSQL

The PostgreSQL provider supports:

- connection testing
- read-only privilege checking
- schema introspection
- read-only query execution

### MySQL

The MySQL provider supports:

- connection testing
- read-only privilege checking
- schema introspection

MySQL read-only query execution is currently not implemented.

### MSSQL

The SQL Server provider supports:

- connection testing
- read-only privilege checking
- schema introspection

MSSQL read-only query execution is currently not implemented.

---

## 13. Read-Only Database Requirement

The BI Agent is designed to query customer databases using read-only credentials.

The connection layer validates read-only access before saving/testing a connection.

Examples of rejected privileges include:

```text
INSERT
UPDATE
DELETE
DROP
ALTER
CREATE
ALL PRIVILEGES
```

The exact read-only checks depend on the database engine.

Do not configure the BI Agent with a superuser or write-enabled production database account.

---

## 14. Node 5 — Chart Recommendation

### Node

```text
recommend_chart
```

Implementation:

```text
ChartRecommender.run()
```

This is currently a stub.

It returns:

```text
chart_suggestion = None
```

and records:

```text
recommend_chart
```

in the workflow steps.

Chart recommendation is therefore a future extension point.

---

## 15. Node 6 — Insight Generation

### Node

```text
generate_insight
```

Implementation:

```text
InsightGenerator.run()
```

The behavior depends on the intent.

### Greeting

Returns:

```text
Hello! Ask me a question about your data.
```

### RAG / Hybrid

Uses `merge_sql_and_rag_for_insight()`:

- RAG: prefer `rag_context` / retrieval answer seed
- Hybrid: insights prompt with SQL/rows plus document context appended

### SQL

The SQL result is passed to the versioned:

```text
insights
```

prompt.

The prompt receives:

```text
question
sql
rows
```

The LLM then produces a concise business-friendly answer.

The prompt explicitly instructs the LLM not to invent information that is not present in the query result.

---

## 16. Versioned Prompt Templates

Prompt templates are centralized in:

```text
app/agent/prompts.py
```

Current prompt version:

```text
v1
```

Current prompts:

```text
intent_classifier
sql_generation
insights
```

Templates are represented by the immutable `PromptTemplate` dataclass.

Each prompt has:

```text
name
version
template
```

Prompts are retrieved using:

```python
get_prompt(name, version="v1")
```

This keeps prompt content separate from workflow logic and allows prompts to be versioned and tested independently.

---

## 17. Caching

### Schema cache

The discovered customer schema is cached using:

```text
SchemaCache
```

The key is based on:

```text
tenant_id
connection_id
```

The schema is used by SQL generation and SQL safety validation.

### Semantic cache

Semantic context is retrieved using:

```text
SemanticCache
```

and is scoped by tenant.

### Redis

The schema cache is backed by the application's Redis caching layer.

A schema discovery operation stores the discovered schema in the persistent schema tables and then writes the result to Redis for fast access.

---

## 18. Schema Discovery

Schema discovery is performed against the selected customer database.

The discovery flow is:

```text
Connection
    ↓
Decrypt connection URL
    ↓
ProviderFactory
    ↓
Provider.introspect_schema()
    ↓
Discovered tables
    ↓
schema_tables
    ↓
Redis SchemaCache
```

For each table, the schema contains:

```text
table_name
columns
foreign_keys
```

Each column contains:

```text
name
data_type
nullable
is_pk
```

Foreign keys contain:

```text
column
referred_table
referred_column
```

---

## 19. Error Handling

The workflow generally keeps the graph moving when an optional stage cannot produce data.

Examples:

### Missing connection ID

Query execution returns empty rows.

### Missing SQL

Query execution returns empty rows.

### Missing schema

SQL generation or query execution may continue with an empty result depending on the stage, while the final response exposes the available error/state.

### SQL validation/execution error

The workflow returns:

```text
rows = []
error = "SQL execution blocked: ..."
```

### Missing LLM configuration

Intent classification has a deterministic fallback. Other LLM-dependent stages depend on the configured LLM client and its fallback behavior.

---

## 20. Example SQL Workflow

For a question such as:

```text
How many orders are there?
```

the expected SQL path is:

```text
Question
  ↓
IntentClassifier
  ↓
sql
  ↓
SQLGenerator
  ↓
SchemaCache
  ↓
SemanticCache
  ↓
LLM
  ↓
Generated SELECT
  ↓
SQLSafetyPipeline
  ↓
Validated SELECT
  ↓
run_readonly_query
  ↓
Customer Database
  ↓
Rows
  ↓
InsightGenerator
  ↓
Business-friendly answer
```

The final result exposes the SQL, rows, answer, workflow steps, and any error.

---

## 21. Troubleshooting

### No SQL is generated

Check:

1. `tenant_id`
2. `connection_id`
3. Schema discovery
4. `SchemaCache`
5. Database type in the discovered schema
6. LLM configuration

### `Table '<name>' does not exist`

The SQL referenced a table that is not present in the discovered schema.

Run schema discovery again and verify the table exists.

### `Column '<table>.<column>' does not exist`

The generated SQL referenced a column that is not present in the discovered schema.

Verify the schema and generated SQL.

### Read-only connection rejected

Use a dedicated read-only database user.

Do not use:

```text
root
superuser
db_owner
write-enabled accounts
```

depending on the database engine.

### RAG response looks stubbed

Expected until embeddings/pgvector are wired. `retrieve_docs` still runs and
fills `rag_*` state fields with a contract-shaped stub context.

### No chart suggestion

This is expected in the current version because `ChartRecommender` is currently a stub.

### Schema appears stale

Run schema discovery again. Discovery refreshes the stored schema and updates the Redis schema cache.

---

## 22. Development Guidelines

When modifying the agent:

1. Keep FastAPI routes separate from workflow logic.
2. Keep LLM calls inside agent stages/client code.
3. Keep database access inside customer database tools/providers.
4. Validate generated SQL before execution.
5. Use discovered schema rather than hard-coded table/column names.
6. Keep prompts in `app/agent/prompts.py`.
7. Preserve tenant and connection context throughout the workflow.
8. Use read-only customer database credentials.
9. Add/update pytest tests when changing workflow behavior.
10. Document new workflow nodes or routing changes in this file.

---

## 23. Current Implementation Status

### Implemented

- LangGraph workflow skeleton
- Typed workflow state
- Intent classification
- Versioned prompts
- SQL generation
- Schema/semantic context assembly
- SQL safety pipeline
- Read-only customer database access
- Query execution path
- Insight generation
- Workflow result structure
- SQL/greeting routing
- WebSocket progress event schemas
- Document upload / extract / chunk / embed (`/api/v1/documents`)
- RAG retrieval (pgvector on Postgres; cosine fallback for tests)
- LangGraph RAG / hybrid merge (SQL facts authoritative)
- LangGraph → Redis pub/sub progress hooks
- Authenticated WebSocket chat (`/api/v1/ws/chat`) with Redis progress bridge

### Stub / Future Work

- Chart recommendation beyond rule-based heuristics
- Conversation memory beyond AnalysisState (Postgres source of truth)
- Token-level answer streaming / WebSocket reconnect-resume
- Durable async document workers (sync ingestion is intentional for Phase 5)

---

## 24. Quick Reference

| Component | Location | Responsibility |
|---|---|---|
| Agent entry point | `app/agent/service.py` | Accept question and run workflow |
| Agent types | `app/agent/types.py` | Request/result contracts |
| Progress events | `app/agent/events.py` | WebSocket/Redis event schemas |
| Workflow | `app/agent/workflow.py` | LangGraph orchestration + progress hooks |
| Prompts | `app/agent/prompts.py` | Versioned LLM prompts |
| SQL safety | `app/tools/sql_safety.py` | Validate generated SQL |
| Customer DB | `app/tools/customer_db.py` | Database providers and execution |
| RAG contract | `app/tools/rag.py` | Retrieval request/response + merge rules |
| Schema cache | `app/redis_client.py` | Cached schema/semantic context |
| Progress pub/sub | `app/redis_client.py` | `RedisPublisher` for live updates  |
| LLM client | `app/llm/client.py` | LLM completion |

---

## 25. Week 5 Integration Contracts

### Progress events (T-56)

Typed payloads in `app/agent/events.py`:

- `intent_detected`
- `sql_generated`
- `query_executed`
- `insight_ready`
- `error`

Redis channel: `progress:{tenant_id}:{conversation_id|user_id}`.

LangGraph nodes publish these via `RedisPublisher` after each relevant stage .

### RAG / hybrid merge (T-57)

Workflow state fields:

- `rag_query`, `rag_chunks`, `rag_context` (from `retrieve_docs`)
- `sql`, `rows` (from SQL path)

Merge rules (`merge_sql_and_rag_for_insight`):

- `sql` → SQL + rows only
- `rag` → rag_context only
- `hybrid` → both SQL/rows and rag_context

Retrieval I/O shapes: `RAGRetrieveRequest` / `RAGRetrieveResponse` / `RAGChunk`.

---

## 26. Node Map

```text
START
  |
  v
classify_intent
  |
  +--> greeting ----------------------> generate_insight --> END
  |
  +--> rag -------> retrieve_docs ----> generate_insight --> END
  |
  +--> hybrid ----> generate_sql
  |                    |
  |                    v
  |               execute_query
  |                    |
  |                    v
  |               retrieve_docs
  |                    |
  |                    v
  |              recommend_chart
  |                    |
  |                    v
  |              generate_insight --> END
  |
  +--> sql -------> generate_sql
                       |
                       v
                  execute_query
                       |
                       v
                 recommend_chart
                       |
                       v
                 generate_insight --> END
```

This is the current graph. Update this document whenever the graph routing, nodes, prompts, database capabilities, or execution flow changes.
