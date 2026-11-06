# Conversational BI SaaS

Ask your database questions in plain English and get back real answers.

That's the whole idea. Instead of teaching business users SQL or making them wait on the data team for every little report, they just type something like *"show me monthly revenue for last quarter"* and the backend figures out the SQL, runs it safely, and explains the result.

This repo is the backend only. It's a headless FastAPI service, so there's no UI here. You talk to it over REST and WebSocket. That was a deliberate call because it lets a web app, a mobile app, or even a Slack bot sit on top of the same API later.

## Team

- Ali - Lead Backend Architect
- Prerna - Backend Dev
- Krishnendu - Backend Dev

Timeline is 8 weeks: 6 weeks building, 1 week testing, 1 week fixing whatever we broke.

## What it actually does

- Sign up, verify your email with an OTP (Twilio), log in and get a JWT.
- Connect your own database. We encrypt the credentials with Fernet before they ever touch disk.
- Works with PostgreSQL, MySQL, and SQL Server, so a tenant isn't forced onto one engine.
- Turns a natural language question into a PostgreSQL `SELECT`, runs it through safety checks, executes it read-only, and returns the rows plus a short human explanation.
- Streams progress live over WebSocket, so the user sees "understanding question... writing SQL... running query..." instead of staring at a spinner.
- Remembers the last few things you asked (in Redis) so follow-ups like *"only for Delhi"* work without repeating yourself.
- Can also answer from uploaded documents using vector search (RAG with pgvector).
  See `docs/RAG.md` for formats, config, and the upload → retrieve flow.

## Why these choices

**FastAPI** because it's async by default, fast, and the Pydantic validation saves us from a lot of manual input checking. It also plays nicely with the Python AI libraries we need.

**LangGraph** because the AI part isn't a single call. It's a chain of steps (figure out intent, load the schema, write SQL, validate it, run it, explain it). LangGraph lets us model that as a state machine instead of a giant tangled function.

**Redis** does a lot of heavy lifting: short-term conversation memory, the pub/sub channels that feed the WebSocket, schema caching, locks, and rate limiting.

**Multi-tenant from day one.** Every table carries a `tenant_id`, every query filters on it, and even the Redis keys are namespaced per tenant. One customer should never be able to see another customer's data, so we don't rely on remembering to add a filter, it's baked into the repository layer.

## How a query flows

1. Client opens a WebSocket and sends a question.
2. We classify what kind of question it is (SQL, document lookup, both, or just a greeting).
3. We load that tenant's schema and semantic definitions (e.g. "Revenue" means `SUM(orders.ord_amt)`).
4. The model writes a SQL query.
5. The query goes through 4 safety gates before it's allowed to run:
   - Is the syntax valid? (parsed with SQLGlot)
   - Is it a read-only `SELECT`? (no INSERT/UPDATE/DELETE/DROP/etc.)
   - Do the tables and columns actually exist?
   - Add a 15s timeout and a `LIMIT 10000` so nothing runs away.
6. Run it against the customer DB, get the rows, generate a chart suggestion and a plain-English insight.
7. Every step is pushed back to the client as it happens.

## Project layout

```
app/
  main.py, config.py, database.py, models.py, ...
  routes/       # HTTP + WebSocket (API)
  agent/        # LangGraph — call service.run_question from routes
  tools/        # SQL safety, customer DB, RAG
  llm/          # talk to the model
tests/
```

Simple rule: **routes → agent → tools/llm**. That way you can split Docker services later without rewriting everything.

## Running it locally

Everything runs through Docker Compose. You get four containers:

- `fastapi-backend` - the app itself, on port 8000
- `platform-postgres` - our metadata DB (users, tenants, connections, embeddings), port 5432
- `redis-cluster` - Redis, port 6379
- `customer-db-mock` - a fake customer database to test queries against

```bash
cp .env.example .env      # then fill in the secrets
docker compose up --build
```

Once it's up, the API docs are at http://localhost:8000/docs.

You'll need to set these in `.env`:

- `FERNET_SECRET_KEY` - the master key for encrypting connection passwords
- `JWT_SECRET_KEY` - for signing tokens
- Twilio credentials - for sending OTP emails
- an LLM API key - for the SQL generation

## Main endpoints

| Method | Path | What it does |
|--------|------|--------------|
| POST | `/api/v1/auth/signup` | register, hash password, send OTP |
| POST | `/api/v1/auth/verify-otp` | verify OTP, return JWT |
| POST | `/api/v1/auth/login` | log in |
| POST | `/api/v1/connections` | save an encrypted DB connection |
| POST | `/api/v1/connections/{id}/discover` | scan the connected DB's schema |
| POST | `/api/v1/documents/upload` | upload a document for RAG (txt/md/csv/json/pdf) |
| GET | `/api/v1/documents` | list tenant documents |
| WS | `/api/v1/ws/chat` | the live chat channel |

## A note on the code

Right now most files are class stubs with a `TODO` and a `pass`. That's intentional. The structure and the class names are laid out so each of us can pick a piece and fill it in without stepping on each other. If you're implementing something, the docstring on each class tells you what it's supposed to do.
