"""Active dataset resolution for file-backed connections in BI workflow."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.agent.analysis_state import AnalysisState
from app.agent.follow_up import _match_clarification_choice
from app.dataset_errors import (
    DatasetAccessDeniedError,
    DatasetNotFoundError,
    DatasetNotReadyError,
)
from app.models import Connection, Dataset
from app.services.dataset_service import ensure_dataset_ready, get_tenant_dataset, resolve_active_dataset
from app.source_types import SourceType


def is_file_backed_db_type(db_type: str | None) -> bool:
    return SourceType.is_file_backed(str(db_type or "").lower())


def filter_schema_for_dataset(schema: dict[str, Any], physical_table: str) -> dict[str, Any]:
    """Restrict schema metadata to one dataset table."""
    if not physical_table:
        return schema
    tables = schema.get("tables") or []
    filtered = [
        t
        for t in tables
        if isinstance(t, dict)
        and str(t.get("table_name") or "").split(".")[-1] == physical_table
    ]
    out = dict(schema)
    out["tables"] = filtered
    out["active_dataset_table"] = physical_table
    return out


def _semantic_item_matches_table(item: dict[str, Any], physical_table: str) -> bool:
    expr = str(item.get("expression") or "")
    ref = str(item.get("source_column_ref") or "")
    return physical_table in expr or physical_table in ref or f'"{physical_table}"' in expr


def filter_semantic_for_dataset(
    semantic: dict[str, Any] | None,
    physical_table: str,
) -> dict[str, Any]:
    if not semantic or not physical_table:
        return semantic or {}
    metrics = [
        m
        for m in (semantic.get("metrics") or [])
        if isinstance(m, dict) and _semantic_item_matches_table(m, physical_table)
    ]
    dimensions = [
        d
        for d in (semantic.get("dimensions") or [])
        if isinstance(d, dict) and _semantic_item_matches_table(d, physical_table)
    ]
    return {
        **semantic,
        "metrics": metrics,
        "dimensions": dimensions,
        "relationships": semantic.get("relationships") or [],
    }


def format_dataset_clarification_answer(clarification: dict[str, Any]) -> str:
    message = str(
        clarification.get("message") or "Which dataset should I use?"
    ).strip()
    lines = [message, ""]
    for choice in clarification.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        cid = choice.get("id") or "?"
        label = choice.get("label") or "Dataset"
        lines.append(f"{cid}. {label}")
    return "\n".join(lines).strip()


def format_dataset_clarification(
    *,
    message: str,
    datasets: list[Dataset],
    original_question: str,
) -> dict[str, Any]:
    choices = [
        {
            "id": str(i + 1),
            "label": ds.display_name,
            "dataset_id": str(ds.id),
        }
        for i, ds in enumerate(datasets)
    ]
    return {
        "kind": "pick_dataset",
        "message": message,
        "choices": choices,
        "original_question": original_question,
    }


def resolve_dataset_clarification(
    question: str,
    state: AnalysisState | None,
) -> UUID | None:
    """Resolve a user's answer to pending dataset clarification."""
    if state is None or not state.pending_dataset_clarification:
        return None

    pending = state.pending_dataset_clarification
    choices = pending.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return None

    text = (question or "").strip()
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        ds_id = choice.get("dataset_id")
        if ds_id and str(ds_id).lower() == text.lower():
            try:
                return UUID(str(ds_id))
            except (ValueError, TypeError):
                continue

    chosen_label = _match_clarification_choice(question, choices)
    if not chosen_label:
        return None

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if str(choice.get("label") or "").strip() == chosen_label:
            try:
                return UUID(str(choice["dataset_id"]))
            except (ValueError, TypeError, KeyError):
                return None
    return None


# An explicit instruction only: the question starts with "use X"/"switch to
# X", or names a dataset/file/sheet. A bare "use" anywhere made "what is the
# energy use by plant?" switch to a dataset called "by plant" and wipe the
# conversation's query context.
_DATASET_SWITCH = re.compile(
    r"^(?:please\s+|ok(?:ay)?,?\s+|now\s+)*(?:use|switch\s+to)\s+(?:the\s+)?['\"]?(.+?)['\"]?(?:\.|$|\?)"
    r"|\b(?:use|switch\s+to)\s+(?:the\s+)?(?:dataset|file|sheet)\s+['\"]?(.+?)['\"]?(?:\.|$|\?)",
    re.IGNORECASE,
)


def detect_dataset_switch(question: str) -> str | None:
    match = _DATASET_SWITCH.search((question or "").strip())
    if not match:
        return None
    hint = (match.group(1) or match.group(2) or "").strip()
    return hint or None


def _authorize_dataset(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    dataset_id: UUID | None,
) -> Dataset | None:
    if dataset_id is None:
        return None
    row = get_tenant_dataset(db, dataset_id=dataset_id, tenant_id=tenant_id)
    if row.connection_id != connection_id:
        raise DatasetAccessDeniedError("Dataset does not belong to this connection")
    return row


class FileDatasetContextResult:
    __slots__ = (
        "schema",
        "semantic",
        "dataset",
        "analysis",
        "clarification",
        "effective_question",
        "error",
    )

    def __init__(
        self,
        *,
        schema: dict[str, Any],
        semantic: dict[str, Any],
        dataset: Dataset | None,
        analysis: AnalysisState,
        clarification: dict[str, Any] | None = None,
        effective_question: str | None = None,
        error: str | None = None,
    ) -> None:
        self.schema = schema
        self.semantic = semantic
        self.dataset = dataset
        self.analysis = analysis
        self.clarification = clarification
        self.effective_question = effective_question
        self.error = error


def prepare_file_dataset_context(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    schema: dict[str, Any],
    semantic: dict[str, Any] | None,
    analysis: AnalysisState,
    question: str,
) -> FileDatasetContextResult:
    """Resolve active dataset and filter schema/semantic for file-backed connections."""
    db_type = str(schema.get("db_type") or "")
    if not is_file_backed_db_type(db_type):
        return FileDatasetContextResult(
            schema=schema,
            semantic=semantic or {},
            dataset=None,
            analysis=analysis,
            effective_question=question,
        )

    conn = db.get(Connection, connection_id)
    if conn is None or conn.tenant_id != tenant_id:
        return FileDatasetContextResult(
            schema=schema,
            semantic=semantic or {},
            dataset=None,
            analysis=analysis,
            error="Connection not found",
            effective_question=question,
        )

    working = analysis.model_copy(deep=True)
    effective_question = question

    resolved_id = resolve_dataset_clarification(question, working)
    if resolved_id is not None:
        pending = working.pending_dataset_clarification or {}
        effective_question = str(pending.get("original_question") or question)
        working.dataset_id = resolved_id
        working.pending_dataset_clarification = None
        working.query_plan = working.query_plan  # keep plan for clarification resume

    switch_hint = detect_dataset_switch(question)
    dataset_hint: str | None = switch_hint
    if switch_hint:
        working.dataset_id = None
        working.query_plan = None
        working.last_sql = None
        working.pending_query_clarification = None

    try:
        if working.dataset_id is not None:
            dataset = _authorize_dataset(
                db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                dataset_id=working.dataset_id,
            )
            if dataset is not None:
                ensure_dataset_ready(dataset)
                filtered_schema = filter_schema_for_dataset(schema, dataset.physical_table)
                filtered_semantic = filter_semantic_for_dataset(semantic, dataset.physical_table)
                working.dataset_display_name = dataset.display_name
                return FileDatasetContextResult(
                    schema=filtered_schema,
                    semantic=filtered_semantic,
                    dataset=dataset,
                    analysis=working,
                    effective_question=effective_question,
                )

        active, clarify_msg = resolve_active_dataset(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            dataset_id=None,
            dataset_hint=dataset_hint,
        )
        if active is not None:
            ensure_dataset_ready(active)
            working.dataset_id = active.id
            working.dataset_display_name = active.display_name
            working.pending_dataset_clarification = None
            filtered_schema = filter_schema_for_dataset(schema, active.physical_table)
            filtered_semantic = filter_semantic_for_dataset(semantic, active.physical_table)
            return FileDatasetContextResult(
                schema=filtered_schema,
                semantic=filtered_semantic,
                dataset=active,
                analysis=working,
                effective_question=effective_question,
            )

        if clarify_msg:
            from app.services.dataset_service import list_datasets_for_tenant

            choices_src = [
                d
                for d in list_datasets_for_tenant(
                    db, tenant_id=tenant_id, connection_id=connection_id
                )
                if d.status == "ready"
            ]
            clarification = format_dataset_clarification(
                message=clarify_msg,
                datasets=choices_src,
                original_question=effective_question,
            )
            working.pending_dataset_clarification = clarification
            return FileDatasetContextResult(
                schema=schema,
                semantic=semantic or {},
                dataset=None,
                analysis=working,
                clarification=clarification,
                effective_question=effective_question,
            )

        return FileDatasetContextResult(
            schema=schema,
            semantic=semantic or {},
            dataset=None,
            analysis=working,
            error="No ready dataset found on this connection",
            effective_question=effective_question,
        )
    except DatasetNotReadyError as exc:
        return FileDatasetContextResult(
            schema=schema,
            semantic=semantic or {},
            dataset=None,
            analysis=working,
            error=str(exc),
            effective_question=effective_question,
        )
    except (DatasetAccessDeniedError, DatasetNotFoundError) as exc:
        working.dataset_id = None
        return FileDatasetContextResult(
            schema=schema,
            semantic=semantic or {},
            dataset=None,
            analysis=working,
            error=str(exc),
            effective_question=effective_question,
        )
