import assert from "node:assert/strict";
import test from "node:test";
import {
  defaultPositionForWidget,
  hasExplicitGridLayout,
  isExplicitGridPosition,
  shouldCollapseVizPaneForDashboardTab,
  shouldRenderDashboardInVizPane,
  withSafePositions,
} from "./dashboardLayout.ts";

test("empty position does not enable grid mode", () => {
  assert.equal(isExplicitGridPosition({}), false);
  assert.equal(isExplicitGridPosition(null), false);
  assert.equal(isExplicitGridPosition(undefined), false);
  assert.equal(isExplicitGridPosition({ x: Number.NaN, y: 0, w: 3, h: 2 }), false);
  assert.equal(isExplicitGridPosition({ x: 0, y: 0, w: 0, h: 2 }), false);
  assert.equal(
    hasExplicitGridLayout([
      { type: "kpi", position: {} },
      { type: "chart", position: {} },
      { type: "ai_insight", position: { x: 0, y: 3, w: 12, h: 3 } },
    ]),
    false,
  );
});

test("widgets without position receive safe layout", () => {
  const widgets = [
    { id: "k1", type: "kpi" },
    { id: "c1", type: "chart" },
    { id: "i1", type: "ai_insight" },
  ];
  const positioned = withSafePositions(widgets);
  for (const widget of positioned) {
    assert.equal(isExplicitGridPosition(widget.position), true);
  }
  assert.equal(positioned[0].position.w >= 3, true);
  assert.equal(positioned[1].position.w, 6);
  assert.equal(positioned[2].position.w, 12);
  const fallback = defaultPositionForWidget({ type: "kpi" }, 0, widgets);
  assert.equal(fallback.x, 0);
  assert.equal(fallback.h, 2);
});

test("dashboard is rendered only once", () => {
  assert.equal(shouldRenderDashboardInVizPane("dashboard"), false);
  assert.equal(shouldRenderDashboardInVizPane("chat"), true);
});

test("dashboard tab collapses the idle viz pane", () => {
  assert.equal(shouldCollapseVizPaneForDashboardTab("dashboard"), true);
  assert.equal(shouldCollapseVizPaneForDashboardTab("chat"), false);
});

test("explicit data-widget coordinates enable grid mode", () => {
  assert.equal(
    hasExplicitGridLayout([
      { type: "kpi", position: { x: 0, y: 0, w: 3, h: 2 } },
      { type: "ai_insight", position: { x: 0, y: 8, w: 12, h: 3 } },
    ]),
    true,
  );
});
