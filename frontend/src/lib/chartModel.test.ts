import assert from "node:assert/strict";
import test from "node:test";
import { analyzeColumns, buildKpiFacts, formatCell, formatMeasure, planChart } from "./chartModel.ts";

const zoneState = [
  { zone: "East", state: "Bihar", sales_value: 88.7e9, profit_margin_percentage: 4.94 },
  { zone: "East", state: "Odisha", sales_value: 92.58e9, profit_margin_percentage: 4.86 },
  { zone: "West", state: "Goa", sales_value: 70e9, profit_margin_percentage: 5.1 },
  { zone: "West", state: "Gujarat", sales_value: 80e9, profit_margin_percentage: 5.0 },
];

test("downtime_hours is a measure, not a time axis", () => {
  const cols = analyzeColumns([{ plant: "A", downtime_hours: 12 }, { plant: "B", downtime_hours: 7 }]);
  assert.equal(cols.find((c) => c.key === "downtime_hours")?.role, "measure");
  const plan = planChart([{ plant: "A", downtime_hours: 12 }, { plant: "B", downtime_hours: 7 }]);
  assert.equal(plan.timeKey, null);
  assert.equal(plan.form, "bar");
});

test("zone + state keeps both levels in the category label, ranked", () => {
  const plan = planChart(zoneState, { measure: "sales_value" });
  assert.deepEqual(plan.categoryKeys, ["zone", "state"]);
  assert.equal(plan.data[0].label, "East / Odisha");
  assert.ok(plan.headline?.startsWith("East / Odisha leads"));
});

test("different-scale measures become small multiples, never a dual axis", () => {
  const plan = planChart(zoneState);
  assert.equal(plan.form, "multiples");
  assert.deepEqual(plan.measures, ["sales_value", "profit_margin_percentage"]);
});

test("percentages are never drawn as a pie and never folded into Other", () => {
  const rows = Array.from({ length: 30 }, (_, i) => ({ plant: `P${i}`, yield_pct: 50 + i }));
  const plan = planChart(rows, { suggestion: "pie" });
  assert.ok(!plan.forms.includes("pie"));
  assert.equal(plan.foldedIntoOther, 0);
  assert.ok(plan.data.every((d) => !d.isOther));
  assert.equal(plan.data.length, 20);
});

test("small additive tail folds into an Other bar that keeps the total", () => {
  const rows = Array.from({ length: 25 }, (_, i) => ({ customer: `C${i}`, revenue: i === 0 ? 1000 : 50 - i }));
  const plan = planChart(rows);
  const other = plan.data.find((d) => d.isOther);
  assert.ok(other);
  assert.equal(other?.revenue, [30, 29, 28, 27, 26].reduce((a, b) => a + b, 0));
  assert.equal(plan.form, "hbar");
});

test("a tail bigger than the leader is noted, not drawn as a giant Other bar", () => {
  const rows = Array.from({ length: 30 }, (_, i) => ({ customer: `C${i}`, revenue: 100 - i }));
  const plan = planChart(rows);
  assert.ok(plan.data.every((d) => !d.isOther));
  assert.match(plan.subtitle, /other 10: .* combined/);
});

test("UTC-midnight timestamps are calendar dates, not 05:30 local times", () => {
  const rows = [
    { month: "2024-04-01T00:00:00Z", qty: 10 },
    { month: "2024-05-01T00:00:00Z", qty: 12 },
    { month: "2024-06-01T00:00:00Z", qty: 11 },
  ];
  const plan = planChart(rows);
  assert.ok(plan.data.every((d) => !d.label.includes(":")));
  assert.match(plan.data[0].label, /2024/);
});

test("evenly spread breakdowns say so instead of crowning a leader", () => {
  const rows = Array.from({ length: 15 }, (_, i) => ({ product_name: `P${i}`, quantity_mt: 600 + (i % 5) }));
  const plan = planChart(rows);
  assert.match(plan.headline ?? "", /evenly spread across all 15 product name values/);
});

test("a clear outlier is called out", () => {
  const rows = Array.from({ length: 10 }, (_, i) => ({ customer: `C${i}`, revenue: i === 0 ? 1000 : 100 + i }));
  const plan = planChart(rows);
  assert.match(plan.headline ?? "", /C0 stands out on revenue/);
});

test("per-unit columns are not shared out as a total", () => {
  const rows = Array.from({ length: 6 }, (_, i) => ({ product: `P${i}`, standard_cost_per_mt: 70 + i * 5 }));
  const plan = planChart(rows);
  assert.doesNotMatch(plan.headline ?? "", /of the .* total/);
});

test("monthly series gets a year-over-year comparison and flags the dip", () => {
  const rows = Array.from({ length: 24 }, (_, i) => ({
    month: `${2023 + Math.floor(i / 12)}-${String((i % 12) + 1).padStart(2, "0")}-01`,
    qty: i === 22 ? 60 : i < 12 ? 100 : 104,
  }));
  const plan = planChart(rows);
  assert.ok(plan.yoy);
  assert.equal(plan.yoy?.unit, "%");
  assert.match(plan.headline ?? "", /year over year \(last 12 months vs the 12 before\)/);
  assert.match(plan.headline ?? "", /stands out as the low/);
  const kpis = buildKpiFacts(rows, plan);
  assert.ok(kpis.some((k) => k.id === "yoy"));
});

test("time series sorts chronologically and formats month labels", () => {
  const rows = [
    { month: "2024-03-01", sales: 30 },
    { month: "2024-01-01", sales: 10 },
    { month: "2024-02-01", sales: 20 },
    { month: "2024-04-01", sales: 25 },
  ];
  const plan = planChart(rows);
  assert.equal(plan.form, "line");
  assert.deepEqual(
    plan.data.map((d) => d.sales),
    [10, 20, 30, 25],
  );
  assert.ok(!plan.data[0].label.includes("T00"));
  assert.match(plan.headline ?? "", /rose 150\.0%/);
});

test("month-name columns order by calendar, not alphabet", () => {
  const rows = [
    { month: "March", units: 3 },
    { month: "January", units: 1 },
    { month: "February", units: 2 },
  ];
  const plan = planChart(rows);
  assert.deepEqual(
    plan.data.map((d) => d.units),
    [1, 2, 3],
  );
});

test("time x category pivots into one line per category", () => {
  const rows = [
    { month: "2024-01-01", region: "N", sales: 1 },
    { month: "2024-01-01", region: "S", sales: 2 },
    { month: "2024-02-01", region: "N", sales: 3 },
    { month: "2024-02-01", region: "S", sales: 4 },
  ];
  const plan = planChart(rows);
  assert.equal(plan.seriesKey, "region");
  assert.deepEqual(plan.measures.sort(), ["N", "S"]);
  assert.equal(plan.data.length, 2);
});

test("KPI tiles report findings, not row counts", () => {
  const plan = planChart(zoneState, { measure: "sales_value" });
  const kpis = buildKpiFacts(zoneState, plan);
  assert.ok(!kpis.some((k) => k.label === "Rows"));
  const top = kpis.find((k) => k.id === "top");
  assert.equal(top?.value, "East / Odisha");
});

test("percent formatting only adds % on a 0-100 scale", () => {
  assert.equal(formatMeasure(4.94, "percentage", true), "4.94%");
  assert.equal(formatMeasure(0.38, "percentage", false), "0.38");
  assert.equal(formatMeasure(88.7e9), "88.7B");
});

test("numeric years label the axis as 2025, not 2,025", () => {
  const plan = planChart([
    { year: 2024, sales_growth_pct: 12.5 },
    { year: 2025, sales_growth_pct: 30.9 },
    { year: 2026, sales_growth_pct: 8.1 },
  ]);
  assert.equal(plan.timeKey, "year");
  assert.deepEqual(plan.data.map((d) => d.label), ["2024", "2025", "2026"]);
  assert.equal(formatCell(2025, "calendar_year"), "2025");
  assert.equal(formatCell(2025), "2,025".replace(",", (1000).toLocaleString().charAt(1)));
});
