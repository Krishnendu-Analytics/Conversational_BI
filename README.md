# Conversational AI — Conversational BI Platform

An AI-powered conversational business intelligence platform that lets users ask questions about connected business databases in natural language. The backend orchestrates the workflow, validates generated SQL, executes read-only queries, and returns results and explanations to the client.

## Core capabilities

- FastAPI backend exposing REST and WebSocket APIs
- Natural-language-to-SQL workflow orchestrated with LangGraph
- SQL validation and read-only query safeguards
- Multi-tenant application structure and role-based access controls
- Database connectivity for PostgreSQL, MySQL, and Microsoft SQL Server
- Authentication and OTP-based verification
- Encrypted storage of connected-database credentials
- Redis-backed conversation context, caching, and real-time messaging
- Document question answering using retrieval-augmented generation (RAG)
- React/Vite frontend
- Docker Compose development environment

## Technology stack

**Backend:** Python, FastAPI, LangGraph, SQLAlchemy, Alembic, PostgreSQL, Redis  
**Frontend:** React, TypeScript, Vite  
**Infrastructure:** Docker, Docker Compose  
**AI / data:** LLM API integration, SQLGlot, pgvector, embeddings

## Repository layout

```text
Conversational_AI/
├── backend/             # FastAPI application, migrations, tests
├── frontend/            # React + TypeScript client
├── docker/              # Supporting Docker assets
├── docker-compose.yml   # Local development services
├── .gitignore
└── README.md
```

## Prerequisites

- Docker Desktop with Docker Compose
- Git
- An LLM provider API key for live LLM features

## Run locally

1. Clone this repository.
2. Create your local backend environment file from the safe template:

   ```bash
   cp backend/.env.example backend/.env
   ```

   On Windows Command Prompt:

   ```cmd
   copy backend\.env.example backend\.env
   ```

3. Edit `backend/.env` and supply your own local credentials and configuration. Never commit this file.
4. Start the services:

   ```bash
   docker compose up --build
   ```

5. Open:
   - Frontend: http://localhost:3000
   - Backend API: http://localhost:8000
   - API documentation: http://localhost:8000/docs

To stop the services:

```bash
docker compose down
```

To stop and remove local database volumes as well (this deletes local persisted data):

```bash
docker compose down -v
```

## Configuration and secrets

`backend/.env.example` documents the environment variables required by the backend. Copy it to `backend/.env` and replace placeholders with local values.

**Never commit `.env` files, API keys, passwords, signing keys, encryption keys, tokens, or production connection strings.** Rotate any credential that has been exposed publicly.

The Compose file contains development-only service defaults. Do not use those defaults in production. Configure unique, strong secrets and production-grade infrastructure before deployment.

## Security notes

- Treat this repository as public: do not add customer data, production database dumps, private certificates, access tokens, or confidential company information.
- Review authorization, tenant isolation, SQL safety, CORS, rate limits, logging, and secret management before production deployment.
- Use a managed secret store or deployment environment variables for production secrets.

## Team / contribution

This project was developed collaboratively. Review the source history and project documentation for implementation details and team ownership before redistributing or presenting the code publicly.

## License

No license is included. Unless a license is added by the copyright owner, standard copyright restrictions apply; public visibility alone does not grant permission to reuse or redistribute the code.
