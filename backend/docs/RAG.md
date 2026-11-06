# Document RAG (Original Phase 5)

## Architecture

```
Upload (txt/md/csv/json/pdf)
  → tenant-scoped storage (DOCUMENT_STORAGE_ROOT)
  → Document row (status: processing)
  → extract text
  → deterministic chunking
  → EmbeddingProvider (fake | openai)
  → DocumentChunk + embedding vector
  → status: ready | failed

Ask (RAG / hybrid intent)
  → query embedding
  → tenant-scoped similarity search (pgvector on Postgres; cosine in-process on SQLite tests)
  → bounded context → LangGraph retrieve_docs → grounded answer
```

SQL facts remain authoritative on hybrid paths. Documents provide qualitative context only —
except when the user **attaches** files to a message: that message always searches the
attached files (a data question runs as `hybrid`, anything else as `rag`), and the answer
comes from the documents first, with the database result only as a labelled fallback
(prompt `attached_document_answer`).

## Supported formats

| Format | Extensions |
|--------|------------|
| Plain text | `.txt`, `.md`, `.csv`, `.tsv`, `.json` |
| PDF | `.pdf` — text via pypdf; scanned pages (no text layer) are read by the vision model |
| Excel | `.xlsx`, `.xlsm` (openpyxl, cached values — macros never run), `.xls` (xlrd) |
| Word | `.docx` (document XML: paragraphs + tables), `.doc` (best effort via the piece table, olefile) |
| Images | `.png`, `.jpg`, `.jpeg`, `.webp` — transcribed (text, tables, chart values) by the configured vision-capable LLM |

Images and scanned PDFs need `LLM_API_KEY` and a vision-capable `LLM_MODEL`; without one
the upload fails with a clear error rather than storing a stub.

Unsupported types return `400` with a clear error.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOCUMENT_STORAGE_ROOT` | `./data/documents` | Tenant-scoped file storage |
| `DOCUMENT_MAX_SIZE_BYTES` | 10 MiB | Upload size cap |
| `DOCUMENT_CHUNK_SIZE` | 800 | Characters per chunk |
| `DOCUMENT_CHUNK_OVERLAP` | 120 | Overlap between chunks |
| `DOCUMENT_MAX_CHUNKS` | 500 | Cap chunks per document |
| `EMBEDDING_PROVIDER` | `fake` | `fake` (dev/tests, matches shared words only) or `openai` (matches meaning). **Production (`APP_ENV=production`) rejects `fake`/missing.** In Docker Compose set it in the repo-root `.env` — `docker-compose.yml` overrides `backend/.env` for this variable. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI-compatible model |
| `EMBEDDING_DIMENSION` | `384` | Vector size (must match DB column) |
| `EMBEDDING_API_KEY` | (falls back to `LLM_API_KEY`) | Embedding API key |
| `RAG_TOP_K` | 5 | Max chunks retrieved |
| `RAG_MIN_SIMILARITY` | 0.15 | Cosine similarity floor |

Changing `EMBEDDING_DIMENSION` requires a new Alembic migration for the `vector(n)` column.

When `APP_ENV` is `production` / `prod`, the API refuses to start (and
`get_embedding_provider()` refuses to resolve) if `EMBEDDING_PROVIDER` is
missing or set to `fake`/`hash`/`test`. There is no silent fallback from
OpenAI embedding failures to the fake provider.

## API

- `POST /api/v1/documents/upload` — multipart `file` (admin/analyst)
- `GET /api/v1/documents` — list tenant documents
- `GET /api/v1/documents/{id}` — get document
- `DELETE /api/v1/documents/{id}` — delete document + chunks + storage

Tenant is always taken from the JWT. Client `tenant_id` is never trusted.

## Docker

Platform Postgres uses `pgvector/pgvector:pg16`. Migration `0014_rag_documents`
enables the `vector` extension and adds document metadata + embedding columns.
Uploaded files are kept on the `document_data` volume (`/app/data/documents`).

## WebSocket

Existing `/api/v1/ws/chat` accepts optional `document_ids` on ask messages to
scope retrieval. Progress events and tenant room isolation are unchanged.

## Ingestion model

Document ingestion is **synchronous** inside the upload request (same product
choice as CSV/Excel for this phase). No Celery/Arq workers.
