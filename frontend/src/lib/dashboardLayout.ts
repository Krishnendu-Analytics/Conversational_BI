/** Grid position helpers for dashboard widgets. Empty `{}` is not a layout. */

export type GridPosition = {
  x: number;
  y: number;
  w: number;
  h: number;
};

export type LayoutWidget = {
  id?: string;
  type?: string;
  position?: Partial<GridPosition> | Record<string, unknown> | null;
};

function asFiniteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** True only when x/y/w/h are all finite and spans are at least 1. */
export function isExplicitGridPosition(
  position: LayoutWidget["position"],
): position is GridPosition {
  if (position == null || typeof position !== "object") return false;
  const x = asFiniteNumber((position as GridPosition).x);
  const y = asFiniteNumber((position as GridPosition).y);
  const w = asFiniteNumber((position as GridPosition).w);
  const h = asFiniteNumber((position as GridPosition).h);
  return x != null && y != null && w != null && h != null && x >= 0 && y >= 0 && w >= 1 && h >= 1;
}

function isDataWidget(widget: LayoutWidget): boolean {
  const type = widget.type ?? "";
  return type !== "ai_insight" && type !== "text";
}

/**
 * Use CSS grid only when at least one data widget has explicit coordinates.
 * An Insights-only position (or empty `{}`) must not enable 12-column grid.
 */
export function hasExplicitGridLayout(widgets: LayoutWidget[]): boolean {
  return widgets.some((widget) => isDataWidget(widget) && isExplicitGridPosition(widget.position));
}

export function defaultPositionForWidget(
  widget: LayoutWidget,
  index: number,
  widgets: LayoutWidget[],
): GridPosition {
  const type = widget.type ?? "chart";
  const kpis = widgets.filter((item) => item.type === "kpi");
  const kpiCount = Math.max(kpis.length, 1);
  const kpiWidth = Math.max(3, Math.floor(12 / Math.min(kpiCount, 4)));

  if (type === "kpi") {
    const kpiIndex = widgets.slice(0, index).filter((item) => item.type === "kpi").length;
    return {
      x: (kpiIndex * kpiWidth) % 12,
      y: Math.floor((kpiIndex * kpiWidth) / 12) * 2,
      w: kpiWidth,
      h: 2,
    };
  }
  if (type === "ai_insight" || type === "text") {
    const charts = widgets.filter(
      (item) => item.type === "chart" || item.type === "pie" || item.type === "table",
    ).length;
    return { x: 0, y: 2 + charts * 4, w: 12, h: 3 };
  }
  const chartIndex = widgets
    .slice(0, index)
    .filter((item) => item.type === "chart" || item.type === "pie" || item.type === "table").length;
  return {
    x: (chartIndex % 2) * 6,
    y: 2 + Math.floor(chartIndex / 2) * 4,
    w: 6,
    h: 4,
  };
}

export function withSafePositions<T extends LayoutWidget>(widgets: T[]): T[] {
  return widgets.map((widget, index) => {
    if (isExplicitGridPosition(widget.position)) return widget;
    return { ...widget, position: defaultPositionForWidget(widget, index, widgets) };
  });
}

/** Whether the dashboard canvas should render a second copy of the spec. */
export function shouldRenderDashboardInVizPane(mainTab: "chat" | "dashboard"): boolean {
  return mainTab !== "dashboard";
}

/** Dashboard tab uses the left pane; hide the idle viz placeholder. */
export function shouldCollapseVizPaneForDashboardTab(
  mainTab: "chat" | "dashboard",
): boolean {
  return mainTab === "dashboard";
}
