"""Frontend layout contract — keep in sync with frontend/src/lib/dashboardLayout.ts."""


def is_explicit_grid_position(position) -> bool:
    if not isinstance(position, dict):
        return False
    x, y, w, h = (position.get("x"), position.get("y"), position.get("w"), position.get("h"))
    nums = [x, y, w, h]
    if not all(isinstance(v, (int, float)) for v in nums):
        return False
    return x >= 0 and y >= 0 and w >= 1 and h >= 1


def has_explicit_grid_layout(widgets: list[dict]) -> bool:
    return any(
        w.get("type") not in {"ai_insight", "text"} and is_explicit_grid_position(w.get("position"))
        for w in widgets
    )


def should_render_dashboard_in_viz_pane(main_tab: str) -> bool:
    return main_tab != "dashboard"


def should_collapse_viz_pane_for_dashboard_tab(main_tab: str) -> bool:
    return main_tab == "dashboard"


def _default_position(widget: dict, index: int, widgets: list[dict]) -> dict:
    wtype = widget.get("type") or "chart"
    kpis = [w for w in widgets if w.get("type") == "kpi"]
    kpi_width = max(3, 12 // min(max(len(kpis), 1), 4))
    if wtype == "kpi":
        kpi_index = sum(1 for w in widgets[:index] if w.get("type") == "kpi")
        return {
            "x": (kpi_index * kpi_width) % 12,
            "y": (kpi_index * kpi_width) // 12 * 2,
            "w": kpi_width,
            "h": 2,
        }
    if wtype in {"ai_insight", "text"}:
        charts = sum(1 for w in widgets if w.get("type") in {"chart", "pie", "table"})
        return {"x": 0, "y": 2 + charts * 4, "w": 12, "h": 3}
    chart_index = sum(1 for w in widgets[:index] if w.get("type") in {"chart", "pie", "table"})
    return {"x": (chart_index % 2) * 6, "y": 2 + chart_index // 2 * 4, "w": 6, "h": 4}


def test_empty_position_does_not_enable_grid_mode() -> None:
    assert is_explicit_grid_position({}) is False
    assert is_explicit_grid_position(None) is False
    widgets = [
        {"type": "kpi", "position": {}},
        {"type": "chart", "position": {}},
        {"type": "ai_insight", "position": {"x": 0, "y": 3, "w": 12, "h": 3}},
    ]
    assert has_explicit_grid_layout(widgets) is False


def test_widgets_without_position_get_safe_defaults() -> None:
    widgets = [
        {"id": "k1", "type": "kpi"},
        {"id": "c1", "type": "chart"},
        {"id": "i1", "type": "ai_insight"},
    ]
    positioned = [{**w, "position": _default_position(w, i, widgets)} for i, w in enumerate(widgets)]
    assert all(is_explicit_grid_position(w["position"]) for w in positioned)
    assert positioned[0]["position"]["w"] >= 3
    assert positioned[1]["position"]["w"] == 6
    assert positioned[2]["position"]["w"] == 12


def test_dashboard_is_rendered_only_once() -> None:
    assert should_render_dashboard_in_viz_pane("dashboard") is False
    assert should_render_dashboard_in_viz_pane("chat") is True


def test_dashboard_tab_collapses_idle_viz_pane() -> None:
    assert should_collapse_viz_pane_for_dashboard_tab("dashboard") is True
    assert should_collapse_viz_pane_for_dashboard_tab("chat") is False
