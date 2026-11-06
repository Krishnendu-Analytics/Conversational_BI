"""Deterministic in-memory dashboard operation applier."""

from __future__ import annotations

import copy
from typing import Any

from app.agent.dashboard_operation_schema import ResolvedOperation, WidgetPosition
from app.agent.dashboard_semantic_widget import build_widget_from_semantic, regenerate_widget_sql


def _clone_spec(dashboard: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dashboard)


def _normalize_spec(dashboard: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": dashboard.get("id"),
        "title": dashboard.get("title") or dashboard.get("name") or "Dashboard",
        "name": dashboard.get("name") or dashboard.get("title") or "Dashboard",
        "source_table": dashboard.get("source_table"),
        "filters": list(dashboard.get("filters") or []),
        "layout": dict(dashboard.get("layout") or {}),
        "widgets": [
            dict(w)
            for w in dashboard.get("widgets") or []
            if isinstance(w, dict)
        ],
    }


def apply_resolved_operations(
    dashboard: dict[str, Any],
    resolved: list[ResolvedOperation],
    *,
    semantic_catalog: dict[str, Any],
    schema: dict[str, Any],
    db_type: str,
) -> tuple[dict[str, Any], list[str], set[str]]:
    """Apply all resolved operations to a copy of the dashboard spec."""
    spec = _normalize_spec(dashboard)
    notes: list[str] = []
    modified_ids: set[str] = set()

    for rop in resolved:
        item = rop.item
        action = item.action

        if action == "noop" or action == "show_dashboard":
            notes.append("No changes applied.")
            continue

        if action == "rename_dashboard" and item.title:
            spec["title"] = item.title
            spec["name"] = item.title
            notes.append(f"Renamed dashboard to **{item.title}**.")
            continue

        if action == "clear_filters":
            spec["filters"] = []
            notes.append("Cleared all dashboard filters.")
            continue

        if action == "set_filters" and item.filters is not None:
            spec["filters"] = [f.model_dump() for f in item.filters]
            notes.append("Updated dashboard filters.")
            continue

        if action == "add_filter" and item.filter is not None:
            spec["filters"].append(item.filter.model_dump())
            notes.append(f"Added filter on {item.filter.field}.")
            continue

        if action == "remove_filter" and item.filter is not None:
            field = item.filter.field.lower()
            before = len(spec["filters"])
            spec["filters"] = [
                f
                for f in spec["filters"]
                if str(f.get("field", "")).lower() != field
            ]
            if len(spec["filters"]) < before:
                notes.append(f"Removed filter on {item.filter.field}.")
            else:
                notes.append(f"No filter found for {item.filter.field}.")
            continue

        if action == "remove_widget":
            ids = set(rop.resolved_widget_ids)
            before = len(spec["widgets"])
            spec["widgets"] = [w for w in spec["widgets"] if str(w.get("id")) not in ids]
            removed = before - len(spec["widgets"])
            if removed:
                notes.append(f"Removed {removed} widget(s).")
                modified_ids.update(ids)
            else:
                notes.append("No matching widgets to remove.")
            continue

        if action == "add_widget" and item.widget_spec is not None:
            widget = build_widget_from_semantic(
                item.widget_spec,
                semantic_catalog=semantic_catalog,
                schema=schema,
                db_type=db_type,
            )
            pos = item.position or WidgetPosition(
                y=_next_row(spec),
                w=6 if widget.get("type") == "chart" else 3,
                h=3 if widget.get("type") == "kpi" else 4,
            )
            widget["position"] = pos.model_dump()
            spec["widgets"].append(widget)
            wid = str(widget.get("id"))
            modified_ids.add(wid)
            notes.append(f"Added widget **{widget.get('title')}**.")
            continue

        if action == "update_widget" and item.widget_patch is not None:
            patch = item.widget_patch
            for widget in spec["widgets"]:
                if str(widget.get("id")) not in rop.resolved_widget_ids:
                    continue
                wid = str(widget.get("id"))
                if patch.title:
                    widget["title"] = patch.title
                if patch.chart:
                    widget["chart"] = patch.chart
                if patch.widget_type:
                    widget["type"] = patch.widget_type
                if patch.metric or patch.dimension:
                    widget["sql"] = regenerate_widget_sql(
                        widget,
                        patch.metric,
                        patch.dimension,
                        semantic_catalog=semantic_catalog,
                        schema=schema,
                        db_type=db_type,
                    )
                modified_ids.add(wid)
                notes.append(f"Updated widget **{widget.get('title')}**.")
            continue

        if action == "rename_widget" and item.title:
            for widget in spec["widgets"]:
                if str(widget.get("id")) in rop.resolved_widget_ids:
                    widget["title"] = item.title
                    modified_ids.add(str(widget.get("id")))
                    notes.append(f"Renamed widget to **{item.title}**.")
            continue

        if action == "move_widget":
            for widget in spec["widgets"]:
                if str(widget.get("id")) not in rop.resolved_widget_ids:
                    continue
                wid = str(widget.get("id"))
                pos = dict(widget.get("position") or {})
                if item.position_preset == "top":
                    pos["y"] = 0
                    pos.setdefault("x", 0)
                    pos.setdefault("w", 6)
                    pos.setdefault("h", 3)
                    _shift_widgets_down(spec, exclude_id=wid)
                elif item.position:
                    pos = item.position.model_dump()
                widget["position"] = pos
                modified_ids.add(wid)
                notes.append(f"Moved widget **{widget.get('title')}**.")
            continue

    return spec, notes, modified_ids


def _next_row(spec: dict[str, Any]) -> int:
    max_y = 0
    for w in spec.get("widgets") or []:
        pos = w.get("position") or {}
        y = int(pos.get("y") or 0)
        h = int(pos.get("h") or 3)
        max_y = max(max_y, y + h)
    return max_y


def _shift_widgets_down(spec: dict[str, Any], *, exclude_id: str) -> None:
    for w in spec.get("widgets") or []:
        if str(w.get("id")) == exclude_id:
            continue
        pos = dict(w.get("position") or {})
        pos["y"] = int(pos.get("y") or 0) + 3
        w["position"] = pos
