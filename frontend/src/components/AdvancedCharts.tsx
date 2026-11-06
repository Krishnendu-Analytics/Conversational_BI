/**
 * Special-purpose chart forms for dashboards: Stacked Bar, Waterfall,
 * Funnel, Gauge. Everyday forms (bar / line / area / donut / scatter) live
 * in SmartChart; these share its formatting, tooltip language and marks.
 */

import { memo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { CHART_COLORS, formatMeasure, humanize, truncate } from "@/lib/chartModel";

const GRID = "var(--chart-grid)";
const AXIS = "var(--chart-axis)";
const TICK = { fontSize: 11, fill: "var(--text-muted)" };

type Datum = { name: string; value: number };

function fmtFor(values: number[], kind?: string) {
  const pctScale = kind === "percentage" && values.some((v) => Math.abs(v) > 1);
  return (n: number) => formatMeasure(n, kind, pctScale);
}

type TipEntry = { name?: string; value?: unknown; color?: string; payload?: Record<string, unknown> };

function Tip({
  active,
  payload,
  label,
  fmt,
  pick,
}: {
  active?: boolean;
  payload?: TipEntry[];
  label?: string;
  fmt: (n: number) => string;
  pick?: (entry: TipEntry) => number | null;
}) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="chart-tip" role="status">
      {label ? <p className="chart-tip-title">{label}</p> : null}
      {payload.map((entry, idx) => {
        const v = pick ? pick(entry) : typeof entry.value === "number" ? entry.value : null;
        if (v == null) return null;
        return (
          <p key={idx} className="chart-tip-row">
            <span className="chart-tip-key" style={{ background: entry.color ?? CHART_COLORS[0] }} aria-hidden />
            <strong className="chart-tip-value">{fmt(v)}</strong>
            <span className="chart-tip-name">{entry.name}</span>
          </p>
        );
      })}
    </div>
  );
}

// Stacked Bar: composition and totals
export const StackedBarComponent = memo(function StackedBarComponent({
  data,
  series,
  seriesLabels = {},
  valueKind,
}: {
  data: Array<Record<string, unknown> & { name: string }>;
  series: string[];
  seriesLabels?: Record<string, string>;
  valueKind?: string;
}) {
  const values = data.flatMap((d) => series.map((s) => d[s])).filter((v): v is number => typeof v === "number");
  const fmt = fmtFor(values, valueKind);
  return (
    <ResponsiveContainer width="100%" height={300}>
      <BarChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis
          dataKey="name"
          tick={TICK}
          interval={0}
          tickFormatter={(v: string) => truncate(String(v), 14)}
          axisLine={{ stroke: AXIS }}
          tickLine={false}
        />
        <YAxis tickFormatter={fmt} tick={TICK} width={60} axisLine={false} tickLine={false} />
        <Tooltip cursor={{ fill: "var(--surface-muted)" }} content={<Tip fmt={fmt} />} />
        {series.length > 1 ? (
          <Legend
            iconType="square"
            iconSize={10}
            wrapperStyle={{ fontSize: 12 }}
            formatter={(value: string) => <span style={{ color: "var(--muted)" }}>{value}</span>}
          />
        ) : null}
        {series.map((s, idx) => (
          <Bar
            key={s}
            dataKey={s}
            name={seriesLabels[s] ?? humanize(s)}
            stackId="stack"
            fill={CHART_COLORS[idx % CHART_COLORS.length]}
            stroke="var(--surface)"
            strokeWidth={1}
            maxBarSize={24}
            isAnimationActive={false}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
});

// Waterfall: step-by-step contribution to a running total. Increases and
// decreases are told apart by colour AND by the signed value in the tooltip.
export const WaterfallComponent = memo(function WaterfallComponent({
  data,
  valueKind,
}: {
  data: Datum[];
  valueKind?: string;
}) {
  let running = 0;
  const steps = data.map((item) => {
    const start = running;
    running += item.value;
    return {
      name: item.name,
      delta: item.value,
      base: Math.min(start, running),
      span: Math.abs(item.value),
    };
  });
  const fmt = fmtFor(data.map((d) => d.value), valueKind);
  return (
    <ResponsiveContainer width="100%" height={300}>
      <BarChart data={steps} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis
          dataKey="name"
          tick={TICK}
          interval={0}
          tickFormatter={(v: string) => truncate(String(v), 14)}
          axisLine={{ stroke: AXIS }}
          tickLine={false}
        />
        <YAxis tickFormatter={fmt} tick={TICK} width={60} axisLine={false} tickLine={false} />
        <Tooltip
          cursor={{ fill: "var(--surface-muted)" }}
          content={
            <Tip
              fmt={(n) => `${n >= 0 ? "+" : "−"}${fmt(Math.abs(n))}`}
              pick={(entry) => (entry.name === "Change" ? (entry.payload?.delta as number) : null)}
            />
          }
        />
        <Bar dataKey="base" stackId="wf" fill="transparent" isAnimationActive={false} name="base" />
        <Bar dataKey="span" stackId="wf" name="Change" maxBarSize={24} radius={[4, 4, 4, 4]} isAnimationActive={false}>
          {steps.map((s) => (
            <Cell key={s.name} fill={s.delta >= 0 ? CHART_COLORS[0] : CHART_COLORS[1]} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
});

// Funnel: progressive reduction, each stage as a share of the first.
export const FunnelComponent = memo(function FunnelComponent({
  data,
  valueKind,
}: {
  data: Datum[];
  valueKind?: string;
}) {
  if (data.length === 0) return null;
  const maxValue = Math.max(...data.map((d) => d.value));
  const fmt = fmtFor(data.map((d) => d.value), valueKind);

  return (
    <div className="funnel-chart">
      {data.map((item, idx) => {
        const pct = maxValue > 0 ? (item.value / maxValue) * 100 : 0;
        const prev = idx > 0 ? data[idx - 1].value : null;
        const stepPct = prev ? (item.value / prev) * 100 : null;
        return (
          <div key={`${item.name}-${idx}`} className="funnel-row">
            <div className="funnel-meta">
              <span className="funnel-label">{item.name}</span>
              <span className="funnel-value">
                {fmt(item.value)}
                {stepPct != null ? <span className="funnel-step"> · {stepPct.toFixed(0)}% of previous</span> : null}
              </span>
            </div>
            <div className="funnel-track">
              <div className="funnel-segment" style={{ width: `${Math.max(pct, 1.5)}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
});

// Gauge: a single value against a limit — a meter, one ramp.
export const GaugeComponent = memo(function GaugeComponent({
  value,
  maxValue = 100,
  label,
}: {
  value: number;
  maxValue?: number;
  label: string;
}) {
  const percentage = maxValue > 0 ? Math.max(0, Math.min(100, (value / maxValue) * 100)) : 0;
  const arc = 251.2;

  return (
    <div className="gauge-chart">
      <svg viewBox="0 0 200 120" className="gauge-svg" role="img" aria-label={`${label}: ${percentage.toFixed(1)}%`}>
        <path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="var(--chart-grid)" strokeWidth="10" strokeLinecap="round" />
        <path
          d="M 20 100 A 80 80 0 0 1 180 100"
          fill="none"
          stroke={CHART_COLORS[0]}
          strokeWidth="10"
          strokeLinecap="round"
          strokeDasharray={`${(percentage / 100) * arc} ${arc}`}
        />
        <text x="20" y="116" fontSize="10" fill="var(--text-muted)" textAnchor="middle">
          0
        </text>
        <text x="180" y="116" fontSize="10" fill="var(--text-muted)" textAnchor="middle">
          {formatMeasure(maxValue)}
        </text>
      </svg>
      <div className="gauge-value">
        <p className="gauge-number">{percentage.toFixed(1)}%</p>
        <p className="gauge-label">
          {label} · {formatMeasure(value)} of {formatMeasure(maxValue)}
        </p>
      </div>
    </div>
  );
});
