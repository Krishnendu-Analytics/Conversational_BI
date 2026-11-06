/**
 * Enhanced dashboard: Recharts, cross-filtering, advanced charts, performance optimized.
 */

import { useState, useMemo, useCallback, useRef, memo,type CSSProperties, type ReactNode } from "react";
import { ChatMarkdown } from "@/components/ChatMarkdown";
import {
  hasExplicitGridLayout,
  isExplicitGridPosition,
  withSafePositions,
  type GridPosition,
} from "@/lib/dashboardLayout";
import { applyFiltersToRows, type DashboardFilterState } from "@/lib/dashboardFilters";
import { StackedBarComponent, WaterfallComponent, FunnelComponent, GaugeComponent } from "@/components/AdvancedCharts";
import { SmartChart } from "@/components/SmartChart";
import { formatCompact, planChart } from "@/lib/chartModel";

export type WidgetPosition = GridPosition;

export type DashboardWidget = {
  id: string;
  type: "kpi" | "chart" | "table" | "pie" | "ai_insight" | "text";
  title: string;
  chart?: "bar" | "line" | "pie" | "area" | "stacked" | "waterfall" | "funnel" | "gauge" | null;
  rows?: Record<string, unknown>[];
  value?: unknown;
  error?: string;
  content?: string;
  insight_status?: string | null;
  position?: Partial<WidgetPosition> | null;
  section?: string | null;
  /** Category column the chart breaks down by (used for cross-filtering). */
  dimension?: string | null;
  /** Semantic metric the widget measures (pairs a KPI with its trend). */
  metric?: string | null;
  purpose?: string | null;
  /** "higher_better" / "lower_better": whether a rise is good news. */
  polarity?: string | null;
  /** Multi-series widgets (e.g. actual vs target): column -> series name. */
  measure_labels?: Record<string, string> | null;
  value_kind?: string | null;
  aggregation?: string | null;
  /** Display unit shown after the value ("MT", "days"). */
  unit?: string | null;
  /** One-line definition of the KPI, shown on hover. */
  note?: string | null;
  /** Set when the value is implausible (e.g. targets off in scale or units). */
  data_warning?: string | null;
  /** "rolling_12m" (last 12 months vs prior 12) or "snapshot" (latest state). */
  period_kind?: string | null;
  /** Not re-queried on this refresh; rows are the last stored ones. */
  stale?: boolean;
  result?: {
    value_kind?: string;
    aggregation?: string | null;
    raw_value?: number | null;
    series_total?: number | null;
    is_percentage?: boolean;
    points?: Array<{
      label: string;
      raw_value: number;
      share_of_series?: number | null;
    }>;
  } | null;
};

export type DashboardFilter = {
  field: string;
  operator?: string;
  value?: string | number | string[] | null;
};

export type DashboardSpec = {
  title: string;
  widgets: DashboardWidget[];
  layout?: { cols?: number; rows?: number };
  filters?: DashboardFilter[];
};

type Props = {
  dashboard: DashboardSpec;
  queryLabel?: string;
  error?: string | null;
};

const GRID_COLS = 12;
const MAX_CHART_POINTS = 24;

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "string") {
    const trimmed = value.trim().replace(/,/g, "");
    if (!trimmed) return null;
    const n = Number(trimmed);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

const formatNumber = formatCompact;

function widgetValueKind(widget: DashboardWidget): string {
  return widget.result?.value_kind || widget.value_kind || "";
}

function formatMetric(n: number, kind?: string | null): string {
  if (kind === "percentage") return `${formatNumber(n)}%`;
  return formatNumber(n);
}

function formatValue(value: unknown, kind?: string | null, unit?: string | null): string {
  if (value == null || value === "") return "—";
  const n = asNumber(value);
  if (n != null) {
    const text = kind === "percentage" && Math.abs(n) < 1000 ? `${n.toFixed(1)}%` : formatMetric(n, kind);
    return unit ? `${text} ${unit}` : text;
  }
  return String(value);
}

function widgetValue(widget: DashboardWidget): unknown {
  if (widget.result?.raw_value != null) return widget.result.raw_value;
  if (widget.value != null) return widget.value;
  const rows = widget.rows ?? [];
  if (rows.length > 0 && rows[0].value != null) return rows[0].value;
  return null;
}

function gridStyle(widget: DashboardWidget): CSSProperties | undefined {
  if (!isExplicitGridPosition(widget.position)) return undefined;
  const pos = widget.position;
  return {
    gridColumn: `${pos.x + 1} / span ${pos.w}`,
    gridRow: `${pos.y + 1} / span ${pos.h}`,
  };
}

function buildChartData(widget: DashboardWidget) {
  const canonical = widget.result?.points;
  if (canonical && canonical.length > 0) {
    return canonical.slice(0, MAX_CHART_POINTS).map((p) => ({
      name: p.label,
      value: p.raw_value,
      share: p.share_of_series ?? null,
    }));
  }
  const rows = widget.rows ?? [];
  if (rows.length === 0) return [];
  const keys = Object.keys(rows[0]);
  const valueKey =
    keys.find((k) => k.toLowerCase() === "value") ??
    keys.find((k) => asNumber(rows[0][k]) != null) ??
    keys[keys.length - 1];
  const labelKey =
    keys.find((k) => k !== valueKey && k.toLowerCase() !== "value") ?? keys[0];

  return rows.slice(0, MAX_CHART_POINTS).map((r) => ({
    name: String(r[labelKey] ?? ""),
    value: asNumber(r[valueKey]) ?? 0,
  }));
}

type KpiDelta = { text: string; sub: string; direction: "up" | "down" | "flat" };

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** 202603 -> "Mar 2026". */
function formatYyyymm(value: unknown): string | null {
  const n = asNumber(value);
  if (n == null || n < 190001) return null;
  const month = Math.round(n) % 100;
  if (month < 1 || month > 12) return null;
  return `${MONTHS[month - 1]} ${Math.floor(Math.round(n) / 100)}`;
}

/** Change for a KPI whose query returns its own comparison (``previous``),
 *  e.g. overview KPIs: last 12 months vs the 12 before. Percentages move in
 *  points, everything else in percent. */
function rowDelta(widget: DashboardWidget): KpiDelta | null {
  const row = widget.rows?.[0];
  if (!row || !("previous" in row)) return null;
  const value = asNumber(row.value);
  const previous = asNumber(row.previous);
  if (value == null || previous == null) return null;
  const isPct = widgetValueKind(widget) === "percentage";
  const change = isPct ? value - previous : previous === 0 ? null : ((value - previous) / Math.abs(previous)) * 100;
  if (change == null) return null;
  const arrow = change > 0 ? "▲" : change < 0 ? "▼" : "■";
  const magnitude = isPct ? `${Math.abs(change).toFixed(1)} pts` : `${Math.abs(change).toFixed(1)}%`;
  const snapshot = widget.period_kind === "snapshot";
  return {
    text: `${arrow} ${magnitude}`,
    sub: snapshot ? "vs a year earlier" : "vs prior 12 months",
    direction: Math.abs(change) < 0.05 ? "flat" : change > 0 ? "up" : "down",
  };
}

/** "Last 12 months to Mar 2026" / "as of Mar 2026" for time-framed KPIs. */
function kpiPeriod(widget: DashboardWidget): string | null {
  const end = formatYyyymm(widget.rows?.[0]?.period_end);
  if (!end) return null;
  return widget.period_kind === "snapshot" ? `As of ${end}` : `Last 12 months to ${end}`;
}

const KpiTile = memo(function KpiTile({
  widget,
  delta,
  label,
}: {
  widget: DashboardWidget;
  delta?: KpiDelta | null;
  label?: string;
}) {
  const title = label ?? widget.title;
  const period = kpiPeriod(widget);
  // Colour says good/bad, the arrow says up/down: a rising rejection rate is
  // an up-arrow in the "bad" colour.
  const lowerBetter = widget.polarity === "lower_better";
  const tone =
    delta == null || delta.direction === "flat"
      ? "flat"
      : (delta.direction === "up") !== lowerBetter
        ? "up"
        : "down";
  if (widget.error) {
    return (
      <div className="insight-kpi dashboard-kpi dashboard-kpi-error">
        <p className="insight-kpi-label">{title}</p>
        <p className="dashboard-widget-error">{widget.error}</p>
      </div>
    );
  }
  const warned = Boolean(widget.data_warning);
  return (
    <div
      className={`insight-kpi dashboard-kpi${warned ? " dashboard-kpi-warned" : ""}`}
      title={widget.note ?? undefined}
    >
      <p className="insight-kpi-label">{title}</p>
      <p className="insight-kpi-value">
        {formatValue(widgetValue(widget), widgetValueKind(widget), widget.unit)}
      </p>
      {warned ? (
        <p className="dashboard-kpi-warning" role="note">
          ⚠ {widget.data_warning}
        </p>
      ) : delta ? (
        <p className={`insight-kpi-delta insight-kpi-delta-${tone}`} title={delta.sub}>
          {delta.text} <span className="dashboard-kpi-delta-sub">{delta.sub}</span>
        </p>
      ) : null}
      {period || widget.stale ? (
        <p className="dashboard-kpi-period">
          {period}
          {widget.stale ? `${period ? " · " : ""}not refreshed this time` : ""}
        </p>
      ) : null}
    </div>
  );
});

function isTrendWidget(w: DashboardWidget): boolean {
  return w.purpose === "trend" || w.chart === "line" || w.chart === "area";
}

/** Change for a KPI, read from the trend of the same metric in the same
 *  section: year over year when the history allows, else the latest
 *  period vs the one before. */
function kpiDeltas(widgets: DashboardWidget[]): Map<string, KpiDelta> {
  const out = new Map<string, KpiDelta>();
  for (const kpi of widgets) {
    if (kpi.type !== "kpi" || kpi.error) continue;
    const own = rowDelta(kpi);
    if (own) {
      out.set(kpi.id, own);
      continue;
    }
    if (!kpi.metric) continue;
    const trend = widgets.find(
      (w) =>
        w !== kpi &&
        isTrendWidget(w) &&
        !w.error &&
        (w.section ?? "") === (kpi.section ?? "") &&
        (w.metric ?? "").toLowerCase() === kpi.metric!.toLowerCase(),
    );
    if (!trend) continue;
    const input = widgetChartInput(trend);
    const plan = planChart(input.rows, { kinds: input.kinds, measureLabels: input.labels, suggestion: "line" });
    const primary = plan.primary;
    if (!plan.timeKey || !primary || plan.seriesKey) continue;
    const dir = (n: number): KpiDelta["direction"] => (n === 0 ? "flat" : n > 0 ? "up" : "down");
    const arrow = (n: number) => (n > 0 ? "▲" : n < 0 ? "▼" : "■");
    if (plan.yoy) {
      const c = plan.yoy.change;
      out.set(kpi.id, {
        text: `${arrow(c)} ${Math.abs(c).toFixed(1)}${plan.yoy.unit} YoY`,
        sub: plan.yoy.basis,
        direction: dir(c),
      });
      continue;
    }
    const pts = plan.data.filter((d) => typeof d[primary] === "number");
    if (pts.length < 2) continue;
    const last = pts[pts.length - 1];
    const prev = pts[pts.length - 2];
    const lv = last[primary] as number;
    const pv = prev[primary] as number;
    if (pv === 0) continue;
    const c = ((lv - pv) / Math.abs(pv)) * 100;
    out.set(kpi.id, {
      text: `${arrow(c)} ${Math.abs(c).toFixed(1)}%`,
      sub: `${last.label} vs ${prev.label}`,
      direction: dir(c),
    });
  }
  return out;
}

type Finding = { id: string; section: string; text: string; score: number };

/** The most noteworthy chart headlines across the dashboard — outliers,
 *  big year-over-year moves and concentration first, routine "X leads"
 *  lines only when nothing else stands out. */
function dashboardFindings(widgets: DashboardWidget[], max = 7): Finding[] {
  const found: Finding[] = [];
  for (const w of widgets) {
    if ((w.type !== "chart" && w.type !== "pie") || w.error) continue;
    if (w.chart && ADVANCED_FORMS.has(w.chart)) continue;
    const input = widgetChartInput(w);
    if (input.rows.length < 2) continue;
    const plan = planChart(input.rows, {
      kinds: input.kinds,
      measureLabels: input.labels,
      suggestion: w.type === "pie" ? "pie" : w.chart ?? null,
    });
    if (plan.headline) {
      found.push({ id: w.id, section: displaySection(w.section ?? ""), text: plan.headline, score: plan.significance });
    }
  }
  // Data checks lead: a KPI whose value is implausible (targets off in
  // scale, output above capacity) must not be read as performance.
  for (const w of widgets) {
    if (w.type !== "kpi" || w.error || !w.data_warning) continue;
    found.push({ id: w.id, section: displaySection(w.section ?? ""), text: `Data check · ${w.title}: ${w.data_warning}`, score: 5 });
  }
  // Achievement against a plan or target far from 100% is itself a
  // headline finding (e.g. sales at 24% of target, or targets exceeded 2×).
  for (const w of widgets) {
    if (w.type !== "kpi" || w.error || w.data_warning || !/achievement/i.test(w.title)) continue;
    const v = asNumber(widgetValue(w));
    if (v == null) continue;
    const gap = v - 100;
    if (Math.abs(gap) < 5) continue;
    const text =
      gap < 0
        ? `${w.title} is ${v.toFixed(1)}% — ${Math.abs(gap).toFixed(1)} points short of plan.`
        : v >= 150
          ? `${w.title} is ${v.toFixed(1)}% — actuals are well above plan; the targets may be understated.`
          : `${w.title} is ${v.toFixed(1)}% — ${gap.toFixed(1)} points ahead of plan.`;
    found.push({ id: w.id, section: displaySection(w.section ?? ""), text, score: Math.abs(gap) >= 20 ? 4 : Math.abs(gap) >= 10 ? 3 : 2 });
  }
  found.sort((a, b) => b.score - a.score);
  const notable = found.filter((f) => f.score >= 1);
  return (notable.length ? notable : found).slice(0, max);
}

function KeyFindings({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) return null;
  return (
    <section className="insight-chart-card dashboard-findings" aria-label="Key findings">
      <header className="insight-chart-head">
        <h3 className="insight-chart-title">Key findings</h3>
        <p className="insight-chart-sub">Ranked by how unusual they are · computed from the data below</p>
      </header>
      <ul className="dashboard-findings-list">
        {findings.map((f) => (
          <li key={f.id} className={`dashboard-finding dashboard-finding-${f.score >= 3 ? "high" : f.score >= 2 ? "mid" : "low"}`}>
            {f.section ? <span className="dashboard-finding-section">{f.section}</span> : null}
            <span>{f.text}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Rows for SmartChart: the backend's canonical points (label/raw_value)
 *  become one category column + one measure column named after the widget,
 *  so tooltips and axes say "Revenue", not "value". */
function widgetChartInput(widget: DashboardWidget): {
  rows: Record<string, unknown>[];
  kinds: Record<string, string>;
  labels: Record<string, string>;
} {
  const kind = widgetValueKind(widget);
  if (widget.measure_labels && Object.keys(widget.measure_labels).length > 0) {
    // Several value columns on one axis (same unit, e.g. actual vs target):
    // use the raw rows so every series survives.
    const labels = widget.measure_labels;
    return {
      rows: widget.rows ?? [],
      kinds: kind ? Object.fromEntries(Object.keys(labels).map((k) => [k, kind])) : {},
      labels,
    };
  }
  // Name the measure after the metric ("Quantity Mt"), not the chart title
  // ("Quantity Mt Over Time"), so headlines and tooltips read naturally.
  const measureName = widget.metric || widget.title;
  const dim = widget.dimension ? widget.dimension.toLowerCase().replace(/\s+/g, "_") : "category";
  const points = widget.result?.points;
  if (points && points.length > 0) {
    return {
      rows: points.map((p) => ({ [dim]: p.label, value: p.raw_value })),
      kinds: kind ? { value: kind } : {},
      labels: { value: measureName },
    };
  }
  const raw = widget.rows ?? [];
  // Compiled widgets alias their columns category/value; restore the real
  // dimension name so the chart says "15 products", not "15 category values".
  const rows =
    dim !== "category" && raw.length > 0 && "category" in raw[0] && !isTrendWidget(widget)
      ? raw.map(({ category, ...rest }) => ({ [dim]: category, ...rest }))
      : raw;
  const hasValue = rows.length > 0 && "value" in rows[0];
  return {
    rows,
    kinds: kind && hasValue ? { value: kind } : {},
    labels: hasValue ? { value: measureName } : {},
  };
}

const ADVANCED_FORMS = new Set(["stacked", "waterfall", "funnel", "gauge"]);

const ChartWidget = memo(function ChartWidget({
  widget,
  onFilterClick,
}: {
  widget: DashboardWidget;
  onFilterClick?: (column: string, value: string | number) => void;
}) {
  const input = useMemo(() => widgetChartInput(widget), [widget]);
  const advanced = widget.chart && ADVANCED_FORMS.has(widget.chart) ? widget.chart : null;
  const data = useMemo(() => (advanced ? buildChartData(widget) : []), [advanced, widget]);

  if (widget.error) {
    return (
      <section className="insight-chart-card dashboard-chart-card">
        <header className="insight-chart-head">
          <h3 className="insight-chart-title">{widget.title}</h3>
        </header>
        <p className="dashboard-widget-error">{widget.error}</p>
      </section>
    );
  }

  if (advanced) {
    const valueKind = widgetValueKind(widget);
    return (
      <section className="insight-chart-card dashboard-chart-card">
        <header className="insight-chart-head">
          <h3 className="insight-chart-title">{widget.title}</h3>
          <p className="insight-chart-sub">
            {data.length.toLocaleString()} point{data.length === 1 ? "" : "s"}
          </p>
        </header>
        <div className="insight-chart-body">
          {data.length === 0 ? (
            <p className="viz-table-note">No data for this chart.</p>
          ) : advanced === "stacked" ? (
            <StackedBarComponent data={data} series={["value"]} seriesLabels={{ value: widget.title }} valueKind={valueKind} />
          ) : advanced === "waterfall" ? (
            <WaterfallComponent data={data} valueKind={valueKind} />
          ) : advanced === "funnel" ? (
            <FunnelComponent data={data} valueKind={valueKind} />
          ) : (
            <GaugeComponent
              value={data[0]?.value || 0}
              maxValue={Math.max(...data.map((d) => d.value))}
              label={widget.title}
            />
          )}
        </div>
      </section>
    );
  }

  const chartable = input.rows.length >= 2;
  const warning = widget.data_warning ? (
    <p className="dashboard-chart-warning" role="note">
      ⚠ {widget.data_warning}
    </p>
  ) : null;
  return (
    <div className="insight-chart-card dashboard-chart-card">
      {warning}
      {chartable ? (
        <SmartChart
          rows={input.rows}
          kinds={input.kinds}
          measureLabels={input.labels}
          suggestion={widget.type === "pie" ? "pie" : widget.chart ?? null}
          title={widget.title}
          compact
          onCategoryClick={onFilterClick}
        />
      ) : null}
      {!chartable ? (
        <>
          <header className="insight-chart-head">
            <h3 className="insight-chart-title">{widget.title}</h3>
          </header>
          <p className="viz-table-note">
            {input.rows.length === 1 ? "Only one data point — nothing to compare yet." : "No data for this chart."}
          </p>
        </>
      ) : null}
    </div>
  );
});

const TableWidget = memo(function TableWidget({
  widget,
  filters,
  onFilterClick,
}: {
  widget: DashboardWidget;
  filters: DashboardFilterState;
  onFilterClick?: (column: string, value: string | number) => void;
}) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [filterText, setFilterText] = useState("");

  const rows = widget.rows ?? [];
  if (rows.length === 0) {
    return <p className="viz-table-note">No data for this table.</p>;
  }

  const columns = Object.keys(rows[0]);
  const filtered = useMemo(() => {
    let result = applyFiltersToRows(rows, filters);

    if (filterText) {
      result = result.filter((row) =>
        columns.some((col) => String(row[col]).toLowerCase().includes(filterText.toLowerCase()))
      );
    }

    if (sortKey) {
      result = [...result].sort((a, b) => {
        const aVal = asNumber(a[sortKey]) ?? String(a[sortKey]);
        const bVal = asNumber(b[sortKey]) ?? String(b[sortKey]);
        const cmp = typeof aVal === "number" && typeof bVal === "number" ? aVal - bVal : String(aVal).localeCompare(String(bVal));
        return sortDir === "asc" ? cmp : -cmp;
      });
    }

    return result;
  }, [rows, filters, filterText, sortKey, sortDir, columns]);

  const handleExport = useCallback(() => {
    const csv = [
      columns.join(","),
      ...filtered.slice(0, 50).map((row) => columns.map((col) => JSON.stringify(row[col] ?? "")).join(",")),
    ].join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${widget.title}-${Date.now()}.csv`;
    a.click();
  }, [columns, filtered, widget.title]);

  return (
    <div className="viz-table-wrap">
      <div className="table-controls">
        <input
          type="text"
          placeholder="Filter rows..."
          value={filterText}
          onChange={(e) => setFilterText(e.target.value)}
          className="table-filter-input"
        />
        <button onClick={handleExport} className="table-export-btn" title="Export as CSV">
          📥 Export
        </button>
      </div>
      <table className="viz-table viz-table-live">
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col}
                onClick={() => {
                  setSortKey(sortKey === col ? null : col);
                  setSortDir("asc");
                }}
                style={{ cursor: "pointer", userSelect: "none" }}
              >
                {col} {sortKey === col ? (sortDir === "asc" ? "↓" : "↑") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {filtered.slice(0, 50).map((row, idx) => (
            <tr key={idx}>
              {columns.map((col) => (
                <td
                  key={col}
                  onClick={() => onFilterClick?.(col, row[col] as string | number)}
                  style={{ cursor: onFilterClick ? "pointer" : "default" }}
                  title={onFilterClick ? `Click to filter by this value` : ""}
                >
                  {formatValue(row[col])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {filtered.length > 50 ? (
        <p className="viz-table-note">Showing 50 of {filtered.length} filtered rows.</p>
      ) : null}
    </div>
  );
});

const TextWidget = memo(function TextWidget({ widget }: { widget: DashboardWidget }) {
  return (
    <section className="dashboard-text-widget">
      <h3 className="insight-chart-title">{widget.title}</h3>
      {widget.error ? (
        <p className="dashboard-widget-error" role="alert">
          {widget.error}
        </p>
      ) : (
        <p className="dashboard-text-body">{widget.content || "—"}</p>
      )}
    </section>
  );
});

const AiInsightWidget = memo(function AiInsightWidget({ widget }: { widget: DashboardWidget }) {
  const body = widget.content || "";
  const insufficient = widget.insight_status === "insufficient_data";

  return (
    <section className="dashboard-ai-insight">
      <h3 className="insight-chart-title">{widget.title}</h3>
      {widget.error ? (
        <p className="dashboard-widget-error" role="alert">
          {widget.error}
        </p>
      ) : body ? (
        <div
          className={
            insufficient
              ? "dashboard-ai-insight-body dashboard-ai-insight-muted"
              : "dashboard-ai-insight-body"
          }
        >
          <ChatMarkdown content={body} />
        </div>
      ) : (
        <p className="dashboard-ai-insight-body">Generating insights…</p>
      )}
    </section>
  );
});

const WidgetCell = memo(function WidgetCell({
  widget,
  filters,
  onFilterClick,
}: {
  widget: DashboardWidget;
  filters: DashboardFilterState;
  onFilterClick?: (column: string, value: string | number) => void;
}) {
  const style = gridStyle(widget);

  let content: ReactNode;
  switch (widget.type) {
    case "kpi":
      content = <KpiTile widget={widget} />;
      break;
    case "table":
      content = (
        <section className="insight-chart-card dashboard-chart-card">
          <header className="insight-chart-head">
            <h3 className="insight-chart-title">{widget.title}</h3>
          </header>
          <div className="insight-chart-body insight-chart-body-scroll">
            {widget.error ? (
              <p className="dashboard-widget-error" role="alert">
                {widget.error}
              </p>
            ) : (
              <TableWidget widget={widget} filters={filters} onFilterClick={onFilterClick} />
            )}
          </div>
        </section>
      );
      break;
    case "pie":
      content = <ChartWidget widget={{ ...widget, chart: "pie" }} onFilterClick={onFilterClick} />;
      break;
    case "text":
      content = <TextWidget widget={widget} />;
      break;
    case "ai_insight":
      content = <AiInsightWidget widget={widget} />;
      break;
    case "chart":
    default:
      content = <ChartWidget widget={widget} onFilterClick={onFilterClick} />;
      break;
  }

  return (
    <div className="dashboard-grid-cell" style={style}>
      {content}
    </div>
  );
});

/** Business reading order for whole-database sections; unknown sections
 *  follow alphabetically, cross-table ratios last. */
const SECTION_ORDER = [
  "overview",
  "sales",
  "production",
  "dispatch",
  "inventory",
  "procurement",
  "ar",
  "receivables",
  "energy",
  "maintenance",
];

const SECTION_LABELS: Record<string, string> = {
  ar: "Receivables",
  "cross-domain": "Plan, targets & unit costs",
};

function displaySection(section: string): string {
  return SECTION_LABELS[section.toLowerCase()] ?? section;
}

function sectionRank(section: string): number {
  const key = section.toLowerCase();
  if (key === "cross-domain") return SECTION_ORDER.length + 1;
  const i = SECTION_ORDER.indexOf(key);
  return i >= 0 ? i : SECTION_ORDER.length;
}

/** A chart of a metric whose KPI failed a data check (e.g. targets off in
 *  scale) carries the same warning, so "23% by product" isn't read as
 *  performance either. */
function withChartWarnings(widgets: DashboardWidget[]): DashboardWidget[] {
  const byMetric = new Map<string, string>();
  for (const w of widgets) {
    if (w.type === "kpi" && w.data_warning && w.metric) byMetric.set(w.metric.toLowerCase(), w.data_warning);
  }
  if (byMetric.size === 0) return widgets;
  return widgets.map((w) => {
    if (w.type === "kpi" || w.data_warning || !w.metric) return w;
    const metric = w.metric.toLowerCase();
    const warning =
      byMetric.get(metric) ??
      // Target trends name the subject ("Sales vs target"); match the KPI's subject.
      [...byMetric.entries()].find(([k]) => metric.endsWith("vs target") && k.startsWith(metric.replace(/ vs target$/, "")))?.[1];
    return warning ? { ...w, data_warning: warning } : w;
  });
}

function groupWidgetsBySection(
  widgets: DashboardWidget[],
): { section: string; widgets: DashboardWidget[] }[] {
  const order: string[] = [];
  const groups = new Map<string, DashboardWidget[]>();
  for (const widget of widgets) {
    const key = widget.section || "";
    if (!groups.has(key)) {
      order.push(key);
      groups.set(key, []);
    }
    groups.get(key)!.push(widget);
  }
  return order
    .filter((key) => key !== "")
    .map((key) => ({ section: key, widgets: groups.get(key)! }))
    .concat(
      groups.has("") ? [{ section: "", widgets: groups.get("")! }] : [],
    );
}

const DashboardSection = memo(function DashboardSection({
  widgets,
  filters,
  onFilterClick,
  excludeIds,
  deltas,
}: {
  widgets: DashboardWidget[];
  filters: DashboardFilterState;
  onFilterClick?: (column: string, value: string | number) => void;
  /** KPIs already shown in the dashboard's key-metrics strip. */
  excludeIds?: Set<string>;
  /** Change per KPI id (vs prior period), shared with the strip. */
  deltas?: Map<string, KpiDelta>;
}) {
  const kpis = useMemo(
    () => widgets.filter((w) => w.type === "kpi" && !excludeIds?.has(w.id)),
    [widgets, excludeIds],
  );
  const charts = useMemo(() => widgets.filter((w) => w.type === "chart" || w.type === "pie"), [widgets]);
  const other = useMemo(() => widgets.filter((w) => !["kpi", "chart", "pie"].includes(w.type)), [widgets]);

  return (
    <>
      {kpis.length > 0 ? (
        <div className={`insight-kpi-row dashboard-kpi-row insight-kpi-count-${Math.min(kpis.length, 4)}`}>
          {kpis.map((widget) => (
            <KpiTile key={widget.id} widget={widget} delta={deltas?.get(widget.id)} />
          ))}
        </div>
      ) : null}
      {charts.length > 0 ? (
        <div className="dashboard-charts">
          {charts.map((widget) => (
            <ChartWidget key={widget.id} widget={widget} onFilterClick={onFilterClick} />
          ))}
        </div>
      ) : null}
      {other.map((widget) => (
        <WidgetCell key={widget.id} widget={widget} filters={filters} onFilterClick={onFilterClick} />
      ))}
    </>
  );
});

/**
 * First non-transparent background from `el` up to <html>, as a hex string
 * (jsPDF only accepts hex/named colors, and rejects transparent ones).
 */
function resolveOpaqueBackground(el: HTMLElement): string {
  const ctx = document.createElement("canvas").getContext("2d");
  for (let cur: HTMLElement | null = el; cur; cur = cur.parentElement) {
    const color = getComputedStyle(cur).backgroundColor;
    if (!color || !ctx) continue;
    ctx.fillStyle = "#000";
    ctx.fillStyle = color;
    // Canvas serializes opaque colors as #rrggbb and translucent ones as rgba(...).
    if (typeof ctx.fillStyle === "string" && ctx.fillStyle.startsWith("#")) return ctx.fillStyle;
  }
  return "#ffffff";
}

export function DashboardGrid({ dashboard, queryLabel, error = null }: Props) {
  const [filters, setFilters] = useState<DashboardFilterState>({});

  const handleFilterClick = useCallback((column: string, value: string | number) => {
    setFilters((prev) => ({
      ...prev,
      [column]: prev[column] === value ? null : value,
    }));
  }, []);

  const clearFilters = useCallback(() => {
    setFilters({});
  }, []);

  const widgets = useMemo(() => withChartWarnings(dashboard.widgets), [dashboard.widgets]);
  const useGrid = hasExplicitGridLayout(widgets);
  const layoutWidgets = useGrid ? withSafePositions(widgets) : widgets;
  const hasSections = widgets.some((w) => w.section);
  const kpis = useMemo(() => widgets.filter((w) => w.type === "kpi"), [widgets]);
  const charts = useMemo(() => widgets.filter((w) => w.type === "chart" || w.type === "pie"), [widgets]);
  const deltas = useMemo(() => kpiDeltas(widgets), [widgets]);
  // Key metrics strip: cross-table KPIs and each section's headline — not
  // every KPI on the board (the rest stay in their sections).
  const stripKpis = useMemo(() => {
    if (!hasSections) return kpis;
    // An executive overview, when the dashboard has one, is the strip.
    const overview = kpis.filter((w) => w.purpose === "executive_kpi");
    if (overview.length > 0) return overview;
    const picked: DashboardWidget[] = [];
    // At most 4 cross-table KPIs, so every section's headline still fits.
    for (const w of kpis) {
      if (w.section === "Cross-domain" && w.purpose === "business_kpi" && picked.length < 4) picked.push(w);
    }
    const seenSections = new Set<string>();
    for (const w of kpis) {
      const sec = w.section ?? "";
      if (w.section === "Cross-domain" || seenSections.has(sec)) continue;
      if (w.purpose === "headline_measure" || w.purpose == null) {
        picked.push(w);
        seenSections.add(sec);
      }
    }
    return picked.slice(0, 8);
  }, [kpis, hasSections]);
  const stripIds = useMemo(() => new Set(stripKpis.map((w) => w.id)), [stripKpis]);
  const findings = useMemo(() => dashboardFindings(widgets), [widgets]);

  // Story order: key metrics (with change) → key findings → detail.
  const keyMetrics =
    stripKpis.length > 0 ? (
      <section className="dashboard-key-metrics" aria-label="Key metrics">
        <div className={`insight-kpi-row dashboard-kpi-row insight-kpi-count-${Math.min(stripKpis.length, 4)}`}>
          {stripKpis.map((widget) => (
            <KpiTile
              key={widget.id}
              widget={widget}
              delta={deltas.get(widget.id)}
              label={
                hasSections && widget.section && widget.purpose !== "executive_kpi"
                  ? `${displaySection(widget.section)} · ${widget.title}`
                  : undefined
              }
            />
          ))}
        </div>
      </section>
    ) : null;

  const dashboardRef = useRef<HTMLDivElement>(null);
  const [exporting, setExporting] = useState(false);

  const handleExportDashboard = useCallback(async () => {
    const node = dashboardRef.current;
    if (!node || exporting) return;
    setExporting(true);
    try {
      // Loaded on demand: both libraries are heavy and only needed here.
      const [{ default: html2canvas }, { jsPDF }] = await Promise.all([
        import("html2canvas-pro"),
        import("jspdf"),
      ]);
      const background = resolveOpaqueBackground(node);
      const canvas = await html2canvas(node, {
        scale: 2,
        useCORS: true,
        backgroundColor: background,
        // The export button itself shouldn't appear in the document.
        ignoreElements: (el) => el.classList.contains("dashboard-export-btn"),
      });

      const pdf = new jsPDF({ orientation: "portrait", unit: "pt", format: "a4" });
      const margin = 24;
      const pageWidth = pdf.internal.pageSize.getWidth() - margin * 2;
      const pageHeight = pdf.internal.pageSize.getHeight() - margin * 2;
      // Slice the capture into page-sized strips so tall dashboards paginate.
      const sliceHeightPx = Math.floor((pageHeight * canvas.width) / pageWidth);
      const slice = document.createElement("canvas");
      slice.width = canvas.width;
      const ctx = slice.getContext("2d");
      if (!ctx) throw new Error("Canvas unavailable");

      for (let y = 0, page = 0; y < canvas.height; y += sliceHeightPx, page++) {
        const h = Math.min(sliceHeightPx, canvas.height - y);
        slice.height = h;
        ctx.fillStyle = background;
        ctx.fillRect(0, 0, slice.width, h);
        ctx.drawImage(canvas, 0, y, canvas.width, h, 0, 0, canvas.width, h);
        if (page > 0) pdf.addPage();
        pdf.setFillColor(background);
        pdf.rect(0, 0, pdf.internal.pageSize.getWidth(), pdf.internal.pageSize.getHeight(), "F");
        pdf.addImage(slice.toDataURL("image/png"), "PNG", margin, margin, pageWidth, (h * pageWidth) / canvas.width);
      }

      pdf.setProperties({ title: dashboard.title || "Dashboard" });
      pdf.save(`${dashboard.title || "dashboard"}-${Date.now()}.pdf`);
    } catch (err) {
      console.error("Dashboard PDF export failed", err);
      window.alert("Could not export the dashboard as PDF. Please try again.");
    } finally {
      setExporting(false);
    }
  }, [dashboard.title, exporting]);

  const hasActiveFilters = Object.values(filters).some((v) => v !== null);

  return (
    <div className="insight-dashboard dashboard-grid" ref={dashboardRef}>
      <header className="insight-header">
        <div>
          <p className="insight-kicker">Dashboard</p>
          <h2 className="insight-title">{dashboard.title || "Insight Dashboard"}</h2>
          {queryLabel ? <p className="insight-query">{queryLabel}</p> : null}
          {dashboard.filters && dashboard.filters.length > 0 ? (
            <div className="dashboard-filter-bar" aria-label="Active filters">
              {dashboard.filters.map((filter) => (
                <span key={filter.field} className="dashboard-filter-chip">
                  {filter.field} = {String(filter.value ?? "")}
                </span>
              ))}
            </div>
          ) : null}
          {hasActiveFilters ? (
            <div className="dashboard-cross-filters">
              <p className="dashboard-cross-filter-label">Active Cross-Filters:</p>
              {Object.entries(filters).map(([col, val]) => val && (
                <span
                  key={col}
                  className="dashboard-cross-filter-chip"
                  onClick={() => handleFilterClick(col, val)}
                >
                  {col} = {val} ✕
                </span>
              ))}
              <button onClick={clearFilters} className="dashboard-clear-filters">
                Clear All
              </button>
            </div>
          ) : null}
        </div>
        <button
          onClick={handleExportDashboard}
          className="dashboard-export-btn"
          title="Export dashboard as PDF"
          disabled={exporting}
        >
          {exporting ? "Exporting…" : "📊 Export PDF"}
        </button>
      </header>

      {error ? (
        <div className="insight-empty" role="alert">
          <h3>Dashboard error</h3>
          <p>{error}</p>
        </div>
      ) : null}

      {widgets.length === 0 ? (
        <div className="insight-empty">
          <h3>No widgets</h3>
          <p>Could not build dashboard widgets from the discovered schema.</p>
        </div>
      ) : useGrid ? (
        <div
          className="dashboard-grid-layout"
          style={{
            gridTemplateColumns: `repeat(${dashboard.layout?.cols ?? GRID_COLS}, minmax(0, 1fr))`,
          }}
        >
          {layoutWidgets.map((widget) => (
            <WidgetCell key={widget.id} widget={widget} filters={filters} onFilterClick={handleFilterClick} />
          ))}
        </div>
      ) : hasSections ? (
        <>
          {keyMetrics}
          <KeyFindings findings={findings} />
          {groupWidgetsBySection(widgets)
            // Domains in business reading order; the overview is the strip
            // above, so its KPIs don't repeat as a section.
            .filter(({ widgets }) => widgets.some((w) => !stripIds.has(w.id)))
            .sort((a, b) => sectionRank(a.section) - sectionRank(b.section))
            .map(({ section, widgets }) =>
              section ? (
                <section key={section} className="dashboard-domain-section">
                  <h3 className="dashboard-domain-heading">{displaySection(section)}</h3>
                  <DashboardSection
                    widgets={widgets}
                    filters={filters}
                    onFilterClick={handleFilterClick}
                    excludeIds={stripIds}
                    deltas={deltas}
                  />
                </section>
              ) : (
                <DashboardSection
                  key="_unsectioned"
                  widgets={widgets}
                  filters={filters}
                  onFilterClick={handleFilterClick}
                  excludeIds={stripIds}
                  deltas={deltas}
                />
              ),
            )}
        </>
      ) : (
        <>
          {keyMetrics}
          <KeyFindings findings={findings} />

          {charts.length > 0 ? (
            <div className="dashboard-charts">
              {charts.map((widget) => (
                <ChartWidget key={widget.id} widget={widget} onFilterClick={handleFilterClick} />
              ))}
            </div>
          ) : null}

          {widgets
            .filter((w) => !["kpi", "chart", "pie"].includes(w.type))
            .map((widget) => (
              <WidgetCell key={widget.id} widget={widget} filters={filters} onFilterClick={handleFilterClick} />
            ))}
        </>
      )}
    </div>
  );
}
