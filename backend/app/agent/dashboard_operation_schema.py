"""Structured dashboard edit operation schema (v2)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.agent.query_plan import FilterSpec


class WidgetPosition(BaseModel):
    x: int = 0
    y: int = 0
    w: int = 6
    h: int = 3


class WidgetRef(BaseModel):
    widget_id: str | None = None
    title: str | None = None
    metric: str | None = None
    dimension: str | None = None
    widget_type: Literal["kpi", "chart", "table", "pie", "ai_insight", "text"] | None = None
    chart: Literal["bar", "line", "pie"] | None = None
    role: Literal["trend", "breakdown", "kpi", "insight"] | None = None
    # 1-based position among matching widgets (in catalog/display order);
    # negative counts from the end (-1 = last). Set for phrases like
    # "the second chart" or "the last kpi" so a single widget is targeted
    # instead of every widget of that type.
    ordinal: int | None = None


class WidgetAddSpec(BaseModel):
    metric: str | None = None
    dimension: str | None = None
    widget_type: Literal["kpi", "chart", "table", "pie", "ai_insight", "text"] = "kpi"
    chart: Literal["bar", "line", "pie"] | None = None
    title: str | None = None
    role: Literal["trend", "breakdown", "kpi", "insight"] | None = None


class WidgetPatch(BaseModel):
    title: str | None = None
    chart: Literal["bar", "line", "pie"] | None = None
    widget_type: Literal["kpi", "chart", "table", "pie", "ai_insight", "text"] | None = None
    metric: str | None = None
    dimension: str | None = None


DashboardAction = Literal[
    "add_widget",
    "remove_widget",
    "update_widget",
    "move_widget",
    "rename_dashboard",
    "rename_widget",
    "add_filter",
    "remove_filter",
    "set_filters",
    "clear_filters",
    "show_dashboard",
    "noop",
]


class DashboardOperationItem(BaseModel):
    action: DashboardAction
    target: WidgetRef | None = None
    widget_spec: WidgetAddSpec | None = None
    widget_patch: WidgetPatch | None = None
    filter: FilterSpec | None = None
    filters: list[FilterSpec] | None = None
    position: WidgetPosition | None = None
    position_preset: Literal["top", "bottom"] | None = None
    title: str | None = None


class ClarificationChoice(BaseModel):
    id: str
    label: str
    widget_id: str | None = None


class ClarificationRequest(BaseModel):
    kind: Literal[
        "pick_widget",
        "pick_metric",
        "pick_date_field",
        "pick_filter_value",
        "confirm_remove_all",
        "no_dashboard",
        "no_semantic_metrics",
    ]
    message: str
    choices: list[ClarificationChoice] = Field(default_factory=list)
    # False for a dead-end clarification with nothing to resume from (e.g.
    # "there are no metrics defined at all" — the fix means leaving the
    # chat to configure Semantic settings, not answering a follow-up here).
    # Pending-operation resolution only knows how to consume a choice pick
    # or a filter value; without one of those it can never resolve, so if
    # we stored a pending_operation for it anyway, every later message —
    # including an unrelated new instruction — would just re-trigger "I
    # still need clarification" forever with no way out. Non-resumable
    # clarifications are shown once and leave no state behind.
    resumable: bool = True


class DashboardOperationPlan(BaseModel):
    dashboard_id: UUID
    operations: list[DashboardOperationItem] = Field(default_factory=list, max_length=5)
    confidence: float = 1.0
    clarification: ClarificationRequest | None = None


class WidgetCatalogEntry(BaseModel):
    id: str
    type: str
    title: str
    chart: str | None = None
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    position: dict[str, Any] = Field(default_factory=dict)
    has_error: bool = False


class PendingDashboardOp(BaseModel):
    plan: DashboardOperationPlan
    unresolved_index: int = 0
    choices: list[ClarificationChoice] = Field(default_factory=list)
    pending_filter_field: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResolvedOperation(BaseModel):
    item: DashboardOperationItem
    resolved_widget_ids: list[str] = Field(default_factory=list)


class WidgetResolution(BaseModel):
    status: Literal["resolved", "ambiguous", "not_found"]
    widget_ids: list[str] = Field(default_factory=list)
    candidates: list[WidgetCatalogEntry] = Field(default_factory=list)
    scores: list[float] = Field(default_factory=list)


class DashboardEditResult(BaseModel):
    success: bool = False
    dashboard: dict[str, Any] | None = None
    edit_notes: list[str] = Field(default_factory=list)
    clarification: ClarificationRequest | None = None
    pending_operation: PendingDashboardOp | None = None
    error: str | None = None
    modified_widget_ids: list[str] = Field(default_factory=list)
