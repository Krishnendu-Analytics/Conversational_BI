"""Structured dashboard edit operations."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel


class DashboardOperation(BaseModel):
    action: Literal[
        "add_widget",
        "remove_widget",
        "update_widget",
        "update_layout",
        "add_filter",
        "set_filter",
        "show_dashboard",
    ]
    widget_id: str | None = None
    widget: dict[str, Any] | None = None
    layout: dict[str, Any] | None = None
    filter: dict[str, Any] | None = None
    title: str | None = None


def apply_dashboard_operation(
    op: DashboardOperation,
    dashboard_dict: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Apply a structured dashboard operation to an in-memory dashboard spec."""
    notes: list[str] = []
    spec = {
        "title": dashboard_dict.get("title") or dashboard_dict.get("name") or "Dashboard",
        "name": dashboard_dict.get("name") or dashboard_dict.get("title") or "Dashboard",
        "source_table": dashboard_dict.get("source_table"),
        "filters": list(dashboard_dict.get("filters") or []),
        "layout": dict(dashboard_dict.get("layout") or {}),
        "widgets": [
            dict(widget)
            for widget in dashboard_dict.get("widgets") or []
            if isinstance(widget, dict)
        ],
    }

    if op.title:
        spec["title"] = op.title
        spec["name"] = op.title
        notes.append(f"Renamed dashboard to **{op.title}**.")

    if op.action == "show_dashboard":
        notes.append("Showing the current dashboard.")
        return spec, notes

    if op.action == "update_layout" and op.layout is not None:
        spec["layout"] = {**spec["layout"], **op.layout}
        notes.append("Updated dashboard layout.")
        return spec, notes

    if op.action == "add_filter" and op.filter is not None:
        spec["filters"].append(op.filter)
        notes.append("Added dashboard filter.")
        return spec, notes

    if op.action == "set_filter" and op.filter is not None:
        spec["filters"] = [op.filter]
        notes.append("Set dashboard filter.")
        return spec, notes

    if op.action == "remove_widget":
        if op.widget_id in {None, ""}:
            before = len(spec["widgets"])
            spec["widgets"] = [
                widget for widget in spec["widgets"] if widget.get("type") != "chart"
            ]
            if len(spec["widgets"]) < before:
                notes.append("Removed chart widgets from the dashboard.")
            else:
                notes.append("No chart widgets were found to remove.")
        elif op.widget_id == "kpi-rows" or "kpi" in op.widget_id.lower():
            before = len(spec["widgets"])
            spec["widgets"] = [
                widget for widget in spec["widgets"] if widget.get("type") != "kpi"
            ]
            if len(spec["widgets"]) < before:
                notes.append("Removed KPI widgets from the dashboard.")
            else:
                notes.append("No KPI widgets were found to remove.")
        else:
            before = len(spec["widgets"])
            spec["widgets"] = [
                widget for widget in spec["widgets"] if str(widget.get("id")) != op.widget_id
            ]
            if len(spec["widgets"]) < before:
                notes.append(f"Removed widget `{op.widget_id}`.")
            else:
                notes.append(f"Widget `{op.widget_id}` was not found.")
        return spec, notes

    if op.action == "update_widget" and op.widget_id and op.widget:
        updated = False
        for widget in spec["widgets"]:
            if str(widget.get("id")) == op.widget_id:
                widget.update(op.widget)
                updated = True
                break
        notes.append(
            f"Updated widget `{op.widget_id}`." if updated else f"Widget `{op.widget_id}` was not found."
        )
        return spec, notes

    if op.action == "add_widget" and op.widget:
        widget = dict(op.widget)
        if not widget.get("id"):
            widget["id"] = f"widget-{len(spec['widgets']) + 1}"
        spec["widgets"].append(widget)
        notes.append(f"Added widget `{widget.get('id')}`.")
        return spec, notes

    notes.append("No dashboard changes were applied for that operation.")
    return spec, notes


def _parse_llm_operation(raw: str) -> DashboardOperation | None:
    text = (raw or "").strip()
    if not text or text.lower().startswith("[llm stub]"):
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        body = lines[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        text = "\n".join(body).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    try:
        return DashboardOperation.model_validate(data)
    except Exception:  # noqa: BLE001
        return None


def _regex_fallback_operation(instruction: str) -> DashboardOperation | None:
    text = (instruction or "").lower()

    if any(token in text for token in ("show dashboard", "open dashboard", "display dashboard")):
        return DashboardOperation(action="show_dashboard")

    match = re.search(r"rename(?:\s+dashboard)?\s+to\s+[\"']([^\"']+)[\"']", text)
    if match:
        return DashboardOperation(
            action="show_dashboard",
            title=match.group(1).strip().title(),
        )

    if any(token in text for token in ("remove chart", "remove trend", "hide chart")):
        return DashboardOperation(action="remove_widget", widget_id=None)

    remove_match = re.search(r"remove (?:widget|chart|kpi)\s+([a-z0-9_-]+)", text)
    if remove_match:
        return DashboardOperation(action="remove_widget", widget_id=remove_match.group(1))

    if any(token in text for token in ("add trend", "add line", "show trend", "over time")):
        return DashboardOperation(
            action="add_widget",
            widget={"type": "chart", "title": "Trend Over Time", "chart": "line"},
        )

    if any(token in text for token in ("add breakdown", "add bar", "by category", "by type")):
        return DashboardOperation(
            action="add_widget",
            widget={"type": "chart", "title": "Breakdown", "chart": "bar"},
        )

    if "remove kpi" in text or "remove total" in text:
        return DashboardOperation(action="remove_widget", widget_id="kpi-rows")

    filter_match = re.search(r"filter (?:by|on)\s+([a-z0-9_]+)\s*=\s*([\"']?)([^\"']+)\2", text)
    if filter_match:
        return DashboardOperation(
            action="set_filter",
            filter={
                "field": filter_match.group(1),
                "operator": "eq",
                "value": filter_match.group(3),
            },
        )

    return None


def plan_dashboard_operation(
    llm_client: Any,
    instruction: str,
    dashboard_dict: dict[str, Any],
) -> DashboardOperation | None:
    """Plan a dashboard edit using LLM with regex fallbacks."""
    fallback = _regex_fallback_operation(instruction)
    if llm_client is not None and getattr(llm_client, "client", None) is not None:
        prompt = (
            "Convert the dashboard edit instruction into JSON with keys: "
            "action (add_widget|remove_widget|update_widget|update_layout|add_filter|"
            "set_filter|show_dashboard), widget_id, widget, layout, filter, title.\n\n"
            f"Current dashboard:\n{json.dumps(dashboard_dict, default=str)[:5000]}\n\n"
            f"Instruction:\n{instruction}\n"
        )
        raw = llm_client.complete(prompt)
        parsed = _parse_llm_operation(raw)
        if parsed is not None:
            return parsed
    return fallback
