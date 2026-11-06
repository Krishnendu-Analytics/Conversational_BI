/**
 * Chart model: turns raw result rows into a chart plan — which form to draw,
 * which columns are categories / time / measures, how rows are ranked and
 * capped, and the headline + KPI facts that make the chart say something.
 *
 * Pure functions only (no React), shared by the chat visualization panel and
 * dashboard widgets so a fix lands everywhere at once.
 */

export type Row = Record<string, unknown>;

export type ChartForm =
  | "bar" // vertical columns, one measure
  | "hbar" // horizontal bars, one measure (many / long-named categories)
  | "grouped" // several same-scale measures side by side
  | "multiples" // one small chart per measure (different scales — never dual-axis)
  | "line"
  | "area"
  | "pie"
  | "scatter"
  | "none";

export type SortMode = "value_desc" | "value_asc" | "label";

export type ColumnRole = "measure" | "dimension" | "time" | "id";

export type ColumnInfo = {
  key: string;
  label: string;
  role: ColumnRole;
  kind?: string;
};

export type ChartDatum = {
  /** Axis label (may be truncated for display by the renderer). */
  label: string;
  /** Unique id for React keys / click-to-filter (raw category value). */
  key: string;
  isOther?: boolean;
  [measure: string]: unknown;
};

export type ChartPlan = {
  form: ChartForm;
  forms: ChartForm[];
  /** The measure that drives ranking, headline and the measure picker. */
  primary: string | null;
  /** Series drawn: measure keys, or category names for a pivoted time series. */
  measures: string[];
  allMeasures: string[];
  categoryKeys: string[];
  timeKey: string | null;
  seriesKey: string | null;
  seriesNames: string[];
  data: ChartDatum[];
  sort: SortMode;
  sortable: boolean;
  totalCategories: number;
  shownCategories: number;
  foldedIntoOther: number;
  headline: string | null;
  /** How noteworthy the headline is (0 = routine, 3 = outlier / big move),
   *  used to rank findings across a dashboard. */
  significance: number;
  subtitle: string;
  average: number | null;
  leaderKey: string | null;
  columns: ColumnInfo[];
  measureLabel: (key: string) => string;
  kindOf: (key: string) => string | undefined;
  scatter?: { x: string; y: string; r: number | null };
  /** Year-over-year comparison for a regular monthly/quarterly series. */
  yoy?: YearOverYear | null;
};

export type YearOverYear = {
  /** % change (amounts, ratios) or point change (0–100 percentages). */
  change: number;
  unit: "%" | " pts";
  /** e.g. "last 12 months vs prior 12" or "Mar 2025 vs Mar 2024". */
  basis: string;
};

export type KpiFact = {
  id: string;
  label: string;
  value: string;
  sub?: string;
  direction?: "up" | "down" | "flat";
  /** Value is a name (e.g. top category), not a number. */
  text?: boolean;
};

export type PlanOptions = {
  kinds?: Record<string, string>;
  measureLabels?: Record<string, string>;
  suggestion?: string | null;
  form?: ChartForm | null;
  measure?: string | null;
  sort?: SortMode | null;
  /** Max categories drawn before the tail folds into "Other". */
  maxCategories?: number;
};

export const CHART_COLORS = [
  "var(--chart-series-1)",
  "var(--chart-series-2)",
  "var(--chart-series-3)",
  "var(--chart-series-4)",
  "var(--chart-series-5)",
  "var(--chart-series-6)",
  "var(--chart-series-7)",
  "var(--chart-series-8)",
];

const NON_ADDITIVE_KINDS = new Set([
  "percentage",
  "ratio",
  "rate",
  "average",
  "minimum",
  "maximum",
  "running_total",
]);

// Whole name tokens only, so "duration" is not read as "ratio" and
// "downtime_hours" is not read as a time column.
const PERCENT_NAME_RE = /(^|_)(pct|percent|percentage|margin|share|contribution)(_|$)/i;
const RATIO_NAME_RE = /(^|_)(ratio|rate|yield|efficiency|per|price|tariff|unit_cost|unit_price)(_|$)/i;
const AVG_NAME_RE = /(^|_)(avg|average|mean)(_|$)/i;
const TIME_NAME_RE =
  /(^|_)(date|day|week|month|quarter|year|period|dt|timestamp|time|yyyymm|fiscal_year|fiscal_quarter|fiscal_month)(_|$)|_at$/i;
const ID_NAME_RE = /(^|_)(id|uuid|pk|key|sk|fk)$/i;
const ISO_DATE_RE = /^\d{4}-\d{2}(-\d{2})?([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;
const YEAR_MONTH_RE = /^\d{4}[-/](0[1-9]|1[0-2])$/;

const DEFAULT_MAX_CATEGORIES = 20;
const MAX_SERIES = 6;
const MAX_MULTIPLES = 4;
const GROUPED_SCALE_RATIO = 10;

// ---------------------------------------------------------------- values

export function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const trimmed = value.trim().replace(/,/g, "");
    if (!trimmed || !/^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(trimmed)) return null;
    const n = Number(trimmed);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

export function formatCompact(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1_000_000_000_000) return `${trimZeros((n / 1_000_000_000_000).toFixed(2))}T`;
  if (abs >= 1_000_000_000) return `${trimZeros((n / 1_000_000_000).toFixed(2))}B`;
  if (abs >= 1_000_000) return `${trimZeros((n / 1_000_000).toFixed(2))}M`;
  if (abs >= 10_000) return `${trimZeros((n / 1_000).toFixed(1))}K`;
  if (Number.isInteger(n)) return n.toLocaleString();
  if (abs >= 100) return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (abs >= 1) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return n.toLocaleString(undefined, { maximumSignificantDigits: 3 });
}

function trimZeros(text: string): string {
  return text.includes(".") ? text.replace(/\.?0+$/, "") : text;
}

/** Format a measure value. Percentages get a % sign only when the series is
 *  on a 0–100 scale — a 0–1 fraction labelled "percent" is shown as-is
 *  rather than as a misleading "0.38%". */
export function formatMeasure(n: number, kind?: string, percentScale = true): string {
  if (kind === "percentage" && percentScale) {
    return `${n.toLocaleString(undefined, { maximumFractionDigits: Math.abs(n) < 10 ? 2 : 1 })}%`;
  }
  return formatCompact(n);
}

export function humanize(key: string): string {
  return String(key)
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, Math.max(1, max - 1))}…` : text;
}

function isNullish(value: unknown): boolean {
  return value == null || value === "";
}

// A midnight timestamp in UTC (or with no time part) is a calendar date:
// "2024-04-01T00:00:00Z" must read as 1 Apr 2024, not "1 Apr, 05:30" after
// conversion to the viewer's time zone.
const MIDNIGHT_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T]00:00(?::00(?:\.0+)?)?(?:Z|[+-]00:?00)?)?$/;

function parseDate(value: unknown): Date | null {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value !== "string") return null;
  const text = value.trim();
  const midnight = MIDNIGHT_DATE_RE.exec(text);
  if (midnight) {
    const date = new Date(Number(midnight[1]), Number(midnight[2]) - 1, Number(midnight[3]));
    return Number.isNaN(date.getTime()) ? null : date;
  }
  if (!ISO_DATE_RE.test(text) && !YEAR_MONTH_RE.test(text)) return null;
  const normalized = YEAR_MONTH_RE.test(text) ? `${text.replace("/", "-")}-01` : text;
  const date = new Date(normalized.length === 10 ? `${normalized}T00:00:00` : normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

type TimeGrain = "year" | "month" | "day" | "time";

function detectGrain(dates: Date[]): TimeGrain {
  if (dates.every((d) => d.getMonth() === 0 && d.getDate() === 1 && d.getHours() === 0)) return "year";
  if (dates.every((d) => d.getDate() === 1 && d.getHours() === 0 && d.getMinutes() === 0)) return "month";
  if (dates.every((d) => d.getHours() === 0 && d.getMinutes() === 0)) return "day";
  return "time";
}

function formatDate(date: Date, grain: TimeGrain): string {
  switch (grain) {
    case "year":
      return String(date.getFullYear());
    case "month":
      return date.toLocaleDateString(undefined, { month: "short", year: "numeric" });
    case "day":
      return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    default:
      return date.toLocaleString(undefined, {
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
  }
}

export function formatCell(value: unknown, key?: string): string {
  if (isNullish(value)) return "—";
  if (typeof value === "number" && Number.isFinite(value)) {
    // Period and identifier columns (year 2025, month 10, customer_key) are
    // labels, not quantities: no "2,025" or "12.4K".
    if (key && Number.isInteger(value) && (TIME_NAME_RE.test(key) || ID_NAME_RE.test(key))) {
      return String(value);
    }
    return formatCompact(value);
  }
  if (typeof value === "boolean") return value ? "true" : "false";
  const date = parseDate(value);
  // A lone value can't reveal the series grain, so show the full date
  // (a bare "2024" for 2024-01-01 would be misleading in a table cell).
  if (date) return formatDate(date, date.getHours() || date.getMinutes() ? "time" : "day");
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

// --------------------------------------------------------------- columns

export function measureKind(key: string, kinds: Record<string, string> = {}): string | undefined {
  const explicit = kinds[key];
  if (explicit) return explicit;
  if (PERCENT_NAME_RE.test(key)) return "percentage";
  if (RATIO_NAME_RE.test(key)) return "ratio";
  if (AVG_NAME_RE.test(key)) return "average";
  return undefined;
}

export function isAdditive(key: string, kinds: Record<string, string> = {}): boolean {
  const kind = measureKind(key, kinds);
  return !kind || !NON_ADDITIVE_KINDS.has(kind);
}

export function columnNames(rows: Row[]): string[] {
  const keys = new Set<string>();
  for (const row of rows.slice(0, 20)) {
    for (const key of Object.keys(row)) keys.add(key);
  }
  return Array.from(keys);
}

export function analyzeColumns(
  rows: Row[],
  kinds: Record<string, string> = {},
  measureLabels: Record<string, string> = {},
): ColumnInfo[] {
  const sample = rows.slice(0, 200);
  return columnNames(rows).map((key) => {
    const values = sample.map((r) => r[key]).filter((v) => !isNullish(v));
    const numeric = values.filter((v) => asNumber(v) != null).length;
    const dates = values.filter((v) => parseDate(v) != null).length;
    const share = (n: number) => (values.length ? n / values.length : 0);
    const label = measureLabels[key] ?? humanize(key);

    let role: ColumnRole;
    if (ID_NAME_RE.test(key)) {
      role = "id";
    } else if (share(dates) >= 0.8) {
      role = "time";
    } else if (TIME_NAME_RE.test(key) && !kinds[key]) {
      // Named like a period (year, month, fiscal_quarter...): a label on the
      // time axis even when stored as a number — never a summable measure.
      role = "time";
    } else if (values.length && share(numeric) >= 0.8) {
      role = "measure";
    } else {
      role = "dimension";
    }
    return { key, label, role, kind: role === "measure" ? measureKind(key, kinds) : undefined };
  });
}

/** Category columns that together identify a row: when the first label
 *  repeats (zone in a zone + state result), add the next until unique. */
export function grainKeys(rows: Row[], dims: string[]): string[] {
  const chosen: string[] = [];
  for (const key of dims.slice(0, 3)) {
    chosen.push(key);
    const distinct = new Set(rows.map((r) => chosen.map((c) => String(r[c] ?? "")).join("\u0000")));
    if (distinct.size >= rows.length) break;
  }
  return chosen;
}

// ------------------------------------------------------------------ plan

function peak(rows: Row[], key: string): number {
  return rows.reduce((m, r) => Math.max(m, Math.abs(asNumber(r[key]) ?? 0)), 0);
}

function sameScale(rows: Row[], keys: string[]): boolean {
  const peaks = keys.map((k) => peak(rows, k)).filter((p) => p > 0);
  if (peaks.length < 2) return true;
  return Math.max(...peaks) / Math.min(...peaks) <= GROUPED_SCALE_RATIO;
}

function sameKindFamily(keys: string[], kinds: Record<string, string>): boolean {
  const additive = keys.map((k) => isAdditive(k, kinds));
  return additive.every((a) => a === additive[0]);
}

function normalizeSuggestion(s: string | null | undefined): ChartForm | null {
  const t = (s || "").toLowerCase();
  if (!t) return null;
  if (t.includes("table") || t.includes("none")) return "none";
  if (t.includes("scatter")) return "scatter";
  if (t.includes("pie") || t.includes("donut")) return "pie";
  if (t.includes("area")) return "area";
  if (t.includes("line")) return "line";
  if (t.includes("combo")) return "grouped";
  if (t.includes("bar") || t.includes("column")) return "bar";
  return null;
}

function pearson(xs: number[], ys: number[]): number | null {
  const n = xs.length;
  if (n < 3) return null;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let sxy = 0;
  let sxx = 0;
  let syy = 0;
  for (let i = 0; i < n; i += 1) {
    sxy += (xs[i] - mx) * (ys[i] - my);
    sxx += (xs[i] - mx) ** 2;
    syy += (ys[i] - my) ** 2;
  }
  if (sxx === 0 || syy === 0) return null;
  return sxy / Math.sqrt(sxx * syy);
}

function emptyPlan(columns: ColumnInfo[], labelOf: (k: string) => string, kindOf: (k: string) => string | undefined): ChartPlan {
  return {
    form: "none",
    forms: [],
    primary: null,
    measures: [],
    allMeasures: columns.filter((c) => c.role === "measure").map((c) => c.key),
    categoryKeys: [],
    timeKey: null,
    seriesKey: null,
    seriesNames: [],
    data: [],
    sort: "value_desc",
    sortable: false,
    totalCategories: 0,
    shownCategories: 0,
    foldedIntoOther: 0,
    headline: null,
    significance: 0,
    subtitle: "",
    average: null,
    leaderKey: null,
    columns,
    measureLabel: labelOf,
    kindOf,
  };
}

export function planChart(rows: Row[], options: PlanOptions = {}): ChartPlan {
  const kinds = options.kinds ?? {};
  const columns = analyzeColumns(rows, kinds, options.measureLabels ?? {});
  const byKey = new Map(columns.map((c) => [c.key, c]));
  const labelOf = (k: string) => byKey.get(k)?.label ?? humanize(k);
  const kindOf = (k: string) => byKey.get(k)?.kind;
  const plan = emptyPlan(columns, labelOf, kindOf);

  const measures = columns.filter((c) => c.role === "measure").map((c) => c.key);
  const dims = columns.filter((c) => c.role === "dimension").map((c) => c.key);
  const times = columns.filter((c) => c.role === "time").map((c) => c.key);
  const suggested = normalizeSuggestion(options.suggestion);

  if (rows.length < 2 || measures.length === 0 || suggested === "none") return plan;

  // ---- time series -----------------------------------------------------
  // A line/area suggestion over a single unparseable label column (e.g.
  // "Q1 FY24") is still a sequence: keep the row order on a time axis.
  const sequenceKey =
    times.length === 0 && (suggested === "line" || suggested === "area") && dims.length === 1 ? dims[0] : null;
  if (times.length > 0 || sequenceKey) {
    return planTimeSeries(rows, plan, {
      timeKey: times[0] ?? (sequenceKey as string),
      dims: sequenceKey ? [] : dims,
      measures,
      kinds,
      suggested,
      options,
    });
  }

  // ---- no category at all: correlation between two measures -----------
  if (dims.length === 0) {
    if (measures.length < 2) return plan;
    const [x, y] = measures;
    const pairs = rows
      .map((r) => [asNumber(r[x]), asNumber(r[y])] as const)
      .filter((p): p is readonly [number, number] => p[0] != null && p[1] != null);
    const r = pearson(pairs.map((p) => p[0]), pairs.map((p) => p[1]));
    return {
      ...plan,
      form: "scatter",
      forms: ["scatter"],
      primary: x,
      measures: [x, y],
      data: pairs.map(([a, b], i) => ({ label: String(i + 1), key: String(i), [x]: a, [y]: b })),
      headline: scatterHeadline(labelOf(x), labelOf(y), r),
      significance: r != null && Math.abs(r) >= 0.7 ? 2 : 0,
      subtitle: `${labelOf(y)} vs ${labelOf(x)} · ${pairs.length.toLocaleString()} points`,
      scatter: { x, y, r },
    };
  }

  // ---- categorical breakdown -------------------------------------------
  return planCategorical(rows, plan, { dims, measures, kinds, suggested, options });
}

function planCategorical(
  rows: Row[],
  plan: ChartPlan,
  ctx: {
    dims: string[];
    measures: string[];
    kinds: Record<string, string>;
    suggested: ChartForm | null;
    options: PlanOptions;
  },
): ChartPlan {
  const { dims, measures, kinds, suggested, options } = ctx;
  const labelOf = plan.measureLabel;
  const categoryKeys = grainKeys(rows, dims);
  const categoryLabel = (r: Row) => categoryKeys.map((k) => formatCell(r[k], k)).join(" / ");
  const maxCats = options.maxCategories ?? DEFAULT_MAX_CATEGORIES;

  // Which measures to draw by default: one, or a same-kind, same-scale
  // family drawn as grouped bars; otherwise small multiples (never a
  // second y-axis).
  // The multi-measure view available for this data does not depend on what
  // is currently selected, so the Grouped / Split buttons stay reachable
  // after narrowing to one measure.
  const family = measures.slice(0, MAX_MULTIPLES);
  const multi: "grouped" | "multiples" | null =
    family.length < 2
      ? null
      : family.length <= 3 && sameKindFamily(family, kinds) && sameScale(rows, family)
        ? "grouped"
        : "multiples";
  const wantsMulti = options.form === "grouped" || options.form === "multiples";
  let selected: string[];
  if (multi && (wantsMulti || !(options.measure && measures.includes(options.measure)))) {
    selected = family;
  } else if (options.measure && measures.includes(options.measure)) {
    selected = [options.measure];
  } else {
    selected = [measures[0]];
  }

  const primary = selected[0];
  const additive = isAdditive(primary, kinds);
  const sort: SortMode = options.sort ?? "value_desc";

  // Rank by the primary measure (or label), then cap with an "Other" bucket
  // for additive single-measure charts — the tail's total stays visible.
  const indexed = rows.map((r, i) => ({ r, i, v: asNumber(r[primary]) }));
  indexed.sort((a, b) => {
    if (sort === "label") return categoryLabel(a.r).localeCompare(categoryLabel(b.r), undefined, { numeric: true });
    const av = a.v ?? Number.NEGATIVE_INFINITY;
    const bv = b.v ?? Number.NEGATIVE_INFINITY;
    return sort === "value_asc" ? av - bv : bv - av;
  });

  const total = rows.length;
  let shown = indexed;
  let folded = 0;
  if (indexed.length > maxCats) {
    shown = indexed.slice(0, maxCats);
    folded = indexed.length - maxCats;
  }

  const data: ChartDatum[] = shown.map(({ r, i }) => {
    const datum: ChartDatum = { label: categoryLabel(r), key: String(i) };
    for (const m of measures) datum[m] = asNumber(r[m]);
    for (const k of categoryKeys) datum[`__cat_${k}`] = r[k];
    return datum;
  });

  const allAdditive = selected.every((m) => isAdditive(m, kinds));
  // The tail is summarised as an "Other" bar only when it stays smaller
  // than the leader — a combined bucket larger than any real category
  // flattens every real bar into a sliver. Otherwise the subtitle notes it.
  let otherNote = "";
  if (folded > 0 && allAdditive && sort !== "label") {
    const rest = indexed.slice(maxCats);
    const other: ChartDatum = { label: `Other (${folded})`, key: "__other", isOther: true };
    for (const m of measures) {
      other[m] = isAdditive(m, kinds)
        ? rest.reduce((s, x) => s + (asNumber(x.r[m]) ?? 0), 0)
        : null;
    }
    const leaderValue = shown[0]?.v ?? null;
    const otherValue = other[primary];
    if (typeof otherValue === "number" && leaderValue != null && otherValue <= leaderValue) {
      data.push(other);
    } else if (typeof otherValue === "number") {
      otherNote = ` · other ${folded}: ${formatCompact(otherValue)} combined`;
    }
  }

  const values = data.filter((d) => !d.isOther).map((d) => d[primary]).filter((v): v is number => typeof v === "number");
  const average = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  const leader = [...data]
    .filter((d) => !d.isOther && typeof d[primary] === "number")
    .sort((a, b) => (b[primary] as number) - (a[primary] as number))[0];

  const longLabels = data.some((d) => d.label.length > 14);
  const barForm: ChartForm = data.length > 8 || longLabels ? "hbar" : "bar";
  const pieEligible =
    selected.length === 1 && additive && data.length >= 2 && data.length <= 6 && values.every((v) => v >= 0);

  const forms: ChartForm[] = [];
  if (multi) forms.push(...(multi === "grouped" ? (["grouped", "multiples"] as const) : (["multiples"] as const)));
  forms.push("bar", "hbar");
  if (pieEligible) forms.push("pie");

  let form: ChartForm;
  if (options.form && forms.includes(options.form)) form = options.form;
  else if (multi && selected.length > 1) form = multi;
  else if (suggested === "pie" && pieEligible) form = "pie";
  else form = barForm;

  if (form === "bar" || form === "hbar" || form === "pie") selected = [primary];

  const dimLabel = categoryKeys.map((k) => humanize(k)).join(" / ");
  const otherBar = data.some((d) => d.isOther);
  const capNote =
    folded > 0 ? ` · top ${maxCats} of ${total}${otherBar ? ", rest in Other" : otherNote}` : "";
  const measureNames = selected.map(labelOf).join(", ");

  return {
    ...plan,
    form,
    forms,
    primary,
    measures: selected,
    categoryKeys,
    data,
    sort,
    sortable: true,
    totalCategories: total,
    shownCategories: shown.length,
    foldedIntoOther: otherBar ? folded : 0,
    ...scoredHeadline(
      categoricalHeadline(
        rows,
        primary,
        categoryLabel,
        labelOf(primary),
        plan.kindOf(primary),
        additive,
        categoryKeys.map((k) => humanize(k).toLowerCase()).join(" / "),
      ),
    ),
    subtitle: `${measureNames} by ${dimLabel}${capNote}`,
    average,
    leaderKey: leader ? leader.key : null,
  };
}

function planTimeSeries(
  rows: Row[],
  plan: ChartPlan,
  ctx: {
    timeKey: string;
    dims: string[];
    measures: string[];
    kinds: Record<string, string>;
    suggested: ChartForm | null;
    options: PlanOptions;
  },
): ChartPlan {
  const { timeKey, dims, measures, kinds, suggested, options } = ctx;
  const labelOf = plan.measureLabel;

  const dates = rows.map((r) => parseDate(r[timeKey]));
  const allDates = dates.every((d) => d != null);
  const grain = allDates ? detectGrain(dates as Date[]) : null;
  // Order: real dates, else numbers (years), else month names; anything
  // else keeps the query's own ORDER BY rather than sorting labels A–Z.
  const allNumeric = rows.every((r) => asNumber(r[timeKey]) != null);
  const allMonths = rows.every((r) => monthIndex(r[timeKey]) != null);
  const sortValue = (r: Row, i: number): number => {
    const d = dates[i];
    if (allDates && d) return d.getTime();
    if (allNumeric) return asNumber(r[timeKey]) as number;
    if (allMonths) return monthIndex(r[timeKey]) as number;
    return i;
  };
  const timeLabel = (r: Row, i: number) => {
    const d = dates[i];
    if (d && grain) return formatDate(d, grain);
    // A numeric period (year 2025, yyyymm 202510) is a label, not a quantity:
    // no thousands separator ("2,025").
    const n = asNumber(r[timeKey]);
    return n != null && Number.isInteger(n) ? String(n) : formatCell(r[timeKey]);
  };

  const order = rows.map((r, i) => ({ r, i, s: sortValue(r, i) }));
  order.sort((a, b) => a.s - b.s || a.i - b.i);

  const primary = options.measure && measures.includes(options.measure) ? options.measure : measures[0];
  const additive = isAdditive(primary, kinds);
  const seriesKey = dims.length > 0 ? dims[0] : null;

  let data: ChartDatum[];
  let seriesNames: string[] = [];
  let selected: string[];

  if (seriesKey) {
    // Pivot: one line per category (top N by total/mean), the rest folded
    // into "Other" only when the measure is additive.
    const totals = new Map<string, { sum: number; n: number }>();
    for (const { r } of order) {
      const name = formatCell(r[seriesKey], seriesKey);
      const v = asNumber(r[primary]) ?? 0;
      const t = totals.get(name) ?? { sum: 0, n: 0 };
      t.sum += v;
      t.n += 1;
      totals.set(name, t);
    }
    const ranked = [...totals.entries()].sort((a, b) =>
      additive ? b[1].sum - a[1].sum : b[1].sum / b[1].n - a[1].sum / a[1].n,
    );
    const keep = new Set(ranked.slice(0, ranked.length > MAX_SERIES ? MAX_SERIES - 1 : MAX_SERIES).map(([n]) => n));
    const foldOther = ranked.length > MAX_SERIES && additive;
    seriesNames = [...keep];
    if (foldOther) seriesNames.push("Other");

    const byTime = new Map<string, ChartDatum>();
    for (const { r, i } of order) {
      const label = timeLabel(r, i);
      const datum = byTime.get(label) ?? { label, key: label };
      const name = formatCell(r[seriesKey], seriesKey);
      const v = asNumber(r[primary]);
      const target = keep.has(name) ? name : foldOther ? "Other" : null;
      if (target && v != null) {
        datum[target] = additive ? ((datum[target] as number | undefined) ?? 0) + v : v;
      }
      byTime.set(label, datum);
    }
    data = [...byTime.values()];
    selected = [primary];
  } else {
    // Same-scale measures share one chart; otherwise one measure at a time.
    const family = measures.slice(0, 3);
    selected =
      options.measure || family.length === 1 || !(sameKindFamily(family, kinds) && sameScale(rows, family))
        ? [primary]
        : family;
    data = order.map(({ r, i }) => {
      const datum: ChartDatum = { label: timeLabel(r, i), key: String(i) };
      for (const m of measures) datum[m] = asNumber(r[m]);
      return datum;
    });
  }

  let yoy: YearOverYear | null = null;
  if (!seriesKey && allDates) {
    const series = order
      .map(({ r, i }) => ({ date: dates[i] as Date, v: asNumber(r[primary]), label: timeLabel(r, i) }))
      .filter((p): p is { date: Date; v: number; label: string } => p.v != null);
    yoy = yearOverYear(series, additive, plan.kindOf(primary));
  }

  const forms: ChartForm[] = ["line", "area", "bar"];
  let form: ChartForm = "line";
  if (options.form && forms.includes(options.form)) form = options.form;
  else if (suggested === "area" && !seriesKey && selected.length === 1) form = "area";
  else if (data.length <= 3) form = "bar";

  const series = seriesKey ? seriesNames : selected;
  const measureNames = selected.map(labelOf).join(", ");
  const subtitle = seriesKey
    ? `${labelOf(primary)} over ${humanize(timeKey)} by ${humanize(seriesKey)}${seriesNames.length ? ` · ${seriesNames.length} series` : ""}`
    : `${measureNames} over ${humanize(timeKey)} · ${data.length.toLocaleString()} periods`;

  return {
    ...plan,
    form,
    forms,
    primary,
    measures: series,
    categoryKeys: [],
    timeKey,
    seriesKey,
    seriesNames,
    data,
    sort: "label",
    sortable: false,
    totalCategories: data.length,
    shownCategories: data.length,
    ...scoredHeadline(seriesKey ? null : trendHeadline(data, primary, labelOf(primary), plan.kindOf(primary), yoy)),
    subtitle,
    average: null,
    leaderKey: null,
    yoy,
  };
}

const MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

/** "January" / "Feb" / "sept" -> 0..11, so month-name columns sort in
 *  calendar order rather than alphabetically. */
function monthIndex(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const idx = MONTH_NAMES.indexOf(value.trim().slice(0, 3).toLowerCase());
  return idx >= 0 ? idx : null;
}

// -------------------------------------------------------------- headlines

function pctChange(from: number, to: number): number | null {
  if (from === 0) return null;
  return ((to - from) / Math.abs(from)) * 100;
}

function isPercentScale(values: number[], kind?: string): boolean {
  return kind === "percentage" && values.some((v) => Math.abs(v) > 1);
}

function stats(values: number[]): { mean: number; sd: number; cv: number } {
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  const sd = Math.sqrt(values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length);
  return { mean, sd, cv: mean !== 0 ? sd / Math.abs(mean) : Number.POSITIVE_INFINITY };
}

function monthsBetween(a: Date, b: Date): number {
  return (b.getFullYear() - a.getFullYear()) * 12 + (b.getMonth() - a.getMonth());
}

/** Year-over-year change for an evenly spaced monthly or quarterly series:
 *  trailing year vs the year before when there are two full years, else the
 *  latest period vs the same period a year earlier. */
function yearOverYear(
  points: { date: Date; v: number; label: string }[],
  additive: boolean,
  kind?: string,
): YearOverYear | null {
  if (points.length < 3) return null;
  const step = monthsBetween(points[0].date, points[1].date);
  if (step !== 1 && step !== 3) return null;
  for (let i = 2; i < points.length; i += 1) {
    if (monthsBetween(points[i - 1].date, points[i].date) !== step) return null;
  }
  const perYear = 12 / step;
  const pctScale = kind === "percentage" && points.some((p) => Math.abs(p.v) > 1);
  const compare = (now: number, before: number): { change: number; unit: "%" | " pts" } | null => {
    if (pctScale) return { change: now - before, unit: " pts" };
    const c = pctChange(before, now);
    return c == null ? null : { change: c, unit: "%" };
  };
  const agg = (vals: number[]) => {
    const sum = vals.reduce((a, b) => a + b, 0);
    return additive ? sum : sum / vals.length;
  };
  const unitName = step === 1 ? "months" : "quarters";
  if (points.length >= perYear * 2) {
    const recent = points.slice(-perYear).map((p) => p.v);
    const prior = points.slice(-perYear * 2, -perYear).map((p) => p.v);
    const res = compare(agg(recent), agg(prior));
    return res ? { ...res, basis: `last ${perYear} ${unitName} vs the ${perYear} before` } : null;
  }
  if (points.length >= perYear + 1) {
    const last = points[points.length - 1];
    const yearAgo = points[points.length - 1 - perYear];
    const res = compare(last.v, yearAgo.v);
    return res ? { ...res, basis: `${last.label} vs ${yearAgo.label}` } : null;
  }
  return null;
}

type Scored = { text: string; score: number };

function scoredHeadline(h: Scored | string | null): { headline: string | null; significance: number } {
  if (h == null) return { headline: null, significance: 0 };
  if (typeof h === "string") return { headline: h, significance: 0 };
  return { headline: h.text, significance: h.score };
}

function signed(n: number, digits = 1): string {
  return `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(digits)}`;
}

/** Trend headline: direction (year over year when available), the one
 *  period that stands out, and whether the series is otherwise stable. */
function trendHeadline(
  data: ChartDatum[],
  key: string,
  name: string,
  kind: string | undefined,
  yoy: YearOverYear | null,
): Scored | null {
  const pts = data.filter((d) => typeof d[key] === "number");
  if (pts.length < 2) return null;
  const vals = pts.map((d) => d[key] as number);
  const pctScale = isPercentScale(vals, kind);
  const fmt = (n: number) => formatMeasure(n, kind, pctScale);
  const { mean, sd, cv } = stats(vals);
  const first = pts[0];
  const last = pts[pts.length - 1];
  const parts: string[] = [];
  let score = 0;

  if (yoy) {
    const size = Math.abs(yoy.change);
    score = Math.max(score, size >= 10 ? 3 : size >= 5 ? 2 : 0);
    const dir = Math.abs(yoy.change) < (yoy.unit === "%" ? 1 : 0.1) ? "flat" : yoy.change > 0 ? "up" : "down";
    parts.push(
      dir === "flat"
        ? `${name} is flat year over year (${yoy.basis})`
        : `${name} is ${dir} ${Math.abs(yoy.change).toFixed(1)}${yoy.unit} year over year (${yoy.basis})`,
    );
  } else {
    const a = first[key] as number;
    const b = last[key] as number;
    const change = pctScale ? b - a : pctChange(a, b);
    if (change == null || Math.abs(change) < (pctScale ? 0.1 : 1)) {
      parts.push(`${name} ended where it started, around ${fmt(b)} (${first.label} → ${last.label})`);
    } else {
      const unit = pctScale ? " pts" : "%";
      parts.push(`${name} ${change > 0 ? "rose" : "fell"} ${Math.abs(change).toFixed(1)}${unit} from ${first.label} (${fmt(a)}) to ${last.label} (${fmt(b)})`);
      score = Math.max(score, Math.abs(change) >= 15 ? 2 : Math.abs(change) >= 5 ? 1 : 0);
    }
  }

  // The period furthest from the average, if it is genuinely unusual.
  let notable: ChartDatum | null = null;
  let notableDev = 0;
  for (const d of pts) {
    const dev = (d[key] as number) - mean;
    if (Math.abs(dev) > Math.abs(notableDev)) {
      notable = d;
      notableDev = dev;
    }
  }
  const relDev = mean !== 0 ? (notableDev / Math.abs(mean)) * 100 : 0;
  const unusual = notable && sd > 0 && Math.abs(notableDev) / sd >= 1.8 && Math.abs(relDev) >= 8;
  if (unusual && notable) {
    score = Math.max(score, Math.abs(relDev) >= 15 ? 3 : 2);
    parts.push(
      `${notable.label} stands out as the ${notableDev < 0 ? "low" : "high"} at ${fmt(notable[key] as number)} (${signed(relDev, 0)}% vs the ${fmt(mean)} average)`,
    );
  } else if (cv < 0.06) {
    const maxDev = Math.max(...vals.map((v) => Math.abs(v - mean))) / Math.abs(mean) * 100;
    parts.push(`otherwise steady — every period is within ±${Math.max(1, Math.round(maxDev))}% of ${fmt(mean)}`);
  } else {
    const peak = pts.reduce((m, d) => ((d[key] as number) > (m[key] as number) ? d : m), pts[0]);
    parts.push(`peak ${fmt(peak[key] as number)} in ${peak.label}`);
  }
  return { text: `${parts.join("; ")}.`, score };
}

/** Breakdown headline: evenly spread vs concentrated, outliers, leader. */
function categoricalHeadline(
  rows: Row[],
  key: string,
  labelFor: (r: Row) => string,
  name: string,
  kind: string | undefined,
  additive: boolean,
  dimName: string,
): Scored | string | null {
  const pts = rows
    .map((r) => ({ label: labelFor(r), v: asNumber(r[key]) }))
    .filter((p): p is { label: string; v: number } => p.v != null);
  if (pts.length < 2) return null;
  pts.sort((a, b) => b.v - a.v);
  const pctScale = isPercentScale(pts.map((p) => p.v), kind);
  const fmt = (n: number) => formatMeasure(n, kind, pctScale);
  const top = pts[0];
  const bottom = pts[pts.length - 1];
  const { mean, sd, cv } = stats(pts.map((p) => p.v));
  const lname = name.toLowerCase();
  const total = pts.reduce((s, p) => s + p.v, 0);
  const shareable = additive && total > 0 && pts.every((p) => p.v >= 0);

  // Evenly spread: the finding is that nothing stands out.
  if (pts.length >= 4 && cv < 0.08 && mean > 0) {
    const maxDev = Math.max(...pts.map((p) => Math.abs(p.v - mean))) / mean * 100;
    const band = `±${Math.max(1, Math.round(maxDev))}%`;
    return {
      score: 1,
      text: additive
        ? `${name} is evenly spread across all ${pts.length} ${dimName} values — each is within ${band} of the ${fmt(mean)} average, so no single one dominates (${top.label} is only marginally ahead at ${fmt(top.v)}).`
        : `${name} is consistent across ${dimName}: every value sits within ${band} of ${fmt(mean)} (range ${fmt(bottom.v)}–${fmt(top.v)}).`,
    };
  }

  // A clear outlier on either end.
  if (pts.length >= 5 && sd > 0) {
    const zTop = (top.v - mean) / sd;
    const zBottom = (bottom.v - mean) / sd;
    if (zTop >= 2.2) {
      const share = shareable ? `, ${((top.v / total) * 100).toFixed(1)}% of the total` : "";
      return { score: 3, text: `${top.label} stands out on ${lname} at ${fmt(top.v)} — ${(top.v / mean).toFixed(1)}× the ${fmt(mean)} average${share}.` };
    }
    if (zBottom <= -2.2 && mean !== 0) {
      return { score: 3, text: `${bottom.label} lags on ${lname} at ${fmt(bottom.v)} — ${Math.abs(((bottom.v - mean) / mean) * 100).toFixed(0)}% below the ${fmt(mean)} average; ${top.label} leads at ${fmt(top.v)}.` };
    }
  }

  if (shareable) {
    const share = (top.v / total) * 100;
    const top3 = pts.slice(0, 3).reduce((s, p) => s + p.v, 0);
    const top3Note = pts.length > 4 ? `; the top 3 make up ${((top3 / total) * 100).toFixed(0)}%` : "";
    // Concentration is a finding in itself: one category holding a large
    // share of the total matters more than a leader with 7%.
    return {
      score: share >= 40 ? 3 : share >= 25 ? 2 : 0,
      text: `${top.label} leads ${lname} at ${fmt(top.v)} — ${share.toFixed(1)}% of the ${formatCompact(total)} total${top3Note}.`,
    };
  }
  if (additive) {
    return `${top.label} has the highest ${lname} (${fmt(top.v)}); ${bottom.label} the lowest (${fmt(bottom.v)}).`;
  }
  return `${name}: highest ${top.label} (${fmt(top.v)}), lowest ${bottom.label} (${fmt(bottom.v)}), average ${fmt(mean)}.`;
}

function scatterHeadline(x: string, y: string, r: number | null): string | null {
  if (r == null) return null;
  const strength = Math.abs(r) >= 0.7 ? "strong" : Math.abs(r) >= 0.4 ? "moderate" : "weak";
  const dir = r >= 0 ? "positive" : "negative";
  return `${strength[0].toUpperCase()}${strength.slice(1)} ${dir} relationship between ${x} and ${y} (r = ${r.toFixed(2)}).`;
}

// ------------------------------------------------------------------- KPIs

/** Insight tiles for the chat panel: real findings (leader, share, change)
 *  instead of row counts. */
export function buildKpiFacts(rows: Row[], plan: ChartPlan, kinds: Record<string, string> = {}): KpiFact[] {
  const measures = plan.allMeasures;
  if (rows.length === 0 || measures.length === 0) return [];

  if (rows.length === 1) {
    return measures.slice(0, 4).map((m) => {
      const n = asNumber(rows[0][m]);
      const kind = plan.kindOf(m);
      return {
        id: `v-${m}`,
        label: plan.measureLabel(m),
        value: n == null ? formatCell(rows[0][m]) : formatMeasure(n, kind, isPercentScale([n], kind)),
      };
    });
  }

  const primary = plan.primary ?? measures[0];
  const name = plan.measureLabel(primary);
  const kind = plan.kindOf(primary);
  const additive = isAdditive(primary, kinds);
  const values = rows.map((r) => asNumber(r[primary])).filter((v): v is number => v != null);
  if (values.length === 0) return [];
  const pctScale = isPercentScale(values, kind);
  const fmt = (n: number) => formatMeasure(n, kind, pctScale);
  const total = values.reduce((a, b) => a + b, 0);
  const facts: KpiFact[] = [];

  if (plan.timeKey && !plan.seriesKey) {
    const series = plan.data.filter((d) => typeof d[primary] === "number");
    if (series.length >= 2) {
      const last = series[series.length - 1];
      const prev = series[series.length - 2];
      const first = series[0];
      const lv = last[primary] as number;
      const pv = prev[primary] as number;
      const fv = first[primary] as number;
      const step = additive ? pctChange(pv, lv) : lv - pv;
      facts.push({
        id: "latest",
        label: `Latest · ${name}`,
        value: fmt(lv),
        sub: step == null ? last.label : `${step >= 0 ? "▲" : "▼"} ${formatCompact(Math.abs(step))}${additive ? "%" : pctScale ? " pts" : ""} vs ${prev.label}`,
        direction: step == null || step === 0 ? "flat" : step > 0 ? "up" : "down",
      });
      const overall = additive ? pctChange(fv, lv) : lv - fv;
      if (plan.yoy) {
        facts.push({
          id: "yoy",
          label: "Year over year",
          value: `${signed(plan.yoy.change)}${plan.yoy.unit}`,
          sub: plan.yoy.basis,
          direction: plan.yoy.change === 0 ? "flat" : plan.yoy.change > 0 ? "up" : "down",
        });
      } else if (overall != null) {
        facts.push({
          id: "change",
          label: "Change over period",
          value: `${overall >= 0 ? "+" : "−"}${formatCompact(Math.abs(overall))}${additive ? "%" : pctScale ? " pts" : ""}`,
          sub: `${first.label} → ${last.label}`,
          direction: overall === 0 ? "flat" : overall > 0 ? "up" : "down",
        });
      }
      const peakPt = series.reduce((m, d) => ((d[primary] as number) > (m[primary] as number) ? d : m), series[0]);
      facts.push({ id: "peak", label: `Peak · ${name}`, value: fmt(peakPt[primary] as number), sub: peakPt.label });
    }
    facts.push(
      additive
        ? { id: "total", label: `Total · ${name}`, value: fmt(total), sub: `${series.length} periods` }
        : { id: "avg", label: `Average · ${name}`, value: fmt(total / values.length), sub: `${series.length} periods` },
    );
    return facts.slice(0, 4);
  }

  if (plan.timeKey && plan.seriesKey) {
    // One line per category: lead with the whole, the top category, and
    // how the latest period compares with the first.
    const seriesKey = plan.seriesKey;
    const byName = new Map<string, number>();
    for (const r of rows) {
      const v = asNumber(r[primary]);
      if (v == null) continue;
      const k = formatCell(r[seriesKey], seriesKey);
      byName.set(k, (byName.get(k) ?? 0) + v);
    }
    const ranked = [...byName.entries()].sort((a, b) => b[1] - a[1]);
    const periodTotal = (d: ChartDatum) =>
      plan.seriesNames.reduce((s, n) => s + (typeof d[n] === "number" ? (d[n] as number) : 0), 0);
    facts.push(
      additive
        ? { id: "total", label: `Total · ${name}`, value: fmt(total), sub: `${plan.data.length} periods` }
        : { id: "avg", label: `Average · ${name}`, value: fmt(total / values.length) },
    );
    if (ranked[0] && additive) {
      facts.push({
        id: "top",
        label: `Top ${humanize(seriesKey)}`,
        value: ranked[0][0],
        sub: `${fmt(ranked[0][1])} · ${total > 0 ? ((ranked[0][1] / total) * 100).toFixed(1) : "0"}% of total`,
        text: true,
      });
    }
    if (additive && plan.data.length >= 2) {
      const first = plan.data[0];
      const last = plan.data[plan.data.length - 1];
      const change = pctChange(periodTotal(first), periodTotal(last));
      facts.push({ id: "latest", label: `Latest · ${name}`, value: fmt(periodTotal(last)), sub: last.label });
      if (change != null) {
        facts.push({
          id: "change",
          label: "Change over period",
          value: `${change >= 0 ? "+" : "−"}${Math.abs(change).toFixed(1)}%`,
          sub: `${first.label} → ${last.label}`,
          direction: change === 0 ? "flat" : change > 0 ? "up" : "down",
        });
      }
    }
    return facts.slice(0, 4);
  }

  if (plan.categoryKeys.length > 0) {
    const labelFor = (r: Row) => plan.categoryKeys.map((k) => formatCell(r[k], k)).join(" / ");
    const ranked = rows
      .map((r) => ({ label: labelFor(r), v: asNumber(r[primary]) }))
      .filter((p): p is { label: string; v: number } => p.v != null)
      .sort((a, b) => b.v - a.v);
    const top = ranked[0];
    const bottom = ranked[ranked.length - 1];
    const dimName = plan.categoryKeys.map(humanize).join(" / ");
    if (additive) {
      facts.push({ id: "total", label: `Total · ${name}`, value: fmt(total), sub: `across ${ranked.length} ${dimName.toLowerCase()}` });
    } else {
      facts.push({ id: "avg", label: `Average · ${name}`, value: fmt(total / values.length), sub: `across ${ranked.length} ${dimName.toLowerCase()}` });
    }
    if (top) {
      const share = additive && total > 0 && top.v >= 0 ? ` · ${((top.v / total) * 100).toFixed(1)}% of total` : "";
      facts.push({ id: "top", label: `Top ${dimName}`, value: top.label, sub: `${fmt(top.v)}${share}`, text: true });
    }
    if (bottom && bottom !== top) {
      facts.push({ id: "bottom", label: `Lowest ${dimName}`, value: bottom.label, sub: fmt(bottom.v), text: true });
    }
    if (additive && ranked.length > 4 && total > 0) {
      const top3 = ranked.slice(0, 3).reduce((s, p) => s + p.v, 0);
      facts.push({ id: "conc", label: "Top 3 share", value: `${((top3 / total) * 100).toFixed(0)}%`, sub: `of ${name.toLowerCase()}` });
    } else if (top && bottom && bottom !== top) {
      facts.push({ id: "spread", label: "Spread", value: fmt(top.v - bottom.v), sub: "highest − lowest" });
    }
    return facts.slice(0, 4);
  }

  facts.push(
    additive
      ? { id: "total", label: `Total · ${name}`, value: fmt(total) }
      : { id: "avg", label: `Average · ${name}`, value: fmt(total / values.length) },
  );
  facts.push({ id: "max", label: `Max · ${name}`, value: fmt(Math.max(...values)) });
  facts.push({ id: "min", label: `Min · ${name}`, value: fmt(Math.min(...values)) });
  if (plan.scatter?.r != null) {
    facts.push({ id: "r", label: "Correlation (r)", value: plan.scatter.r.toFixed(2) });
  }
  return facts.slice(0, 4);
}
