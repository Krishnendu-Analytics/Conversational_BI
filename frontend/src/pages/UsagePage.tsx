import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  ApiError,
  getAiUsage,
  type UsagePoint,
  type UsageRange,
  type UsageReport,
  type UsageScope,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatCompact } from "@/lib/chartModel";

const PRESETS: { key: Exclude<UsageRange, "custom">; label: string }[] = [
  { key: "1h", label: "1h" },
  { key: "3h", label: "3h" },
  { key: "12h", label: "12h" },
  { key: "1d", label: "1d" },
  { key: "1w", label: "1w" },
  { key: "30d", label: "30d" },
];

const LINE = "var(--chart-series-1)";
const GRID = "var(--chart-grid)";
const AXIS = "var(--chart-axis)";
const TICK = { fontSize: 11, fill: "var(--text-muted)" };
const AUTO_REFRESH_MS = 60_000;

type Metric = "tokens" | "input_tokens" | "output_tokens" | "requests";

const OVERVIEW: { metric: Metric; title: string; unit: string }[] = [
  { metric: "tokens", title: "Total tokens", unit: "Tokens" },
  { metric: "input_tokens", title: "Input tokens", unit: "Tokens" },
  { metric: "output_tokens", title: "Output tokens", unit: "Tokens" },
  { metric: "requests", title: "AI requests", unit: "Count" },
];

function formatFull(n: number): string {
  return n.toLocaleString();
}

/** Axis ticks in one unit per axis (7K next to 14K, never 7,000). */
function formatAxis(n: number): string {
  const abs = Math.abs(n);
  const trim = (x: number) => String(Number(x.toFixed(1)));
  if (abs >= 1_000_000) return `${trim(n / 1_000_000)}M`;
  if (abs >= 1_000) return `${trim(n / 1_000)}K`;
  return String(Math.round(n));
}

/** Axis tick for a bucket start, sized to the bucket spacing. */
function tickFormatter(bucketSeconds: number) {
  return (iso: string) => {
    const d = new Date(iso);
    if (bucketSeconds >= 86400) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    if (bucketSeconds >= 21600) {
      return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
    }
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  };
}

function bucketTitle(iso: string, bucketSeconds: number): string {
  const d = new Date(iso);
  const end = new Date(d.getTime() + bucketSeconds * 1000);
  const day = d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  if (bucketSeconds >= 86400) return day;
  const time = (x: Date) => x.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${day}, ${time(d)}–${time(end)}`;
}

/** ``<input type="datetime-local">`` value for a Date, in local time. */
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function CalendarIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="3.5" y="5" width="17" height="15" rx="2" stroke="currentColor" strokeWidth="1.75" />
      <path d="M3.5 10h17M8 3v4M16 3v4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  );
}

function UsageTooltip({
  active,
  payload,
  bucketSeconds,
  unit,
}: {
  active?: boolean;
  payload?: { value?: number; payload?: UsagePoint }[];
  bucketSeconds: number;
  unit: string;
}) {
  if (!active || !payload?.length || !payload[0].payload) return null;
  const point = payload[0].payload;
  const value = Number(payload[0].value ?? 0);
  return (
    <div className="chart-tip" role="status">
      <p className="chart-tip-title">{bucketTitle(point.t, bucketSeconds)}</p>
      <p className="chart-tip-row">
        <span className="chart-tip-key" style={{ background: LINE }} aria-hidden />
        <strong className="chart-tip-value">{formatFull(value)}</strong>
        <span className="chart-tip-name">{unit.toLowerCase()}</span>
      </p>
    </div>
  );
}

function UsageChartCard({
  title,
  unit,
  metric,
  points,
  bucketSeconds,
}: {
  title: string;
  unit: string;
  metric: Metric;
  points: UsagePoint[];
  bucketSeconds: number;
}) {
  const total = points.reduce((sum, p) => sum + p[metric], 0);
  return (
    <section className="usage-card" aria-label={`${title}: ${formatFull(total)} ${unit.toLowerCase()}`}>
      <header className="usage-card-header">
        <h3 className="usage-card-title" title={title}>
          {title}
        </h3>
        <span className="usage-card-total" title={formatFull(total)}>
          {formatCompact(total)}
        </span>
      </header>
      <p className="usage-card-unit">{unit}</p>
      <div className="usage-card-plot">
        <ResponsiveContainer width="100%" height={150}>
          <LineChart data={points} margin={{ top: 6, right: 10, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis
              dataKey="t"
              tickFormatter={tickFormatter(bucketSeconds)}
              tick={TICK}
              axisLine={{ stroke: AXIS }}
              tickLine={false}
              minTickGap={28}
            />
            <YAxis
              tickFormatter={formatAxis}
              tick={TICK}
              width={44}
              axisLine={false}
              tickLine={false}
              allowDecimals={false}
            />
            <Tooltip
              cursor={{ stroke: "var(--text-muted)", strokeWidth: 1 }}
              content={<UsageTooltip bucketSeconds={bucketSeconds} unit={unit} />}
            />
            <Line
              type="linear"
              dataKey={metric}
              name={title}
              stroke={LINE}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4, fill: LINE, stroke: "var(--surface)", strokeWidth: 2 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}

/** Token usage over time — the user's own, or the organization's for admins. */
export function UsagePage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [range, setRange] = useState<UsageRange>("3h");
  const [custom, setCustom] = useState<{ start: string; end: string } | null>(null);
  const [draft, setDraft] = useState(() => ({
    start: toLocalInput(new Date(Date.now() - 24 * 3600 * 1000)),
    end: toLocalInput(new Date()),
  }));
  const [pickerOpen, setPickerOpen] = useState(false);
  const [scope, setScope] = useState<UsageScope>(isAdmin ? "organization" : "me");
  const [report, setReport] = useState<UsageReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const pickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setScope(isAdmin ? "organization" : "me");
  }, [isAdmin]);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const next = await getAiUsage(
          {
            range,
            scope,
            ...(range === "custom" && custom ? custom : {}),
          },
          signal,
        );
        setReport(next);
        setError(null);
        setUpdatedAt(new Date());
      } catch (err) {
        if (signal?.aborted) return;
        setError(err instanceof ApiError ? err.message : "Could not load AI usage");
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [range, scope, custom],
  );

  useEffect(() => {
    if (range === "custom" && !custom) return;
    const controller = new AbortController();
    void load(controller.signal);
    if (range === "custom") return () => controller.abort();
    const timer = window.setInterval(() => void load(), AUTO_REFRESH_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [load, range, custom]);

  useEffect(() => {
    if (!pickerOpen) return;
    function onPointer(e: PointerEvent) {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) setPickerOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setPickerOpen(false);
    }
    window.addEventListener("pointerdown", onPointer);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onPointer);
      window.removeEventListener("keydown", onKey);
    };
  }, [pickerOpen]);

  const draftError = useMemo(() => {
    const s = new Date(draft.start).getTime();
    const e = new Date(draft.end).getTime();
    if (!Number.isFinite(s) || !Number.isFinite(e)) return "Pick a start and an end";
    if (e <= s) return "End must be after start";
    if (e - s > 92 * 86400 * 1000) return "At most 92 days";
    return null;
  }, [draft]);

  function applyCustom() {
    if (draftError) return;
    setCustom({
      start: new Date(draft.start).toISOString(),
      end: new Date(draft.end).toISOString(),
    });
    setRange("custom");
    setPickerOpen(false);
  }

  const bucketSeconds = report?.bucket_seconds ?? 300;
  const customLabel =
    range === "custom" && custom
      ? `${new Date(custom.start).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })} – ${new Date(custom.end).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`
      : null;
  const hasAnyUsage = (report?.all_time.tokens ?? 0) > 0;

  return (
    <div className="usage-page">
      <header className="usage-page-header">
        <div>
          <p className="page-kicker">Workspace</p>
          <h1 className="page-title">AI usage</h1>
          <p className="page-lede">
            Tokens used by chat, schema discovery, semantic modelling and every other AI feature
            {report?.scope === "organization" ? " across your organization" : " by you"}.
          </p>
        </div>
      </header>

      <div className="usage-filters" role="toolbar" aria-label="Usage filters">
        <div className="seg usage-range" role="group" aria-label="Time range">
          {PRESETS.map((p) => (
            <button
              key={p.key}
              type="button"
              className="seg-btn"
              aria-pressed={range === p.key}
              onClick={() => setRange(p.key)}
            >
              {p.label}
            </button>
          ))}
          <div className="usage-picker-anchor" ref={pickerRef}>
            <button
              type="button"
              className="seg-btn usage-calendar-btn"
              aria-pressed={range === "custom"}
              aria-expanded={pickerOpen}
              aria-label="Custom range"
              title="Custom range"
              onClick={() => setPickerOpen((open) => !open)}
            >
              <CalendarIcon />
              {customLabel ? <span className="usage-custom-label">{customLabel}</span> : null}
            </button>
            {pickerOpen ? (
              <div className="usage-picker" role="dialog" aria-label="Custom range">
                <label className="field">
                  <span>From</span>
                  <input
                    type="datetime-local"
                    value={draft.start}
                    max={draft.end}
                    onChange={(e) => setDraft((d) => ({ ...d, start: e.target.value }))}
                  />
                </label>
                <label className="field">
                  <span>To</span>
                  <input
                    type="datetime-local"
                    value={draft.end}
                    min={draft.start}
                    onChange={(e) => setDraft((d) => ({ ...d, end: e.target.value }))}
                  />
                </label>
                {draftError ? <p className="form-hint usage-picker-error">{draftError}</p> : null}
                <div className="usage-picker-actions">
                  <button type="button" className="btn btn-ghost" onClick={() => setPickerOpen(false)}>
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={applyCustom}
                    disabled={Boolean(draftError)}
                  >
                    Apply
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </div>

        {isAdmin ? (
          <div className="seg" role="group" aria-label="Whose usage">
            <button
              type="button"
              className="seg-btn"
              aria-pressed={scope === "organization"}
              onClick={() => setScope("organization")}
            >
              Organization
            </button>
            <button
              type="button"
              className="seg-btn"
              aria-pressed={scope === "me"}
              onClick={() => setScope("me")}
            >
              Just me
            </button>
          </div>
        ) : null}

        <div className="usage-filters-end">
          {updatedAt ? (
            <span className="usage-updated">
              Updated {updatedAt.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
            </span>
          ) : null}
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => void load()}
            disabled={loading || (range === "custom" && !custom)}
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}

      {report ? (
        <div className={loading ? "usage-body is-refreshing" : "usage-body"} aria-busy={loading}>
          <div className="usage-stats">
            <div className="usage-stat">
              <span className="usage-stat-label">Tokens in range</span>
              <strong className="usage-stat-value" title={formatFull(report.totals.tokens)}>
                {formatCompact(report.totals.tokens)}
              </strong>
              <span className="usage-stat-sub">
                {formatCompact(report.totals.input_tokens)} input · {formatCompact(report.totals.output_tokens)} output
              </span>
            </div>
            <div className="usage-stat">
              <span className="usage-stat-label">AI requests in range</span>
              <strong className="usage-stat-value" title={formatFull(report.totals.requests)}>
                {formatCompact(report.totals.requests)}
              </strong>
            </div>
            <div className="usage-stat">
              <span className="usage-stat-label">Total tokens · all time</span>
              <strong className="usage-stat-value" title={formatFull(report.all_time.tokens)}>
                {formatCompact(report.all_time.tokens)}
              </strong>
              <span className="usage-stat-sub">
                {formatCompact(report.all_time.input_tokens)} input · {formatCompact(report.all_time.output_tokens)} output
              </span>
            </div>
            <div className="usage-stat">
              <span className="usage-stat-label">Total requests · all time</span>
              <strong className="usage-stat-value" title={formatFull(report.all_time.requests)}>
                {formatCompact(report.all_time.requests)}
              </strong>
            </div>
          </div>

          {!hasAnyUsage ? (
            <p className="usage-empty">
              No AI usage recorded yet. Ask a question in chat or discover a connection and it will show up here within a
              few seconds.
            </p>
          ) : null}

          <h2 className="usage-section-title">Overall</h2>
          <div className="usage-grid">
            {OVERVIEW.map((item) => (
              <UsageChartCard
                key={item.metric}
                title={item.title}
                unit={item.unit}
                metric={item.metric}
                points={report.series}
                bucketSeconds={bucketSeconds}
              />
            ))}
          </div>

          {report.features.length > 0 ? (
            <>
              <h2 className="usage-section-title">By feature</h2>
              <div className="usage-grid">
                {report.features.map((f) => (
                  <UsageChartCard
                    key={f.feature}
                    title={f.label}
                    unit="Tokens"
                    metric="tokens"
                    points={f.series}
                    bucketSeconds={bucketSeconds}
                  />
                ))}
              </div>

              <div className="usage-tables">
                <section className="usage-table-card">
                  <h3 className="usage-card-title">Feature breakdown</h3>
                  <table className="usage-table">
                    <thead>
                      <tr>
                        <th scope="col">Feature</th>
                        <th scope="col" className="num">Tokens</th>
                        <th scope="col" className="num">Requests</th>
                        <th scope="col" className="num">Share</th>
                      </tr>
                    </thead>
                    <tbody>
                      {report.features.map((f) => (
                        <tr key={f.feature}>
                          <td>{f.label}</td>
                          <td className="num">{formatFull(f.tokens)}</td>
                          <td className="num">{formatFull(f.requests)}</td>
                          <td className="num">
                            {report.totals.tokens
                              ? `${((f.tokens / report.totals.tokens) * 100).toFixed(1)}%`
                              : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </section>

                {report.users && report.users.length > 0 ? (
                  <section className="usage-table-card">
                    <h3 className="usage-card-title">By user</h3>
                    <table className="usage-table">
                      <thead>
                        <tr>
                          <th scope="col">User</th>
                          <th scope="col" className="num">Tokens</th>
                          <th scope="col" className="num">Requests</th>
                        </tr>
                      </thead>
                      <tbody>
                        {report.users.map((u) => (
                          <tr key={u.user_id}>
                            <td className="usage-table-email" title={u.email}>
                              {u.email}
                            </td>
                            <td className="num">{formatFull(u.tokens)}</td>
                            <td className="num">{formatFull(u.requests)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </section>
                ) : null}
              </div>
            </>
          ) : null}
        </div>
      ) : !error ? (
        <p className="loading-copy">Loading usage…</p>
      ) : null}
    </div>
  );
}
