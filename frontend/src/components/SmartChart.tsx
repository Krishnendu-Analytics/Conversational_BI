/**
 * SmartChart — one chart component for the chat panel and dashboards.
 *
 * Reads the chart plan (form, ranking, caps, headline) from lib/chartModel
 * and renders it with Recharts following the dataviz rules: one y-axis only
 * (different scales become small multiples), thin marks with rounded ends,
 * hairline grid, emphasis on the leader, a tooltip that names the category
 * and the measure, and view controls (form / measure / sort).
 */

import { memo, useMemo, useState, type ReactNode } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  Brush,
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import {
  CHART_COLORS,
  asNumber,
  formatMeasure,
  planChart,
  truncate,
  type ChartDatum,
  type ChartForm,
  type ChartPlan,
  type Row,
  type SortMode,
} from "@/lib/chartModel";

type Props = {
  rows: Row[];
  kinds?: Record<string, string>;
  measureLabels?: Record<string, string>;
  suggestion?: string | null;
  /** Card title; defaults to the plan's measure/dimension description. */
  title?: string;
  /** Tighter heights for dashboard grid cells. */
  compact?: boolean;
  /** Click a category (bar / slice) to cross-filter: (column, raw value). */
  onCategoryClick?: (column: string, value: string | number) => void;
};

const FORM_LABELS: Record<ChartForm, string> = {
  bar: "Columns",
  hbar: "Bars",
  grouped: "Grouped",
  multiples: "Split",
  line: "Line",
  area: "Area",
  pie: "Donut",
  scatter: "Scatter",
  none: "Table",
};

const SORT_LABELS: Record<SortMode, string> = {
  value_desc: "Highest first",
  value_asc: "Lowest first",
  label: "A–Z",
};

const ACCENT = CHART_COLORS[0];
const MUTED_MARK = "var(--chart-deemphasis)";
const GRID = "var(--chart-grid)";
const AXIS = "var(--chart-axis)";
const TICK = { fontSize: 11, fill: "var(--text-muted)" };
const BAR_SIZE = 24;
const BRUSH_THRESHOLD = 30;

function colorForSeries(index: number, name: string): string {
  if (name === "Other") return MUTED_MARK;
  return CHART_COLORS[index % CHART_COLORS.length];
}

/** Per-measure value formatter: % only when the series is on a 0–100 scale. */
function makeFormatter(plan: ChartPlan, key: string): (n: number) => string {
  const kind = plan.kindOf(key);
  const values = plan.data.map((d) => d[key]).filter((v): v is number => typeof v === "number");
  const pctScale = kind === "percentage" && values.some((v) => Math.abs(v) > 1);
  return (n: number) => formatMeasure(n, kind, pctScale);
}

const CHAR_PX = 6.2;

function categoryAxisWidth(data: ChartDatum[], max = 190): number {
  const longest = data.reduce((m, d) => Math.max(m, d.label.length), 0);
  return Math.min(max, Math.max(56, Math.round(longest * CHAR_PX) + 14));
}

/** Characters that fit in a category axis of this width on one line. */
function charsFor(width: number): number {
  return Math.max(6, Math.floor((width - 14) / CHAR_PX));
}

type TickProps = {
  x?: number;
  y?: number;
  payload?: { value?: unknown };
};

/** One-line category tick. Recharts' default tick wraps long labels onto
 *  several lines; this draws a single truncated line with the full label
 *  as a native hover title. */
function categoryTick(maxChars: number, mode: "left" | "angled" | "below") {
  return function CategoryTick({ x = 0, y = 0, payload }: TickProps) {
    const full = String(payload?.value ?? "");
    const text = truncate(full, maxChars);
    if (mode === "left") {
      return (
        <text x={x - 6} y={y} dy={4} textAnchor="end" fontSize={TICK.fontSize} fill={TICK.fill}>
          <title>{full}</title>
          {text}
        </text>
      );
    }
    if (mode === "angled") {
      return (
        <text
          x={x}
          y={y + 8}
          textAnchor="end"
          fontSize={TICK.fontSize}
          fill={TICK.fill}
          transform={`rotate(-35, ${x}, ${y + 8})`}
        >
          <title>{full}</title>
          {text}
        </text>
      );
    }
    return (
      <text x={x} y={y + 14} textAnchor="middle" fontSize={TICK.fontSize} fill={TICK.fill}>
        <title>{full}</title>
        {text}
      </text>
    );
  };
}

// ----------------------------------------------------------------- tooltip

type TipEntry = { name?: string; value?: unknown; color?: string; dataKey?: unknown; payload?: ChartDatum };

function ChartTooltip({
  active,
  payload,
  plan,
  nameFor,
  formatFor,
  extra,
}: {
  active?: boolean;
  payload?: TipEntry[];
  plan: ChartPlan;
  nameFor: (dataKey: string) => string;
  formatFor: (dataKey: string) => (n: number) => string;
  extra?: (datum: ChartDatum) => string | null;
}) {
  if (!active || !payload || payload.length === 0) return null;
  const datum = payload[0]?.payload;
  const title = plan.form === "scatter" ? null : datum?.label;
  const note = datum && extra ? extra(datum) : null;
  return (
    <div className="chart-tip" role="status">
      {title ? <p className="chart-tip-title">{title}</p> : null}
      {payload.map((entry, idx) => {
        const key = String(entry.dataKey ?? entry.name ?? "");
        const n = asNumber(entry.value);
        return (
          <p key={`${key}-${idx}`} className="chart-tip-row">
            <span className="chart-tip-key" style={{ background: entry.color ?? ACCENT }} aria-hidden />
            <strong className="chart-tip-value">{n == null ? "—" : formatFor(key)(n)}</strong>
            <span className="chart-tip-name">{nameFor(key)}</span>
          </p>
        );
      })}
      {note ? <p className="chart-tip-note">{note}</p> : null}
    </div>
  );
}

// ---------------------------------------------------------------- component

export const SmartChart = memo(function SmartChart({
  rows,
  kinds,
  measureLabels,
  suggestion,
  title,
  compact = false,
  onCategoryClick,
}: Props) {
  const [form, setForm] = useState<ChartForm | null>(null);
  const [measure, setMeasure] = useState<string | null>(null);
  const [sort, setSort] = useState<SortMode | null>(null);

  const plan = useMemo(
    () =>
      planChart(rows, {
        kinds,
        measureLabels,
        suggestion,
        form,
        measure,
        sort,
        maxCategories: compact ? 15 : 20,
      }),
    [rows, kinds, measureLabels, suggestion, form, measure, sort, compact],
  );

  if (plan.form === "none") return null;

  const customized = form != null || measure != null || sort != null;
  // Title = what is plotted; the sub line carries only the extra notes
  // (caps, period counts) so it never repeats the title.
  const [description, ...notes] = plan.subtitle.split(" · ");
  const heading = title || description;
  const subline = notes.join(" · ");

  return (
    <section className={`smart-chart${compact ? " smart-chart-compact" : ""}`}>
      <header className="smart-chart-head">
        <div className="smart-chart-titles">
          <h3 className="insight-chart-title">{heading}</h3>
          {subline ? <p className="insight-chart-sub">{subline}</p> : null}
        </div>
        <ChartControls
          plan={plan}
          onForm={setForm}
          onMeasure={(m) => {
            // Picking a measure (or "All") returns to that view's default form.
            setMeasure(m);
            setForm(null);
          }}
          onSort={setSort}
          onReset={
            customized
              ? () => {
                  setForm(null);
                  setMeasure(null);
                  setSort(null);
                }
              : undefined
          }
        />
      </header>
      {plan.headline ? <p className="smart-chart-headline">{plan.headline}</p> : null}
      <div className="smart-chart-body">
        <ChartBody plan={plan} compact={compact} onCategoryClick={onCategoryClick} />
      </div>
    </section>
  );
});

function ChartControls({
  plan,
  onForm,
  onMeasure,
  onSort,
  onReset,
}: {
  plan: ChartPlan;
  onForm: (f: ChartForm) => void;
  onMeasure: (m: string | null) => void;
  onSort: (s: SortMode) => void;
  onReset?: () => void;
}) {
  const measureChoices = plan.allMeasures;
  // "All measures" = the default multi-measure view (grouped / split, or
  // several same-scale lines); picking one measure narrows to it.
  const allOption =
    !plan.seriesKey &&
    (plan.forms.includes("grouped") || plan.forms.includes("multiples") || (plan.timeKey != null && measureChoices.length > 1));
  const showingAll = allOption && plan.measures.length > 1;
  const current = showingAll ? "" : plan.primary ?? "";
  return (
    <div className="smart-chart-controls">
      {plan.forms.length > 1 ? (
        <div className="seg" role="group" aria-label="Chart type">
          {plan.forms.map((f) => (
            <button
              key={f}
              type="button"
              className="seg-btn"
              aria-pressed={plan.form === f}
              onClick={() => onForm(f)}
            >
              {FORM_LABELS[f]}
            </button>
          ))}
        </div>
      ) : null}
      {measureChoices.length > 1 && plan.form !== "scatter" ? (
        <select
          className="smart-chart-select"
          aria-label="Measure"
          value={current}
          onChange={(e) => onMeasure(e.target.value || null)}
        >
          {allOption ? <option value="">All measures</option> : null}
          {measureChoices.map((m) => (
            <option key={m} value={m}>
              {plan.measureLabel(m)}
            </option>
          ))}
        </select>
      ) : null}
      {plan.sortable ? (
        <select
          className="smart-chart-select"
          aria-label="Sort"
          value={plan.sort}
          onChange={(e) => onSort(e.target.value as SortMode)}
        >
          {(Object.keys(SORT_LABELS) as SortMode[]).map((s) => (
            <option key={s} value={s}>
              {SORT_LABELS[s]}
            </option>
          ))}
        </select>
      ) : null}
      {onReset ? (
        <button type="button" className="smart-chart-reset" onClick={onReset}>
          Reset
        </button>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------- bodies

function ChartBody({
  plan,
  compact,
  onCategoryClick,
}: {
  plan: ChartPlan;
  compact: boolean;
  onCategoryClick?: (column: string, value: string | number) => void;
}): ReactNode {
  // In a pivoted time series the drawn series are category names, all in
  // the primary measure's units.
  const nameFor = (k: string) => (plan.seriesKey ? k : plan.measureLabel(k));
  const formatFor = (k: string) => makeFormatter(plan, plan.seriesKey ? plan.primary ?? k : k);

  const clickCategory = (datum: ChartDatum | undefined) => {
    if (!datum || datum.isOther || !onCategoryClick || plan.categoryKeys.length === 0) return;
    const col = plan.categoryKeys[plan.categoryKeys.length - 1];
    const raw = datum[`__cat_${col}`];
    if (typeof raw === "string" || typeof raw === "number") onCategoryClick(col, raw);
  };
  const clickable = Boolean(onCategoryClick) && plan.categoryKeys.length > 0;

  switch (plan.form) {
    case "bar":
    case "hbar":
      return (
        <SingleBars
          plan={plan}
          measure={plan.measures[0]}
          horizontal={plan.form === "hbar"}
          compact={compact}
          nameFor={nameFor}
          formatFor={formatFor}
          onClick={clickCategory}
          clickable={clickable}
        />
      );
    case "grouped":
      return <GroupedBars plan={plan} compact={compact} nameFor={nameFor} formatFor={formatFor} />;
    case "multiples":
      return (
        <div className="smart-chart-multiples">
          {plan.measures.map((m) => (
            <div key={m} className="smart-chart-multiple">
              <p className="smart-chart-multiple-title">{plan.measureLabel(m)}</p>
              <SingleBars
                plan={plan}
                measure={m}
                horizontal
                compact
                color={colorForSeries(plan.allMeasures.indexOf(m), m)}
                nameFor={nameFor}
                formatFor={formatFor}
                onClick={clickCategory}
                clickable={clickable}
                small
              />
            </div>
          ))}
        </div>
      );
    case "pie":
      return <Donut plan={plan} compact={compact} nameFor={nameFor} formatFor={formatFor} onClick={clickCategory} />;
    case "scatter":
      return <Scatterplot plan={plan} compact={compact} nameFor={nameFor} formatFor={formatFor} />;
    case "line":
    case "area":
    default:
      return <TimeSeries plan={plan} compact={compact} nameFor={nameFor} formatFor={formatFor} />;
  }
}

type BodyProps = {
  plan: ChartPlan;
  compact: boolean;
  nameFor: (k: string) => string;
  formatFor: (k: string) => (n: number) => string;
};

function shareAndRank(plan: ChartPlan, measure: string): (d: ChartDatum) => string | null {
  const additive = !["percentage", "ratio", "rate", "average", "minimum", "maximum", "running_total"].includes(
    plan.kindOf(measure) ?? "",
  );
  const values = plan.data.map((d) => d[measure]).filter((v): v is number => typeof v === "number");
  const total = values.reduce((a, b) => a + b, 0);
  const ranked = plan.data
    .filter((d) => !d.isOther && typeof d[measure] === "number")
    .sort((a, b) => (b[measure] as number) - (a[measure] as number));
  return (d: ChartDatum) => {
    if (d.isOther) return `${plan.foldedIntoOther} smaller categories combined`;
    const v = d[measure];
    if (typeof v !== "number") return null;
    const rank = ranked.indexOf(d) + 1;
    const parts = [`#${rank} of ${plan.totalCategories}`];
    if (additive && total > 0 && values.every((x) => x >= 0)) parts.push(`${((v / total) * 100).toFixed(1)}% of total`);
    return parts.join(" · ");
  };
}

/** Axis elements for bar charts. Recharts 2 only finds axes among a
 *  chart's direct children (not inside fragments), so callers place these
 *  elements directly. */
function barAxes(data: ChartDatum[], fmt: (n: number) => string, angled: boolean, small = false) {
  const axisWidth = categoryAxisWidth(data, small ? 130 : 190);
  const fit = charsFor(axisWidth);
  return {
    valueAxisH: <XAxis type="number" tickFormatter={fmt} tick={TICK} axisLine={{ stroke: AXIS }} tickLine={false} />,
    categoryAxisH: (
      <YAxis
        type="category"
        dataKey="label"
        width={axisWidth}
        tick={categoryTick(fit, "left")}
        axisLine={{ stroke: AXIS }}
        tickLine={false}
        interval={0}
      />
    ),
    categoryAxisV: (
      <XAxis
        dataKey="label"
        tick={categoryTick(angled ? 16 : 14, angled ? "angled" : "below")}
        height={angled ? 70 : 26}
        interval={0}
        axisLine={{ stroke: AXIS }}
        tickLine={false}
      />
    ),
    valueAxisV: <YAxis tickFormatter={fmt} tick={TICK} width={60} axisLine={false} tickLine={false} />,
  };
}

function SingleBars({
  plan,
  measure,
  horizontal,
  compact,
  nameFor,
  formatFor,
  onClick,
  clickable,
  color,
  small = false,
}: BodyProps & {
  measure: string;
  horizontal: boolean;
  onClick: (d: ChartDatum | undefined) => void;
  clickable: boolean;
  color?: string;
  small?: boolean;
}) {
  const fmt = formatFor(measure);
  const data = plan.data;
  const angled = !horizontal && data.length > 6;
  const height = horizontal
    ? Math.max(small ? 140 : 180, data.length * (small ? 22 : 28) + 44)
    : (compact ? 240 : 300) + (angled ? 44 : 0);
  const leaderKey = small ? null : plan.leaderKey;
  const tipExtra = shareAndRank(plan, measure);
  const average = small || plan.measures[0] !== measure ? null : plan.average;
  const base = color ?? ACCENT;
  const fillFor = (d: ChartDatum) => {
    if (d.isOther) return MUTED_MARK;
    if (!leaderKey) return base;
    return d.key === leaderKey ? base : MUTED_MARK;
  };

  const { valueAxisH, categoryAxisH, categoryAxisV, valueAxisV } = barAxes(data, fmt, angled, small);

  const renderLeaderLabel = (props: { x?: unknown; y?: unknown; width?: unknown; height?: unknown; index?: number }) => {
    const d = props.index != null ? data[props.index] : undefined;
    if (!d || d.key !== leaderKey) return null;
    const v = d[measure];
    if (typeof v !== "number") return null;
    const x = Number(props.x ?? 0);
    const y = Number(props.y ?? 0);
    const w = Number(props.width ?? 0);
    const h = Number(props.height ?? 0);
    return horizontal ? (
      <text x={x + w + 6} y={y + h / 2} dy={4} fontSize={11} fontWeight={600} fill="var(--text)">
        {fmt(v)}
      </text>
    ) : (
      <text x={x + w / 2} y={y - 6} textAnchor="middle" fontSize={11} fontWeight={600} fill="var(--text)">
        {fmt(v)}
      </text>
    );
  };

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart
        data={data}
        layout={horizontal ? "vertical" : "horizontal"}
        margin={{ top: horizontal ? (average != null ? 18 : 4) : 18, right: horizontal ? 56 : 12, left: 4, bottom: 4 }}
        barCategoryGap={horizontal ? "22%" : "28%"}
      >
        <CartesianGrid stroke={GRID} vertical={horizontal} horizontal={!horizontal} />
        {horizontal ? valueAxisH : categoryAxisV}
        {horizontal ? categoryAxisH : valueAxisV}
        <Tooltip
          cursor={{ fill: "var(--surface-muted)" }}
          content={<ChartTooltip plan={plan} nameFor={nameFor} formatFor={formatFor} extra={tipExtra} />}
        />
        {average != null ? (
          <ReferenceLine
            {...(horizontal ? { x: average } : { y: average })}
            stroke="var(--text-muted)"
            strokeDasharray="4 4"
            ifOverflow="extendDomain"
            label={{
              value: `Avg ${fmt(average)}`,
              position: horizontal ? "top" : "insideTopRight",
              fill: "var(--text-muted)",
              fontSize: 10,
            }}
          />
        ) : null}
        <Bar
          dataKey={measure}
          name={nameFor(measure)}
          maxBarSize={BAR_SIZE}
          radius={horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]}
          cursor={clickable ? "pointer" : undefined}
          onClick={(entry: { payload?: ChartDatum }) => onClick(entry?.payload)}
          isAnimationActive={false}
        >
          {data.map((d) => (
            <Cell key={d.key} fill={fillFor(d)} />
          ))}
          {leaderKey ? <LabelList dataKey={measure} content={renderLeaderLabel} /> : null}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function GroupedBars({ plan, compact, nameFor, formatFor }: BodyProps) {
  const data = plan.data;
  const horizontal = data.length > 8 || data.some((d) => d.label.length > 14);
  const fmt = formatFor(plan.measures[0]);
  const height = horizontal
    ? Math.max(200, data.length * (18 * plan.measures.length + 12) + 60)
    : compact
      ? 260
      : 320;
  const { valueAxisH, categoryAxisH, categoryAxisV, valueAxisV } = barAxes(data, fmt, false);
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart
        data={data}
        layout={horizontal ? "vertical" : "horizontal"}
        margin={{ top: 8, right: 16, left: 4, bottom: 4 }}
        barGap={2}
      >
        <CartesianGrid stroke={GRID} vertical={horizontal} horizontal={!horizontal} />
        {horizontal ? valueAxisH : categoryAxisV}
        {horizontal ? categoryAxisH : valueAxisV}
        <Tooltip
          cursor={{ fill: "var(--surface-muted)" }}
          content={<ChartTooltip plan={plan} nameFor={nameFor} formatFor={formatFor} />}
        />
        <Legend
          iconType="square"
          iconSize={10}
          wrapperStyle={{ fontSize: 12, color: "var(--muted)" }}
          formatter={(value: string) => <span style={{ color: "var(--muted)" }}>{value}</span>}
        />
        {plan.measures.map((m) => (
          <Bar
            key={m}
            dataKey={m}
            name={nameFor(m)}
            fill={colorForSeries(plan.allMeasures.indexOf(m), m)}
            maxBarSize={18}
            radius={horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]}
            isAnimationActive={false}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

function TimeSeries({ plan, compact, nameFor, formatFor }: BodyProps) {
  const data = plan.data;
  const series = plan.measures;
  const valueKey = plan.seriesKey ? plan.primary ?? series[0] : series[0];
  const fmt = formatFor(valueKey);
  const showBrush = data.length > BRUSH_THRESHOLD;
  const height = (compact ? 240 : 300) + (showBrush ? 34 : 0);
  const showDots = data.length <= 24;
  const multi = series.length > 1;

  const xAxis = (
    <XAxis
      dataKey="label"
      tick={TICK}
      minTickGap={18}
      interval="preserveStartEnd"
      tickFormatter={(v: string) => truncate(String(v), 12)}
      axisLine={{ stroke: AXIS }}
      tickLine={false}
    />
  );
  const yAxis = <YAxis tickFormatter={fmt} tick={TICK} width={60} axisLine={false} tickLine={false} />;
  const grid = <CartesianGrid stroke={GRID} vertical={false} />;
  const tooltip = (
    <Tooltip
      cursor={{ stroke: "var(--text-muted)", strokeWidth: 1 }}
      content={<ChartTooltip plan={plan} nameFor={nameFor} formatFor={formatFor} />}
    />
  );
  const legend = multi ? (
    <Legend
      iconType="plainline"
      iconSize={14}
      wrapperStyle={{ fontSize: 12 }}
      formatter={(value: string) => <span style={{ color: "var(--muted)" }}>{value}</span>}
    />
  ) : null;
  const brush = showBrush ? (
    <Brush dataKey="label" height={22} stroke={AXIS} travellerWidth={8} tickFormatter={(v: string) => truncate(String(v), 10)} />
  ) : null;

  if (plan.form === "bar") {
    return (
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
          {grid}
          {xAxis}
          {yAxis}
          <Tooltip
            cursor={{ fill: "var(--surface-muted)" }}
            content={<ChartTooltip plan={plan} nameFor={nameFor} formatFor={formatFor} />}
          />
          {legend}
          {series.map((s, i) => (
            <Bar
              key={s}
              dataKey={s}
              name={nameFor(s)}
              fill={colorForSeries(plan.seriesKey ? i : plan.allMeasures.indexOf(s), s)}
              maxBarSize={BAR_SIZE}
              radius={[4, 4, 0, 0]}
              isAnimationActive={false}
            />
          ))}
          {brush}
        </BarChart>
      </ResponsiveContainer>
    );
  }

  if (plan.form === "area") {
    return (
      <ResponsiveContainer width="100%" height={height}>
        <AreaChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
          {grid}
          {xAxis}
          {yAxis}
          {tooltip}
          {legend}
          {series.map((s, i) => {
            const c = colorForSeries(plan.seriesKey ? i : plan.allMeasures.indexOf(s), s);
            return (
              <Area
                key={s}
                type="monotone"
                dataKey={s}
                name={nameFor(s)}
                stroke={c}
                strokeWidth={2}
                fill={c}
                fillOpacity={0.1}
                dot={false}
                activeDot={{ r: 5, stroke: "var(--surface)", strokeWidth: 2 }}
                isAnimationActive={false}
                connectNulls
              />
            );
          })}
          {brush}
        </AreaChart>
      </ResponsiveContainer>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 16, left: 4, bottom: 4 }}>
        {grid}
        {xAxis}
        {yAxis}
        {tooltip}
        {legend}
        {series.map((s, i) => {
          const c = colorForSeries(plan.seriesKey ? i : plan.allMeasures.indexOf(s), s);
          return (
            <Line
              key={s}
              type="monotone"
              dataKey={s}
              name={nameFor(s)}
              stroke={c}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              dot={showDots ? { r: 4, fill: c, stroke: "var(--surface)", strokeWidth: 2 } : false}
              activeDot={{ r: 5, stroke: "var(--surface)", strokeWidth: 2 }}
              isAnimationActive={false}
              connectNulls
            />
          );
        })}
        {brush}
      </LineChart>
    </ResponsiveContainer>
  );
}

function Donut({
  plan,
  compact,
  nameFor,
  formatFor,
  onClick,
}: BodyProps & { onClick: (d: ChartDatum | undefined) => void }) {
  const measure = plan.measures[0];
  const fmt = formatFor(measure);
  const total = plan.data.reduce((s, d) => s + (typeof d[measure] === "number" ? (d[measure] as number) : 0), 0);
  const tipExtra = shareAndRank(plan, measure);
  const height = compact ? 240 : 280;
  return (
    <div className="smart-chart-donut">
      <ResponsiveContainer width="100%" height={height}>
        <PieChart>
          <Tooltip content={<ChartTooltip plan={plan} nameFor={nameFor} formatFor={formatFor} extra={tipExtra} />} />
          <Pie
            data={plan.data}
            dataKey={measure}
            nameKey="label"
            cy="45%"
            innerRadius="58%"
            outerRadius="82%"
            stroke="var(--surface)"
            strokeWidth={2}
            isAnimationActive={false}
            onClick={(entry: { payload?: ChartDatum }) => onClick(entry?.payload)}
          >
            {plan.data.map((d, i) => (
              <Cell key={d.key} fill={d.isOther ? MUTED_MARK : CHART_COLORS[i % CHART_COLORS.length]} />
            ))}
          </Pie>
          <Legend
            iconType="square"
            iconSize={10}
            layout="horizontal"
            align="center"
            verticalAlign="bottom"
            wrapperStyle={{ fontSize: 12 }}
            formatter={(value: string) => <span style={{ color: "var(--muted)" }}>{truncate(value, 22)}</span>}
          />
        </PieChart>
      </ResponsiveContainer>
      <div className="smart-chart-donut-center" aria-hidden>
        <span className="smart-chart-donut-value">{fmt(total)}</span>
        <span className="smart-chart-donut-label">Total</span>
      </div>
    </div>
  );
}

function Scatterplot({ plan, compact, nameFor, formatFor }: BodyProps) {
  const sc = plan.scatter;
  if (!sc) return null;
  return (
    <ResponsiveContainer width="100%" height={compact ? 260 : 320}>
      <ScatterChart margin={{ top: 8, right: 16, left: 4, bottom: 20 }}>
        <CartesianGrid stroke={GRID} />
        <XAxis
          type="number"
          dataKey={sc.x}
          name={nameFor(sc.x)}
          tickFormatter={formatFor(sc.x)}
          tick={TICK}
          axisLine={{ stroke: AXIS }}
          tickLine={false}
          label={{ value: nameFor(sc.x), position: "insideBottom", offset: -12, fontSize: 11, fill: "var(--muted)" }}
        />
        <YAxis
          type="number"
          dataKey={sc.y}
          name={nameFor(sc.y)}
          tickFormatter={formatFor(sc.y)}
          tick={TICK}
          width={60}
          axisLine={false}
          tickLine={false}
          label={{ value: nameFor(sc.y), angle: -90, position: "insideLeft", fontSize: 11, fill: "var(--muted)" }}
        />
        <ZAxis range={[64, 64]} />
        <Tooltip
          cursor={{ strokeDasharray: "0", stroke: "var(--text-muted)" }}
          content={<ChartTooltip plan={plan} nameFor={nameFor} formatFor={formatFor} />}
        />
        <Scatter data={plan.data} fill={ACCENT} stroke="var(--surface)" strokeWidth={2} isAnimationActive={false} />
      </ScatterChart>
    </ResponsiveContainer>
  );
}
