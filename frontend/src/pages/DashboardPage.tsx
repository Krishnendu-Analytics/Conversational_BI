import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { DashboardGrid } from "@/components/DashboardGrid";
import {
  ApiError,
  fetchSemanticCatalog,
  getDashboard,
  type DashboardDetail,
  type DashboardFilter,
  type SemanticDimension,
  updateDashboardFilters,
} from "@/lib/api";

/** Full-page dashboard view with manual refresh. */
export function DashboardPage() {
  const { dashboardId } = useParams<{ dashboardId: string }>();
  const [dashboard, setDashboard] = useState<DashboardDetail | null>(null);
  const [dimensions, setDimensions] = useState<SemanticDimension[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterBusy, setFilterBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedField, setSelectedField] = useState("");
  const [filterValue, setFilterValue] = useState("");

  const loadDashboard = useCallback(async () => {
    if (!dashboardId) return null;
    setError(null);
    try {
      const data = await getDashboard(dashboardId);
      setDashboard(data);
      return data;
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load dashboard",
      );
      setDashboard(null);
      return null;
    }
  }, [dashboardId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      const data = await loadDashboard();
      if (!cancelled && data?.connection_id) {
        try {
          const catalog = await fetchSemanticCatalog(data.connection_id);
          if (!cancelled) {
            setDimensions(catalog.dimensions ?? []);
            if (catalog.dimensions?.length) {
              setSelectedField((prev) => prev || catalog.dimensions[0].name);
            }
          }
        } catch {
          if (!cancelled) setDimensions([]);
        }
      }
      if (!cancelled) setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [loadDashboard]);

  async function handleApplyFilter() {
    if (!dashboardId || !selectedField || !filterValue.trim()) return;
    setFilterBusy(true);
    setError(null);
    const filters: DashboardFilter[] = [
      { field: selectedField, operator: "eq", value: filterValue.trim() },
    ];
    try {
      const data = await updateDashboardFilters(dashboardId, filters);
      setDashboard(data);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not update filters",
      );
    } finally {
      setFilterBusy(false);
    }
  }

  async function handleClearFilters() {
    if (!dashboardId) return;
    setFilterBusy(true);
    setError(null);
    try {
      const data = await updateDashboardFilters(dashboardId, []);
      setDashboard(data);
      setFilterValue("");
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not clear filters",
      );
    } finally {
      setFilterBusy(false);
    }
  }

  if (!dashboardId) {
    return (
      <div className="dashboard-page">
        <p className="form-error" role="alert">
          No dashboard selected.
        </p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="dashboard-page">
        <p className="loading-copy">Loading dashboard…</p>
      </div>
    );
  }

  if (error && !dashboard) {
    return (
      <div className="dashboard-page">
        <p className="form-error" role="alert">
          {error}
        </p>
      </div>
    );
  }

  if (!dashboard) {
    return (
      <div className="dashboard-page">
        <p className="form-error" role="alert">
          Dashboard not found.
        </p>
      </div>
    );
  }

  const spec = {
    title: dashboard.title ?? dashboard.name,
    widgets: dashboard.widgets,
    layout: dashboard.layout,
    filters: dashboard.filters,
  };

  return (
    <div className="dashboard-page">
      {dimensions.length > 0 ? (
        <div className="dashboard-page-filters">
          <label className="field">
            <span className="field-label">Filter</span>
            <select
              value={selectedField}
              onChange={(e) => setSelectedField(e.target.value)}
              disabled={filterBusy}
            >
              {dimensions.map((dim) => (
                <option key={dim.id} value={dim.name}>
                  {dim.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Value</span>
            <input
              type="text"
              value={filterValue}
              onChange={(e) => setFilterValue(e.target.value)}
              placeholder="e.g. West"
              disabled={filterBusy}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => void handleApplyFilter()}
            disabled={filterBusy || !filterValue.trim()}
          >
            {filterBusy ? "Applying…" : "Apply filter"}
          </button>
          {dashboard.filters && dashboard.filters.length > 0 ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => void handleClearFilters()}
              disabled={filterBusy}
            >
              Clear filters
            </button>
          ) : null}
        </div>
      ) : null}
      <DashboardGrid dashboard={spec} error={error} />
    </div>
  );
}
