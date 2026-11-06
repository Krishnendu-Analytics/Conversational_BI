"""Supervisor agent: an LLM that orchestrates specialist agents.

Instead of classifying the question and following one fixed path (with
keyword rules overruling the model), the supervisor sees the question, the
conversation, the database's tables and the user's candidate files, and
*calls* specialists as tools — one, several, or one after another when the
first comes back empty:

- query_database      → data agent (SQL planning, validation, read-only run),
                        on whichever of the tenant's databases fits
- search_documents    → document agent (retrieval over chosen files)
- search_chat_history → history agent (the user's own past questions/memory)
- describe_schema     → schema agent
- build_dashboard / edit_dashboard → dashboard agents
- investigate         → investigator (multi-domain root-cause loop)

Each specialist is the existing LangGraph pipeline run with a forced intent,
so all of its safety checks stay deterministic. The supervisor only decides
*what* to do; the specialists decide *how*.

Latency: the planning call replaces the intent-classifier call, and a single
successful specialist answer is returned as is (no second LLM call).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

MAX_STEPS = 4
MAX_OBSERVATION_CHARS = 1800
MAX_TABLES_LISTED = 40
MAX_TABLES_PER_DATABASE = 25
MAX_SUPERVISOR_FILES = 60  # every ready file is listed (names only), up to this many


@dataclass
class DatabaseInfo:
    id: str
    name: str
    db_type: str = ""
    tables: list[str] = field(default_factory=list)
    selected: bool = False  # the connection the user has selected in the chat


@dataclass
class FileInfo:
    id: str
    filename: str
    snippet: str = ""
    in_chat: bool = False
    attached_now: bool = False
    uploaded_at: str = ""  # ISO date


@dataclass
class SupervisorResult:
    answer: str | None  # the supervisor's own final text, when it wrote one
    runs: list[dict[str, Any]] = field(default_factory=list)  # specialist final states, in order
    trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def primary(self) -> dict[str, Any] | None:
        """The run whose rows/dashboard the UI should show: the last one that
        produced a dashboard or rows, else the last run."""
        for run in reversed(self.runs):
            if run.get("dashboard") or run.get("rows"):
                return run
        return self.runs[-1] if self.runs else None


# ----- tools -----


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_QUESTION = {
    "type": "string",
    "description": (
        "When one question needs several agents: this agent's part of it, in the user's "
        "words, self-contained. Only the business question: never add instructions or ask "
        "for SQL, table names, sources, notes or other details. (With a single agent the "
        "user's own question is used.)"
    ),
}
_FILES = {
    "type": "array",
    "items": {"type": "string"},
    "description": "File labels to use, e.g. [\"F1\"]. Empty = every listed file.",
}


def build_tools(
    *, source: str, has_files: bool, has_dashboard: bool, database_count: int = 1
) -> list[dict[str, Any]]:
    """Tools allowed for this turn. ``source`` is the user's @ choice:
    documents-only hides the databases, database-only hides the files. With
    several databases, the database tools take a ``database`` label."""
    tools: list[dict[str, Any]] = []
    use_db = source != "documents" and database_count > 0
    use_files = source != "database" and has_files
    db_param: dict[str, Any] = {}
    if database_count > 1:
        db_param = {
            "database": {
                "type": "string",
                "enum": [f"DB{i}" for i in range(1, database_count + 1)],
                "description": "Which database to use (see the list). Default: the currently selected one.",
            }
        }
    if use_db:
        tools.append(_tool(
            "query_database",
            "Data agent: answers questions about business data stored in a database "
            "(totals, trends, rankings, comparisons, records). Returns the answer and result rows.",
            {"question": _QUESTION, **db_param}, ["question"],
        ))
        tools.append(_tool(
            "describe_schema",
            "Schema agent: which tables/columns exist, how tables relate, database structure.",
            {"question": _QUESTION, **db_param}, ["question"],
        ))
        tools.append(_tool(
            "investigate",
            "Investigator: explains WHY a metric changed or finds root causes across several "
            "related business areas (tables) of the database. Slower; use only for why/root-cause questions.",
            {"question": _QUESTION, **db_param}, ["question"],
        ))
    if use_files:
        tools.append(_tool(
            "search_documents",
            "Document agent: answers from the user's uploaded files (invoices, receipts, bills, "
            "policies, contracts, reports, spreadsheets...).",
            {"question": _QUESTION, "files": _FILES}, ["question"],
        ))
    tools.append(_tool(
        "search_chat_history",
        "History agent: the user's OWN past questions and chats with you, and what you remember "
        "about them (e.g. 'which question did I ask most', 'what did we discuss yesterday').",
        {"question": _QUESTION}, ["question"],
    ))
    if use_db or use_files:
        dash_props: dict[str, Any] = {"request": {"type": "string", "description": "What the dashboard should cover."}}
        if use_db and use_files:
            dash_props["source"] = {"type": "string", "enum": ["database", "documents"]}
        if use_files:
            dash_props["files"] = _FILES
        if use_db:
            dash_props.update(db_param)
            dash_props["coverage"] = {
                "type": "string",
                "enum": ["focused", "whole_database"],
                "description": (
                    "whole_database: a complete overview surveying every business area of the "
                    "database (the user wants everything, a full/complete/overall dashboard). "
                    "focused: one topic or area. Default focused."
                ),
            }
        tools.append(_tool(
            "build_dashboard",
            "Dashboard agent: builds a multi-chart overview dashboard from the database or from files.",
            dash_props, ["request"],
        ))
    if has_dashboard and use_db:
        tools.append(_tool(
            "edit_dashboard",
            "Changes the dashboard already open in this chat (add/remove/rename charts, filters).",
            {"request": {"type": "string"}}, ["request"],
        ))
    return tools


# Fixed guidelines first, per-turn sources/context last: providers cache a
# repeated prompt prefix, so everything above {databases} is billed at the
# cached rate after the first question.
_SYSTEM = """You are the supervisor of a business-intelligence assistant. You
answer the user by calling specialist agents (tools) and combining what they
return. Think about what the user actually wants, then call the right agent(s).

Guidelines:
- Pick agents by meaning, not keywords. A question about a specific receipt,
  invoice, policy or anything that only a file contains → search_documents.
  Business data the tables hold → query_database. The user's own past
  questions/chats → search_chat_history.
- With several databases, choose the one whose tables actually hold the data
  asked about, and pass it as "database". If the user names a database, use
  it. If two fit equally, prefer the currently selected one. (The app shows
  which source answered; when you write a combined answer, name sources by
  their plain names as listed below, never table names.)
- Usually one agent is enough. Call several (in one step) only when the
  question needs several sources, e.g. comparing a file's figures with data.
- If an agent returns nothing useful, try the next most likely agent before
  giving up.
- Greetings, thanks, or questions about what you can do: answer directly
  without tools.
- Questions about the files themselves (how many, which ones, when uploaded):
  answer directly from the Files list below, counting duplicates of the same
  name as separate uploads.
- Never answer a factual or topic question ("what is X", "explain Y") from
  your own general knowledge. If a listed file could cover the topic, call
  search_documents; if no file or database has it, say that the user's data
  doesn't cover it.
- Write the final answer yourself only when you combined several agents or
  none was needed: concise, business language, keep the agents' numbers
  exactly, never show SQL, and say which source each figure came from.

Available sources:
- Databases:
{databases}
- Files:
{files}

What you know about this conversation and user:
{context}"""


def _files_block(files: list[FileInfo], total: int = 0) -> str:
    if not files:
        return "  (none)"
    total = max(total, len(files))
    lines = [f"  {total} file(s) uploaded in total" + (f", newest {len(files)} listed:" if total > len(files) else ":")]
    for i, f in enumerate(files, start=1):
        tags = [t for t, on in (("attached to this message", f.attached_now), ("in this chat", f.in_chat)) if on]
        if f.uploaded_at:
            tags.append(f"uploaded {f.uploaded_at}")
        tag = f" ({', '.join(tags)})" if tags else ""
        lines.append(f"  [F{i}] {f.filename}{tag}")
        if f.snippet:
            lines.append(f"       excerpt: {f.snippet}")
    return "\n".join(lines)


def _tables_text(table_names: list[str], limit: int = MAX_TABLES_LISTED) -> str:
    if not table_names:
        return "(no tables discovered)"
    text = ", ".join(table_names[:limit])
    if len(table_names) > limit:
        text += f" and {len(table_names) - limit} more"
    return text


def _databases_block(databases: list[DatabaseInfo]) -> str:
    if not databases:
        return "  (none)"
    limit = MAX_TABLES_LISTED if len(databases) == 1 else MAX_TABLES_PER_DATABASE
    lines = []
    for i, d in enumerate(databases, start=1):
        tags = ", ".join(t for t in (d.db_type, "currently selected" if d.selected else "") if t)
        lines.append(f"  [DB{i}] {d.name}" + (f" ({tags})" if tags else ""))
        lines.append(f"       tables: {_tables_text(d.tables, limit)}")
    return "\n".join(lines)


# ----- running specialists -----

SpecialistRunner = Callable[[str, str, dict[str, Any]], dict[str, Any]]
"""(intent, question, overrides) -> the specialist's final state."""


def _resolve_files(labels: Any, files: list[FileInfo]) -> list[str]:
    ids: list[str] = []
    for label in labels if isinstance(labels, list) else []:
        text = str(label).strip().upper().lstrip("[").rstrip("]").lstrip("F")
        if text.isdigit() and 1 <= int(text) <= len(files):
            fid = files[int(text) - 1].id
            if fid not in ids:
                ids.append(fid)
    return ids or [f.id for f in files]


def _resolve_database(label: Any, databases: list[DatabaseInfo]) -> str | None:
    """Connection id for a "DBn" label; None = the selected connection."""
    text = str(label or "").strip().upper().lstrip("[").rstrip("]")
    if text.startswith("DB"):
        text = text[2:]
    if text.isdigit() and 1 <= int(text) <= len(databases):
        return databases[int(text) - 1].id
    return None


def _call_specialist(
    name: str,
    args: dict[str, Any],
    question: str,
    files: list[FileInfo],
    databases: list[DatabaseInfo],
    run: SpecialistRunner,
) -> dict[str, Any]:
    q = str(args.get("question") or args.get("request") or question).strip() or question
    on_db: dict[str, Any] = {"document_ids": []}
    connection_id = _resolve_database(args.get("database"), databases)
    if connection_id:
        on_db["connection_id"] = connection_id
    if name == "query_database":
        return run("sql", q, on_db)
    if name == "describe_schema":
        return run("schema", q, on_db)
    if name == "investigate":
        return run("investigate", q, on_db)
    if name == "search_chat_history":
        return run("history", q, {})
    if name == "search_documents":
        return run("rag", q, {"document_ids": _resolve_files(args.get("files"), files)})
    if name == "build_dashboard":
        from_files = args.get("source") == "documents" or (
            "source" not in args and "files" in args
        )
        ids = _resolve_files(args.get("files"), files) if from_files and files else []
        overrides: dict[str, Any] = {"document_ids": ids}
        if not ids and connection_id:
            overrides["connection_id"] = connection_id
        if not ids and args.get("coverage") == "whole_database":
            overrides["dashboard_scope"] = "whole_database"
        return run("dashboard", q, overrides)
    if name == "edit_dashboard":
        return run("edit_dashboard", q, {"document_ids": []})
    raise ValueError(f"unknown tool {name}")


def _is_complete(name: str, final: dict[str, Any]) -> bool:
    """Good enough to hand straight to the user (no second LLM call)."""
    if final.get("error"):
        return False
    if name == "search_documents" and (final.get("rag_insufficient") or not final.get("rag_chunks")):
        return False
    clarification = final.get("query_clarification")
    if isinstance(clarification, dict) and clarification.get("kind") == "invalid_plan" and not clarification.get("choices"):
        return False  # "the schema doesn't contain that" — another agent may know
    return bool(str(final.get("answer") or "").strip())


def _observation(final: dict[str, Any]) -> str:
    rows = final.get("rows") or []
    obs: dict[str, Any] = {"answer": str(final.get("answer") or "")[:MAX_OBSERVATION_CHARS]}
    if rows:
        obs["row_count"] = len(rows)
        obs["sample_rows"] = rows[:5]
    if final.get("error"):
        obs["error"] = str(final["error"])[:300]
    if final.get("rag_insufficient"):
        obs["note"] = "no relevant passages found in the files"
    if isinstance(final.get("dashboard"), dict):
        obs["dashboard"] = str(final["dashboard"].get("title") or "built")
    return json.dumps(obs, default=str)[: MAX_OBSERVATION_CHARS + 600]


def run_supervisor(
    question: str,
    *,
    llm: LLMClient,
    run_specialist: SpecialistRunner,
    databases: list[DatabaseInfo],
    files: list[FileInfo],
    context: str,
    source: str,
    has_dashboard: bool,
    total_files: int = 0,
) -> SupervisorResult:
    tools = build_tools(
        source=source,
        has_files=bool(files),
        has_dashboard=has_dashboard,
        database_count=len(databases),
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": _SYSTEM.format(
                databases=_databases_block(databases), files=_files_block(files, total_files), context=context or "None."
            ),
        },
        {"role": "user", "content": question},
    ]
    result = SupervisorResult(answer=None)

    for step in range(MAX_STEPS):
        # Last step: no more tools, the supervisor must answer.
        msg = llm.chat(messages, tools=tools if step < MAX_STEPS - 1 else None, max_output_tokens=1500)
        calls = list(getattr(msg, "tool_calls", None) or [])
        if not calls:
            result.answer = (msg.content or "").strip() or None
            return result

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                    for c in calls
                ],
            }
        )
        step_runs: list[tuple[str, dict[str, Any]]] = []
        for call in calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if len(calls) == 1:
                # One agent answers the whole question: give it the user's own
                # words. Rewrites added asks ("mention if based on completed
                # transactions", "include close contenders") that the SQL
                # writer turned into odd literal columns or a failed plan.
                # Specialists resolve follow-ups from the conversation already.
                args = {k: v for k, v in args.items() if k not in {"question", "request"}}
            try:
                final = _call_specialist(name, args, question, files, databases, run_specialist)
            except Exception as exc:  # noqa: BLE001 — report to the supervisor, let it recover
                logger.warning("specialist_failed tool=%s", name, exc_info=True)
                final = {"answer": "", "error": f"{type(exc).__name__}"}
            result.runs.append(final)
            result.trace.append({"step": step, "tool": name, "args": args, "ok": _is_complete(name, final)})
            step_runs.append((name, final))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": _observation(final)})

        # One agent, first try, good answer: that *is* the answer.
        if step == 0 and len(step_runs) == 1 and _is_complete(*step_runs[0]):
            return result

    return result
