"""Orchestrate atomic conversational dashboard edits."""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

from app.agent.dashboard import DashboardEditExecutionError, execute_dashboard_edit
from app.agent.dashboard_follow_up import resolve_pending_dashboard_op
from app.agent.dashboard_operation_applier import apply_resolved_operations
from app.agent.dashboard_operation_planner import plan_dashboard_operations
from app.agent.dashboard_operation_schema import (
    ClarificationRequest,
    DashboardEditResult,
    DashboardOperationPlan,
    PendingDashboardOp,
)
from app.agent.dashboard_operation_validator import validate_operation_plan
from app.agent.dashboard_widget_catalog import build_widget_catalog


def run_dashboard_edit_pipeline(
    *,
    instruction: str,
    dashboard: dict[str, Any],
    dashboard_id: UUID,
    semantic_catalog: dict[str, Any] | None,
    schema: dict[str, Any],
    db_type: str,
    connection_id: str,
    tenant_id: UUID,
    llm: Any,
    active_widget_id: str | None = None,
    pending_operation: PendingDashboardOp | None = None,
) -> DashboardEditResult:
    """Validate, apply, execute, and return result without persisting."""
    baseline = copy.deepcopy(dashboard)
    catalog = build_widget_catalog(baseline, semantic_catalog)

    plan: DashboardOperationPlan | None = None
    if pending_operation is not None:
        plan = resolve_pending_dashboard_op(
            instruction,
            pending_operation,
            catalog,
            dashboard_id=dashboard_id,
        )
        if plan is None:
            return DashboardEditResult(
                success=False,
                clarification=ClarificationRequest(
                    kind="pick_widget",
                    message="I still need clarification to complete the dashboard edit.",
                    choices=pending_operation.choices,
                ),
                pending_operation=pending_operation,
            )
    else:
        plan = plan_dashboard_operations(
            llm,
            instruction,
            dashboard_id=dashboard_id,
            dashboard=baseline,
            catalog=catalog,
            semantic_catalog=semantic_catalog,
        )

    if plan is None:
        return DashboardEditResult(
            success=False,
            error="I couldn't understand that dashboard edit request.",
        )

    if plan.clarification:
        pending = (
            PendingDashboardOp(
                plan=plan,
                choices=plan.clarification.choices,
                pending_filter_field=(
                    plan.operations[0].filter.field
                    if plan.operations
                    and plan.operations[0].filter
                    else None
                ),
            )
            if plan.clarification.resumable
            else None
        )
        return DashboardEditResult(
            success=False,
            clarification=plan.clarification,
            pending_operation=pending,
        )

    validation = validate_operation_plan(
        plan,
        loaded_dashboard_id=dashboard_id,
        catalog=catalog,
        semantic_catalog=semantic_catalog,
        active_widget_id=active_widget_id,
    )

    if not validation.ok:
        if validation.clarification:
            pending = (
                PendingDashboardOp(
                    plan=plan,
                    unresolved_index=validation.unresolved_index,
                    choices=validation.clarification.choices,
                    pending_filter_field=(
                        plan.operations[validation.unresolved_index].filter.field
                        if validation.unresolved_index < len(plan.operations)
                        and plan.operations[validation.unresolved_index].filter
                        else None
                    ),
                )
                if validation.clarification.resumable
                else None
            )
            return DashboardEditResult(
                success=False,
                clarification=validation.clarification,
                pending_operation=pending,
            )
        return DashboardEditResult(
            success=False,
            error="; ".join(validation.errors) or "Dashboard edit validation failed.",
        )

    try:
        working, notes, modified_ids = apply_resolved_operations(
            baseline,
            validation.resolved,
            semantic_catalog=semantic_catalog or {},
            schema=schema,
            db_type=db_type,
        )
        executed = execute_dashboard_edit(
            working,
            connection_id=connection_id,
            schema=schema,
            db_type=db_type,
            tenant_id=tenant_id,
            semantic=semantic_catalog,
            modified_widget_ids=modified_ids,
        )
        executed["id"] = str(dashboard_id)
        return DashboardEditResult(
            success=True,
            dashboard=executed,
            edit_notes=notes,
            modified_widget_ids=sorted(modified_ids),
        )
    except DashboardEditExecutionError as exc:
        return DashboardEditResult(
            success=False,
            error=str(exc),
        )
    except ValueError as exc:
        return DashboardEditResult(
            success=False,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return DashboardEditResult(
            success=False,
            error=f"Dashboard edit failed: {exc}",
        )
