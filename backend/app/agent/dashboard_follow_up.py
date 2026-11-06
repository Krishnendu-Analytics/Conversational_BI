"""Resolve pending dashboard operation clarifications."""

from __future__ import annotations

import re
from uuid import UUID

from app.agent.dashboard_operation_schema import (
    DashboardOperationItem,
    DashboardOperationPlan,
    FilterSpec,
    PendingDashboardOp,
    WidgetCatalogEntry,
    WidgetRef,
)


def resolve_pending_dashboard_op(
    instruction: str,
    pending: PendingDashboardOp,
    catalog: list[WidgetCatalogEntry],
    *,
    dashboard_id: UUID,
) -> DashboardOperationPlan | None:
    """Complete a pending plan from a follow-up utterance."""
    text = (instruction or "").strip()
    lowered = text.lower()
    plan = pending.plan.model_copy(deep=True)

    if pending.pending_filter_field and pending.choices == []:
        field = pending.pending_filter_field
        value = text.strip().strip("\"'")
        if not value:
            return None
        for item in plan.operations:
            if item.action == "add_filter" and item.filter:
                item.filter = FilterSpec(
                    field=field,
                    operator=item.filter.operator,
                    value=value,
                )
        return plan

    if pending.choices:
        chosen_widget_id: str | None = None
        num_match = re.match(r"^(\d+)$", text.strip())
        if num_match:
            idx = int(num_match.group(1)) - 1
            if 0 <= idx < len(pending.choices):
                chosen_widget_id = pending.choices[idx].widget_id
        if not chosen_widget_id:
            for choice in pending.choices:
                if choice.label.lower() in lowered or (
                    choice.widget_id and choice.widget_id.lower() in lowered
                ):
                    chosen_widget_id = choice.widget_id
                    break

        if not chosen_widget_id:
            return None

        op_idx = pending.unresolved_index
        if op_idx < len(plan.operations):
            item = plan.operations[op_idx]
            if item.target is None:
                item.target = WidgetRef(widget_id=chosen_widget_id)
            else:
                item.target.widget_id = chosen_widget_id
        return plan

    return None
