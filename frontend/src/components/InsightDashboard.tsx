/**
 * Chat visualization panel: insight tiles, the answer's chart, and the
 * result table. Chart choice, ranking and headline come from
 * lib/chartModel via SmartChart, shared with dashboards.
 */

import { useMemo, useState } from "react";
import { SmartChart } from "@/components/SmartChart";
import {
  asNumber,
  buildKpiFacts,
  columnNames,
  formatCell,
  humanize,
  planChart,
  type Row,
} from "@/lib/chartModel";

type Props = {
  title?: string;
  queryLabel?: string;
  rows?: Row[];
  chartSuggestion?: string | null;
  /** Measure kind per column from the backend (derived from the semantic
   *  layer and the executed SQL). */
  columnKinds?: Record<string, string>;
  error?: string | null;
};

const MAX_TABLE_ROWS = 50;
const NO_KINDS: Record<string, string> = {};

function ResultTable({ rows, columns }: { rows: Row[]; columns: string[] }) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [filterText, setFilterText] = useState("");

  let filtered = rows;

  if (filterText) {
    const needle = filterText.toLowerCase();
    filtered = rows.filter((row) => columns.some((col) => String(row[col]).toLowerCase().includes(needle)));
  }

  if (sortKey) {
    filtered = [...filtered].sort((a, b) => {
      const aVal = asNumber(a[sortKey]) ?? String(a[sortKey]);
      const bVal = asNumber(b[sortKey]) ?? String(b[sortKey]);
      const cmp =
        typeof aVal === "number" && typeof bVal === "number"
          ? aVal - bVal
          : String(aVal).localeCompare(String(bVal), undefined, { numeric: true });
      return sortDir === "asc" ? cmp : -cmp;
    });
  }

  const shown = filtered.slice(0, MAX_TABLE_ROWS);
  const truncated = filtered.length > MAX_TABLE_ROWS;

  const handleExport = () => {
    // Export every filtered row, not just the rows visible on screen.
    const csv = [
      columns.join(","),
      ...filtered.map((row) => columns.map((col) => JSON.stringify(row[col] ?? "")).join(",")),
    ].join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `data-export-${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

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
                  if (sortKey === col) {
                    setSortDir(sortDir === "asc" ? "desc" : "asc");
                  } else {
                    setSortKey(col);
                    setSortDir("desc");
                  }
                }}
                style={{ cursor: "pointer", userSelect: "none" }}
              >
                {humanize(col)} {sortKey === col ? (sortDir === "asc" ? "↑" : "↓") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, idx) => (
            <tr key={idx}>
              {columns.map((col) => (
                <td key={col}>{formatCell(row[col], col)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {truncated ? (
        <p className="viz-table-note">
          Showing first {MAX_TABLE_ROWS.toLocaleString()} of {filtered.length.toLocaleString()} rows
        </p>
      ) : null}
    </div>
  );
}

export function InsightDashboard({
  title = "Insight",
  queryLabel,
  rows = [],
  chartSuggestion = null,
  columnKinds = NO_KINDS,
  error = null,
}: Props) {
  const columns = useMemo(() => columnNames(rows), [rows]);
  // Default plan (no view overrides) drives the insight tiles and whether a
  // chart exists at all; SmartChart re-plans when the user changes the view.
  const plan = useMemo(
    () => planChart(rows, { kinds: columnKinds, suggestion: chartSuggestion }),
    [rows, columnKinds, chartSuggestion],
  );
  const kpis = useMemo(() => buildKpiFacts(rows, plan, columnKinds), [rows, plan, columnKinds]);

  const hasData = rows.length > 0;
  const showChart = hasData && plan.form !== "none";

  return (
    <div className="insight-dashboard">
      <header className="insight-header">
        <div>
          <p className="insight-kicker">Visualization</p>
          <h2 className="insight-title">{title}</h2>
          {queryLabel ? <p className="insight-query">{queryLabel}</p> : null}
        </div>
      </header>

      {error ? (
        <div className="insight-empty" role="alert">
          <h3>Query error</h3>
          <p>{error}</p>
        </div>
      ) : null}

      {!error && !hasData ? (
        <div className="insight-empty">
          <h3>Nothing to chart</h3>
          <p>No result rows for this answer. The chat still reflects what the analyst found.</p>
        </div>
      ) : null}

      {hasData ? (
        <>
          {kpis.length > 0 ? (
            <div className={`insight-kpi-row insight-kpi-count-${Math.min(kpis.length, 4)}`}>
              {kpis.map((kpi) => (
                <div key={kpi.id} className="insight-kpi">
                  <p className="insight-kpi-label">{kpi.label}</p>
                  <p className={`insight-kpi-value${kpi.text ? " insight-kpi-value-text" : ""}`} title={kpi.value}>
                    {kpi.value}
                  </p>
                  {kpi.sub ? (
                    <p className={`insight-kpi-delta insight-kpi-delta-${kpi.direction ?? "flat"}`}>{kpi.sub}</p>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}

          <div className={`insight-chart-grid${showChart ? "" : " insight-chart-grid-single"}`}>
            {showChart ? (
              <div className="insight-chart-card insight-chart-wide">
                <SmartChart
                  // A new answer starts from its own default view, not the
                  // previous answer's chart-type / measure / sort choices.
                  key={`${queryLabel ?? ""}|${columns.join(",")}|${rows.length}`}
                  rows={rows}
                  kinds={columnKinds}
                  suggestion={chartSuggestion}
                />
              </div>
            ) : null}

            <section className={`insight-chart-card ${showChart ? "insight-chart-narrow" : "insight-chart-full"}`}>
              <header className="insight-chart-head">
                <h3 className="insight-chart-title">Result rows</h3>
                <p className="insight-chart-sub">
                  {rows.length.toLocaleString()} row{rows.length === 1 ? "" : "s"}
                  {columns.length ? ` · ${columns.length} column${columns.length === 1 ? "" : "s"}` : ""}
                </p>
              </header>
              <div className="insight-chart-body insight-chart-body-scroll">
                {columns.length > 0 ? (
                  <ResultTable rows={rows} columns={columns} />
                ) : (
                  <p className="viz-table-note">Rows had no columns to display.</p>
                )}
              </div>
            </section>
          </div>
        </>
      ) : null}
    </div>
  );
}
