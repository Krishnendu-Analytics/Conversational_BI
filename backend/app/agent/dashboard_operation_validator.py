"""Validate dashboard operation plans before apply."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.agent.dashboard_filters import validate_dashboard_filters
from app.agent.dashboard_operation_schema import (
    ClarificationChoice,
    ClarificationRequest,
    DashboardOperationItem,
    DashboardOperationPlan,
    ResolvedOperation,
    WidgetCatalogEntry,
)
from app.agent.dashboard_widget_resolver import resolve_widget_target
from app.agent.sql_compiler import _catalog_maps


class ValidationResult:
    def __init__(
        self,
        *,
        ok: bool,
        resolved: list[ResolvedOperation] | None = None,
        errors: list[str] | None = None,
        clarification: ClarificationRequest | None = None,
        unresolved_index: int = 0,
    ) -> None:
        self.ok = ok
        self.resolved = resolved or []
        self.errors = errors or []
        self.clarification = clarification
        self.unresolved_index = unresolved_index


def _metric_exists(name: str, metrics: dict[str, str]) -> bool:
    return name.lower() in metrics


def _dimension_exists(name: str, dimensions: dict[str, str]) -> bool:
    return name.lower() in dimensions


def validate_operation_plan(
    plan: DashboardOperationPlan,
    *,
    loaded_dashboard_id: UUID,
    catalog: list[WidgetCatalogEntry],
    semantic_catalog: dict[str, Any] | None,
    active_widget_id: str | None = None,
) -> ValidationResult:
    if plan.clarification:
        return ValidationResult(ok=False, clarification=plan.clarification)

    if plan.dashboard_id != loaded_dashboard_id:
        return ValidationResult(
            ok=False,
            errors=[f"Plan dashboard_id {plan.dashboard_id} does not match active dashboard."],
        )

    if not plan.operations:
        return ValidationResult(ok=False, errors=["No dashboard operations in plan."])

    metrics, dimensions = _catalog_maps(semantic_catalog)
    resolved_ops: list[ResolvedOperation] = []

    for idx, item in enumerate(plan.operations):
        err = _validate_item_fields(item, metrics, dimensions, semantic_catalog)
        if err == "__no_semantic_metrics__":
            return ValidationResult(
                ok=False,
                clarification=ClarificationRequest(
                    kind="no_semantic_metrics",
                    message=(
                        "This connection doesn't have any metrics defined yet, "
                        "so I can't add a new KPI or chart to the dashboard. "
                        "Go to Semantic → Discover to auto-suggest metrics "
                        "from your schema (or define one manually), then ask "
                        "me to add the widget again."
                    ),
                    choices=[],
                    resumable=False,
                ),
                unresolved_index=idx,
            )
        if err == "__clarify_metric__":
            metric_labels = [
                str(item.get("name"))
                for item in (semantic_catalog or {}).get("metrics") or []
                if isinstance(item, dict) and item.get("name")
            ][:6]
            return ValidationResult(
                ok=False,
                clarification=ClarificationRequest(
                    kind="pick_metric",
                    message="Which metric should this dashboard widget use?",
                    choices=[
                        ClarificationChoice(id=str(i + 1), label=label, widget_id="")
                        for i, label in enumerate(metric_labels)
                    ],
                ),
                unresolved_index=idx,
            )
        if err == "__clarify_date_field__":
            date_dims = [
                str(d.get("name"))
                for d in (semantic_catalog or {}).get("dimensions") or []
                if isinstance(d, dict)
                and d.get("name")
                and any(
                    t in str(d.get("expression") or d.get("name")).lower()
                    for t in ("date", "_at", "time")
                )
            ][:6]
            return ValidationResult(
                ok=False,
                clarification=ClarificationRequest(
                    kind="pick_date_field",
                    message="Which date field should this widget use?",
                    choices=[
                        ClarificationChoice(id=str(i + 1), label=name, widget_id="")
                        for i, name in enumerate(date_dims)
                    ],
                ),
                unresolved_index=idx,
            )
        if err:
            return ValidationResult(ok=False, errors=[err])

        if item.action in {"remove_widget", "update_widget", "move_widget", "rename_widget"}:
            allow_bulk = item.action == "remove_widget" and bool(
                item.target and item.target.widget_type and not item.target.widget_id
            )
            resolution = resolve_widget_target(
                item.target,
                catalog,
                active_widget_id=active_widget_id,
                allow_bulk_type=allow_bulk,
            )
            if resolution.status == "not_found":
                return ValidationResult(
                    ok=False,
                    errors=[f"Could not find a widget matching: {item.target}"],
                )
            if resolution.status == "ambiguous":
                choices = [
                    ClarificationChoice(id=str(i + 1), label=e.title, widget_id=e.id)
                    for i, e in enumerate(resolution.candidates)
                ]
                return ValidationResult(
                    ok=False,
                    clarification=ClarificationRequest(
                        kind="pick_widget",
                        message=(
                            "Multiple widgets match that request. "
                            "Which one did you mean?"
                        ),
                        choices=choices,
                    ),
                    unresolved_index=idx,
                )
            resolved_ops.append(
                ResolvedOperation(item=item, resolved_widget_ids=resolution.widget_ids)
            )
        elif item.action == "add_filter":
            if item.filter is None:
                return ValidationResult(ok=False, errors=["add_filter requires a filter spec"])
            if item.filter.value is None or item.filter.value == "":
                field = item.filter.field
                return ValidationResult(
                    ok=False,
                    clarification=ClarificationRequest(
                        kind="pick_filter_value",
                        message=f"Which value should I filter {field} by?",
                        choices=[],
                    ),
                    unresolved_index=idx,
                )
            resolved_ops.append(ResolvedOperation(item=item))
        else:
            resolved_ops.append(ResolvedOperation(item=item))

    return ValidationResult(ok=True, resolved=resolved_ops)


def _metrics_matching_text(text: str, semantic_catalog: dict[str, Any] | None) -> list[str]:
    lowered = (text or "").lower()
    names: list[str] = []
    for item in (semantic_catalog or {}).get("metrics") or []:
        if isinstance(item, dict) and item.get("name"):
            name = str(item["name"])
            if name.lower() in lowered:
                names.append(name)
    return names


def _validate_item_fields(
    item: DashboardOperationItem,
    metrics: dict[str, str],
    dimensions: dict[str, str],
    semantic_catalog: dict[str, Any] | None,
) -> str | None:
    if item.action == "add_widget":
        spec = item.widget_spec
        if spec is None:
            return "add_widget requires widget_spec"
        metric_names = list(metrics.keys())
        title_text = spec.title or spec.metric or ""
        if spec.widget_type in {"kpi", "chart"} and not spec.metric:
            if not metric_names:
                # Distinct from "__clarify_metric__" (which picks among
                # several candidates): there is nothing to pick from at
                # all, because this connection has no semantic metrics
                # defined yet. That's a very common state for a freshly
                # connected data source — surface it as guidance toward
                # the fix (Semantic → Discover) rather than a bare
                # "requires a semantic metric name" error that gives the
                # user nothing to act on.
                return "__no_semantic_metrics__"
            matched = _metrics_matching_text(title_text, semantic_catalog)
            if len(matched) > 1:
                return "__clarify_metric__"
            if len(metric_names) > 1 and not matched:
                return "__clarify_metric__"
        if spec.metric and not _metric_exists(spec.metric, metrics):
            return f"Unknown metric: {spec.metric}"
        if spec.dimension and not _dimension_exists(spec.dimension, dimensions):
            return f"Unknown dimension: {spec.dimension}"
        if spec.widget_type == "chart" and spec.role == "trend" and not spec.dimension:
            date_dims = [
                name
                for name in dimensions
                if any(t in name.lower() for t in ("date", "time"))
            ]
            if len(date_dims) > 1:
                return "__clarify_date_field__"
        return None

    if item.action in {"add_filter", "set_filters"}:
        raw_filters = []
        if item.filter:
            raw_filters.append(item.filter.model_dump())
        if item.filters:
            raw_filters.extend(f.model_dump() for f in item.filters)
        if not raw_filters:
            return f"{item.action} requires filter(s)"
        try:
            validate_dashboard_filters(raw_filters, semantic_catalog)
        except ValueError as exc:
            return str(exc)
        return None

    if item.action == "update_widget" and item.widget_patch:
        patch = item.widget_patch
        if patch.metric and not _metric_exists(patch.metric, metrics):
            return f"Unknown metric: {patch.metric}"
        if patch.dimension and not _dimension_exists(patch.dimension, dimensions):
            return f"Unknown dimension: {patch.dimension}"
    return None
