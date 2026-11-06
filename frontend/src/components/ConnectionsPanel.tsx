import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useAuth } from "@/lib/auth";
import {
  ApiError,
  buildConnectionUrl,
  createConnection,
  deleteConnection,
  discoverConnection,
  getDiscoveryStatus,
  listConnectionSchemas,
  listConnections,
  listDatasets,
  parseConnectionUrl,
  resolveConnectionUrl,
  testConnection,
  updateConnection,
  uploadDataset,
  type Connection,
  type DatabaseType,
  type Dataset,
  type SchemaDiscoverScope,
} from "@/lib/api";

const DB_OPTIONS: { value: DatabaseType; label: string; defaultPort: string }[] = [
  { value: "postgres", label: "PostgreSQL", defaultPort: "5432" },
  { value: "mysql", label: "MySQL", defaultPort: "3306" },
  { value: "mssql", label: "SQL Server", defaultPort: "1433" },
];

const emptyForm = {
  name: "",
  db_type: "postgres" as DatabaseType,
  host: "localhost",
  port: "5432",
  database: "",
  username: "",
  password: "",
  connection_url: "",
};

type FormState = typeof emptyForm;
type InputMode = "fields" | "url";

export type ConnectionsView = "list" | "create" | "edit";

type Props = {
  view: ConnectionsView;
  editing: Connection | null;
  onViewChange: (view: ConnectionsView, editing?: Connection | null) => void;
  /** Hide the list toolbar CTA when the parent page already provides one. */
  hideListCta?: boolean;
};

function dbLabel(dbType: DatabaseType) {
  if (dbType === "csv") return "CSV dataset";
  if (dbType === "excel") return "Excel dataset";
  return DB_OPTIONS.find((o) => o.value === dbType)?.label ?? dbType;
}

function urlPlaceholder(dbType: DatabaseType) {
  // Use the same reader credentials as backend init SQL + CONNECTIONS.md.
  if (dbType === "mysql") return "mysql://reader:reader@localhost:3307/customerdb";
  if (dbType === "mssql") return "mssql://reader:Reader1234!@localhost:1433/customerdb";
  return "postgresql://reader:reader@localhost:5433/customerdb";
}

export function ConnectionsPanel({
  view,
  editing,
  onViewChange,
  hideListCta = false,
}: Props) {
  const { user } = useAuth();
  // Members only see connections their departments grant; managing them is admin work.
  const canManage = user?.role === "admin";
  const [connections, setConnections] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [inputMode, setInputMode] = useState<InputMode>("fields");
  const [busy, setBusy] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [discoveringId, setDiscoveringId] = useState<string | null>(null);
  const [discoverTarget, setDiscoverTarget] = useState<Connection | null>(null);
  const [discoverScope, setDiscoverScope] = useState<SchemaDiscoverScope>("default");
  const [availableSchemas, setAvailableSchemas] = useState<string[]>([]);
  const [defaultSchema, setDefaultSchema] = useState("public");
  const [selectedSchemas, setSelectedSchemas] = useState<string[]>([]);
  const [extraSchemas, setExtraSchemas] = useState("");
  const [loadingSchemas, setLoadingSchemas] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [statusById, setStatusById] = useState<Record<string, string>>({});
  const [uploadName, setUploadName] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [datasetsByConnection, setDatasetsByConnection] = useState<
    Record<string, Dataset[]>
  >({});

  const refresh = useCallback(async () => {
    const rows = await listConnections();
    setConnections(rows);
    const fileRows = rows.filter(
      (row) => row.db_type === "csv" || row.db_type === "excel",
    );
    const entries = await Promise.all(
      fileRows.map(async (row) => {
        try {
          const { datasets } = await listDatasets(row.id);
          return [row.id, datasets] as const;
        } catch {
          return [row.id, []] as const;
        }
      }),
    );
    setDatasetsByConnection(Object.fromEntries(entries));
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await refresh();
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load connections");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  useEffect(() => {
    setError(null);
    setConfirmDeleteId(null);
    setInputMode("fields");

    if (view === "edit" && editing) {
      setForm({
        ...emptyForm,
        name: editing.name,
        db_type: editing.db_type,
        host: "",
        port: DB_OPTIONS.find((o) => o.value === editing.db_type)?.defaultPort ?? "5432",
        connection_url: "",
      });
      return;
    }

    if (view === "create") {
      setSuccess(null);
      setForm(emptyForm);
    }
  }, [view, editing]);

  function updateField<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => {
      const next = { ...prev, [key]: value };
      if (key === "db_type") {
        const opt = DB_OPTIONS.find((o) => o.value === value);
        if (opt) next.port = opt.defaultPort;
      }
      return next;
    });
  }

  function switchInputMode(next: InputMode) {
    setError(null);
    if (next === inputMode) return;

    if (next === "url") {
      const hasFields =
        form.host.trim() &&
        form.database.trim() &&
        form.username.trim() &&
        form.password.trim();
      setForm((prev) => ({
        ...prev,
        connection_url: hasFields ? buildConnectionUrl(prev) : prev.connection_url,
      }));
      setInputMode("url");
      return;
    }

    if (form.connection_url.trim()) {
      try {
        const parsed = parseConnectionUrl(form.connection_url);
        setForm((prev) => ({
          ...prev,
          ...parsed,
          name: prev.name,
          connection_url: prev.connection_url,
        }));
      } catch {
        // Keep typed fields; user can fix URL later.
      }
    }
    setInputMode("fields");
  }

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setSuccess(null);

    try {
      const resolved = resolveConnectionUrl({ mode: inputMode, ...form });
      const created = await createConnection({
        name: form.name.trim(),
        db_type: resolved.db_type,
        connection_url: resolved.connection_url,
      });
      // The backend already auto-discovers every schema on creation —
      // poll that job rather than firing a second, redundant discovery.
      setStatusById((prev) => ({
        ...prev,
        [created.id]: "Discovering schema…",
      }));
      try {
        const finalStatus = await pollDiscoveryStatus(created.id);
        const msg =
          finalStatus.status === "failed"
            ? finalStatus.error_message || "Schema discovery failed"
            : `Schema ready · ${finalStatus.tables_discovered} table(s)`;
        setStatusById((prev) => ({ ...prev, [created.id]: msg }));
        setSuccess(
          finalStatus.status === "failed"
            ? `“${created.name}” connected, but ${msg.toLowerCase()}`
            : `“${created.name}” connected. Schema discovered (${finalStatus.tables_discovered} tables).`,
        );
      } catch {
        setSuccess(
          `“${created.name}” connected. Run Discover schema before asking questions.`,
        );
      }
      await refresh();
      onViewChange("list");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Could not save connection",
      );
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdate(e: FormEvent) {
    e.preventDefault();
    if (!editing) return;

    setBusy(true);
    setError(null);
    setSuccess(null);

    const name = form.name.trim();

    try {
      if (inputMode === "url") {
        if (!form.connection_url.trim()) {
          if (name !== editing.name) {
            await updateConnection(editing.id, { name });
          } else {
            setError("Paste a connection URL, or change the name.");
            return;
          }
        } else {
          const resolved = resolveConnectionUrl({ mode: "url", ...form });
          await updateConnection(editing.id, {
            name,
            db_type: resolved.db_type,
            connection_url: resolved.connection_url,
          });
        }
      } else {
        const credentialsFilled =
          form.host.trim() &&
          form.port.trim() &&
          form.database.trim() &&
          form.username.trim() &&
          form.password.trim();

        if (credentialsFilled) {
          await updateConnection(editing.id, {
            name,
            db_type: form.db_type,
            connection_url: buildConnectionUrl(form),
          });
        } else if (name !== editing.name) {
          await updateConnection(editing.id, { name });
        } else {
          setError(
            "Change the name, paste a URL, or enter full credentials to update the connection.",
          );
          return;
        }
      }

      await refresh();
      setSuccess("Connection updated.");
      onViewChange("list");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Could not update connection",
      );
    } finally {
      setBusy(false);
    }
  }

  async function handleTest(id: string) {
    setTestingId(id);
    setError(null);
    try {
      const result = await testConnection(id);
      setStatusById((prev) => ({ ...prev, [id]: result.message }));
      if (!result.ok) setError(result.message);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Test failed";
      setStatusById((prev) => ({ ...prev, [id]: message }));
      setError(message);
    } finally {
      setTestingId(null);
    }
  }

  async function openDiscover(conn: Connection) {
    setDiscoverTarget(conn);
    // Pre-select whatever scope this connection last discovered with,
    // defaulting to "all" so multi-schema databases aren't silently
    // narrowed to a single schema.
    setDiscoverScope(conn.discover_scope ?? "all");
    setSelectedSchemas([]);
    setExtraSchemas("");
    setAvailableSchemas([]);
    setDefaultSchema(conn.db_type === "mysql" ? conn.name : "public");
    setError(null);
    setLoadingSchemas(true);
    try {
      const catalog = await listConnectionSchemas(conn.id);
      setAvailableSchemas(catalog.schemas);
      setDefaultSchema(catalog.default_schema);
      setSelectedSchemas(
        catalog.default_schema ? [catalog.default_schema] : catalog.schemas.slice(0, 1),
      );
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not list schemas",
      );
      setDiscoverTarget(null);
    } finally {
      setLoadingSchemas(false);
    }
  }

  function closeDiscover() {
    if (discoveringId) return;
    setDiscoverTarget(null);
  }

  function toggleSchema(name: string) {
    setSelectedSchemas((prev) =>
      prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name],
    );
  }

  async function handleDiscoverConfirm() {
    if (!discoverTarget) return;
    const id = discoverTarget.id;
    const typed = extraSchemas
      .split(/[,;\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    const schemas =
      discoverScope === "selected"
        ? Array.from(new Set([...selectedSchemas, ...typed]))
        : [];

    if (discoverScope === "selected" && schemas.length === 0) {
      setError("Select or type at least one schema.");
      return;
    }

    setDiscoveringId(id);
    setError(null);
    try {
      const schema = await discoverConnection(id, {
        scope: discoverScope,
        schemas,
      });

      // Discovering every schema runs as a background job — the initial
      // response has no tables yet. Poll until it actually finishes
      // instead of reporting "0 table(s)" for a job still in progress.
      if (schema.status === "queued" || schema.status === "running") {
        setStatusById((prev) => ({
          ...prev,
          [id]: "Discovering schema…",
        }));
        const finalStatus = await pollDiscoveryStatus(id);
        const msg =
          finalStatus.status === "failed"
            ? finalStatus.error_message || "Schema discovery failed"
            : `Schema ready · ${finalStatus.tables_discovered} table(s)`;
        setStatusById((prev) => ({ ...prev, [id]: msg }));
        if (finalStatus.status === "failed") {
          setError(msg);
        } else {
          setSuccess(msg);
          setDiscoverTarget(null);
        }
        return;
      }

      const used =
        schema.schemas_used && schema.schemas_used.length > 0
          ? ` · ${schema.schemas_used.join(", ")}`
          : "";
      const msg = `Schema ready · ${schema.tables.length} table(s)${used}`;
      setStatusById((prev) => ({ ...prev, [id]: msg }));
      setSuccess(msg);
      setDiscoverTarget(null);
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "Schema discovery failed";
      setStatusById((prev) => ({ ...prev, [id]: message }));
      setError(message);
    } finally {
      setDiscoveringId(null);
    }
  }

  /** Poll a background discovery run until it reaches a terminal status. */
  async function pollDiscoveryStatus(
    connectionId: string,
    { intervalMs = 1500, maxAttempts = 40 } = {},
  ) {
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      const status = await getDiscoveryStatus(connectionId);
      if (status.status !== "queued" && status.status !== "running") {
        return status;
      }
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
    return getDiscoveryStatus(connectionId);
  }

  async function handleDelete(id: string) {
    setDeletingId(id);
    setError(null);
    try {
      await deleteConnection(id);
      setConfirmDeleteId(null);
      await refresh();
      setSuccess("Connection removed.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not delete connection");
    } finally {
      setDeletingId(null);
    }
  }

  if (view === "create" || view === "edit") {
    const isEdit = view === "edit";
    return (
      <section className="connections-stage">
        <p className="lede tight">
          {isEdit
            ? "Rename this connection, or update access with a URL or form fields. Password is never shown."
            : "Paste a connection URL or fill in the fields. We test before saving — only a successful test is stored."}
        </p>

        <form
          className="auth-form connection-form"
          onSubmit={(e) => void (isEdit ? handleUpdate(e) : handleCreate(e))}
        >
          <label className="field">
            <span>Connection name</span>
            <input
              value={form.name}
              onChange={(e) => updateField("name", e.target.value)}
              minLength={2}
              required
              placeholder="Customer analytics"
            />
          </label>

          <fieldset className="db-type-fieldset">
            <legend>How do you want to connect?</legend>
            <div className="mode-tabs" role="tablist" aria-label="Credential input mode">
              <button
                type="button"
                role="tab"
                className={`mode-tab ${inputMode === "fields" ? "active" : ""}`}
                aria-selected={inputMode === "fields"}
                onClick={() => switchInputMode("fields")}
              >
                Form fields
              </button>
              <button
                type="button"
                role="tab"
                className={`mode-tab ${inputMode === "url" ? "active" : ""}`}
                aria-selected={inputMode === "url"}
                onClick={() => switchInputMode("url")}
              >
                Connection URL
              </button>
            </div>
          </fieldset>

          {inputMode === "url" ? (
            <label className="field">
              <span>Database URL{isEdit ? " (optional)" : ""}</span>
              <input
                value={form.connection_url}
                onChange={(e) => updateField("connection_url", e.target.value)}
                required={!isEdit}
                spellCheck={false}
                autoComplete="off"
                placeholder={urlPlaceholder(form.db_type)}
              />
              <p className="form-hint">
                Example: postgresql://user:pass@host:5432/dbname — engine is detected from the
                scheme.
              </p>
            </label>
          ) : (
            <>
              <fieldset className="db-type-fieldset">
                <legend>Database type</legend>
                <div className="mode-tabs" role="radiogroup" aria-label="Database type">
                  {DB_OPTIONS.map((opt) => (
                    <button
                      key={opt.value}
                      type="button"
                      role="radio"
                      aria-checked={form.db_type === opt.value}
                      className={`mode-tab ${form.db_type === opt.value ? "active" : ""}`}
                      onClick={() => updateField("db_type", opt.value)}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
              </fieldset>

              <div className="field-row">
                <label className="field">
                  <span>Host{isEdit ? " (optional)" : ""}</span>
                  <input
                    value={form.host}
                    onChange={(e) => updateField("host", e.target.value)}
                    required={!isEdit}
                    placeholder="localhost"
                  />
                </label>
                <label className="field field-port">
                  <span>Port</span>
                  <input
                    value={form.port}
                    onChange={(e) => updateField("port", e.target.value)}
                    required={!isEdit}
                    inputMode="numeric"
                  />
                </label>
              </div>

              <label className="field">
                <span>Database{isEdit ? " (optional)" : ""}</span>
                <input
                  value={form.database}
                  onChange={(e) => updateField("database", e.target.value)}
                  required={!isEdit}
                  placeholder="customerdb"
                />
              </label>

              <div className="field-row">
                <label className="field">
                  <span>Username{isEdit ? " (optional)" : ""}</span>
                  <input
                    value={form.username}
                    onChange={(e) => updateField("username", e.target.value)}
                    required={!isEdit}
                    autoComplete="off"
                  />
                </label>
                <label className="field">
                  <span>Password{isEdit ? " (optional)" : ""}</span>
                  <input
                    type="password"
                    value={form.password}
                    onChange={(e) => updateField("password", e.target.value)}
                    required={!isEdit}
                    autoComplete="new-password"
                    placeholder={isEdit ? "Leave blank to keep current" : undefined}
                  />
                </label>
              </div>
            </>
          )}

          {error && <p className="form-error">{error}</p>}
          {success && <p className="form-success">{success}</p>}

          <div className="form-actions">
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => onViewChange("list")}
              disabled={busy}
            >
              Back to list
            </button>
            <button className="btn btn-primary" type="submit" disabled={busy}>
              {busy
                ? isEdit
                  ? "Saving…"
                  : "Testing & saving…"
                : isEdit
                  ? "Save changes"
                  : "Save connection"}
            </button>
          </div>
        </form>
      </section>
    );
  }

  return (
    <section className="connections-stage">
      <div className="connections-list-toolbar">
        <p className="lede tight" style={{ margin: 0 }}>
          Manage databases linked to this workspace. Credentials stay encrypted and are never
          shown again.
        </p>
        {!hideListCta ? (
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => onViewChange("create")}
          >
            Add connection
          </button>
        ) : null}
      </div>

      {error && <p className="form-error">{error}</p>}
      {success && <p className="form-success">{success}</p>}

      {canManage && (
      <form
        className="panel-card"
        style={{ marginBottom: "1rem" }}
        onSubmit={(e) => {
          e.preventDefault();
          if (!uploadFile || !uploadName.trim()) return;
          void (async () => {
            setUploading(true);
            setError(null);
            setSuccess(null);
            try {
              const result = await uploadDataset(uploadName.trim(), uploadFile);
              setSuccess(
                `Uploaded ${result.datasets.length} dataset(s) — status ${result.discovery_status}`,
              );
              setUploadName("");
              setUploadFile(null);
              await refresh();
            } catch (err) {
              setError(err instanceof ApiError ? err.message : "Upload failed");
            } finally {
              setUploading(false);
            }
          })();
        }}
      >
        <h3 style={{ marginTop: 0 }}>Upload CSV or Excel</h3>
        <p className="form-hint">
          File-backed datasets use the same chat, semantic, and dashboard pipeline as SQL
          connections.
        </p>
        <label className="field">
          <span>Dataset name</span>
          <input
            value={uploadName}
            onChange={(e) => setUploadName(e.target.value)}
            placeholder="Sales data"
            required
          />
        </label>
        <label className="field">
          <span>File (.csv or .xlsx)</span>
          <input
            type="file"
            accept=".csv,.xlsx,.xlsm,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
            required
          />
        </label>
        <button className="btn btn-primary" type="submit" disabled={uploading || !uploadFile}>
          {uploading ? "Uploading…" : "Upload dataset"}
        </button>
      </form>
      )}

      <div className="connection-list">
        {loading ? (
          <p className="form-hint">Loading connections…</p>
        ) : connections.length === 0 ? (
          <div className="empty-state">
            <strong>No connections yet</strong>
            {canManage ? (
              <>
                <p className="form-hint">
                  Add a database to start asking questions in plain English.
                </p>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => onViewChange("create")}
                >
                  Add your first connection
                </button>
              </>
            ) : (
              <p className="form-hint">
                You don't have access to any data yet. Ask your organization admin to add
                you to a department.
              </p>
            )}
          </div>
        ) : (
          <ul>
            {connections.map((conn) => (
              <li key={conn.id}>
                <div className="connection-list-main">
                  <strong>{conn.name}</strong>
                  <span className="connection-meta">
                    {dbLabel(conn.db_type)}
                    <span className="dot" />
                    ID {conn.id.slice(0, 8)}
                  </span>
                  {statusById[conn.id] && (
                    <span className="connection-test-msg">{statusById[conn.id]}</span>
                  )}
                  {(conn.db_type === "csv" || conn.db_type === "excel") && (
                    <span className="connection-meta">File-backed dataset</span>
                  )}
                  {(datasetsByConnection[conn.id] || []).map((ds) => (
                    <span key={ds.id} className="connection-meta">
                      {ds.display_name} — {ds.status}
                      {ds.ingestion_error ? ` (${ds.ingestion_error})` : ""}
                    </span>
                  ))}
                  {confirmDeleteId === conn.id && (
                    <div className="confirm-delete">
                      <span>Delete this connection permanently?</span>
                      <div className="connection-actions">
                        <button
                          type="button"
                          className="btn btn-danger"
                          disabled={deletingId === conn.id}
                          onClick={() => void handleDelete(conn.id)}
                        >
                          {deletingId === conn.id ? "Deleting…" : "Confirm delete"}
                        </button>
                        <button
                          type="button"
                          className="btn btn-ghost"
                          onClick={() => setConfirmDeleteId(null)}
                          disabled={deletingId === conn.id}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </div>
                {confirmDeleteId !== conn.id && (
                  <div className="connection-actions">
                    <button
                      type="button"
                      className="btn btn-ghost"
                      disabled={testingId === conn.id}
                      onClick={() => void handleTest(conn.id)}
                    >
                      {testingId === conn.id ? "Testing…" : "Test"}
                    </button>
                    {canManage && (
                      <>
                        <button
                          type="button"
                          className="btn btn-ghost"
                          disabled={discoveringId === conn.id || loadingSchemas}
                          onClick={() => void openDiscover(conn)}
                        >
                          {discoveringId === conn.id ? "Discovering…" : "Discover schema"}
                        </button>
                        <button
                          type="button"
                          className="btn btn-ghost"
                          onClick={() => onViewChange("edit", conn)}
                        >
                          Edit
                        </button>
                        <button
                          type="button"
                          className="btn btn-ghost btn-danger-ghost"
                          onClick={() => setConfirmDeleteId(conn.id)}
                        >
                          Delete
                        </button>
                      </>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {discoverTarget && (
        <div
          className="modal-backdrop"
          role="presentation"
          onClick={closeDiscover}
        >
          <div
            className="modal-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="discover-schema-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-header">
              <h2 id="discover-schema-title">Discover schema</h2>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={closeDiscover}
                disabled={!!discoveringId}
              >
                Close
              </button>
            </div>
            <p className="muted discover-schema-lead">
              Choose which schemas to load for{" "}
              <strong>{discoverTarget.name}</strong>.
            </p>
            {loadingSchemas ? (
              <p className="muted">Loading schemas…</p>
            ) : (
              <div className="discover-schema-options">
                <label className="discover-option">
                  <input
                    type="radio"
                    name="discover-scope"
                    checked={discoverScope === "default"}
                    onChange={() => setDiscoverScope("default")}
                  />
                  <span>
                    Default only
                    <small>
                      {defaultSchema
                        ? ` (${defaultSchema})`
                        : " — provider default"}
                    </small>
                  </span>
                </label>
                <label className="discover-option">
                  <input
                    type="radio"
                    name="discover-scope"
                    checked={discoverScope === "all"}
                    onChange={() => setDiscoverScope("all")}
                  />
                  <span>
                    All schemas
                    <small>
                      {availableSchemas.length
                        ? ` (${availableSchemas.length})`
                        : ""}
                    </small>
                  </span>
                </label>
                <label className="discover-option">
                  <input
                    type="radio"
                    name="discover-scope"
                    checked={discoverScope === "selected"}
                    onChange={() => setDiscoverScope("selected")}
                  />
                  <span>Selected schemas</span>
                </label>

                {discoverScope === "selected" && (
                  <div className="discover-schema-pick">
                    {availableSchemas.length === 0 ? (
                      <p className="muted">No schemas returned by the database.</p>
                    ) : (
                      <ul className="discover-schema-list">
                        {availableSchemas.map((name) => (
                          <li key={name}>
                            <label>
                              <input
                                type="checkbox"
                                checked={selectedSchemas.includes(name)}
                                onChange={() => toggleSchema(name)}
                              />
                              {name}
                              {name === defaultSchema ? " (default)" : ""}
                            </label>
                          </li>
                        ))}
                      </ul>
                    )}
                    <label className="discover-extra-label">
                      Or type schema names
                      <input
                        type="text"
                        value={extraSchemas}
                        onChange={(e) => setExtraSchemas(e.target.value)}
                        placeholder="sales, analytics"
                        autoComplete="off"
                      />
                    </label>
                  </div>
                )}
              </div>
            )}
            <div className="connection-actions discover-schema-actions">
              <button
                type="button"
                className="btn btn-ghost"
                onClick={closeDiscover}
                disabled={!!discoveringId}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!!discoveringId || loadingSchemas}
                onClick={() => void handleDiscoverConfirm()}
              >
                {discoveringId ? "Discovering…" : "Discover"}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
