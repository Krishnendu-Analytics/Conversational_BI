/**
 * API helpers for the backend.
 * Vite proxies /api → FastAPI (see vite.config.ts).
 */

const ACCESS_KEY = "cbi_access_token";
const REFRESH_KEY = "cbi_refresh_token";

export type User = {
  id: string;
  email: string;
  tenant_id: string;
  tenant_name: string;
  is_verified: boolean;
  role: string;
  /** Platform operator: can create organizations. */
  is_super_admin?: boolean;
  departments?: { id: string; name: string }[];
};

export type Tokens = {
  access_token: string;
  refresh_token: string;
  tenant_id: string;
  user_id: string;
  email: string;
};

export function getAccessToken() {
  return localStorage.getItem(ACCESS_KEY);
}

export function getRefreshToken() {
  return localStorage.getItem(REFRESH_KEY);
}

export function saveTokens(access: string, refresh: string) {
  localStorage.setItem(ACCESS_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
}

export function clearTokens() {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

/** Refresh access JWT this many ms before `exp` so in-app clicks stay logged in. */
const ACCESS_REFRESH_SKEW_MS = 2 * 60 * 1000;

type SessionLostHandler = () => void;
const sessionLostHandlers = new Set<SessionLostHandler>();
let refreshInFlight: Promise<Tokens | null> | null = null;

/** Called when the refresh token is missing, expired, or rejected. */
export function onSessionLost(handler: SessionLostHandler) {
  sessionLostHandlers.add(handler);
  return () => {
    sessionLostHandlers.delete(handler);
  };
}

function notifySessionLost() {
  for (const handler of sessionLostHandlers) handler();
}

function accessExpiryMs(token: string | null): number | null {
  if (!token) return null;
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    const json = JSON.parse(atob(normalized)) as { exp?: number };
    return typeof json.exp === "number" ? json.exp * 1000 : null;
  } catch {
    return null;
  }
}

function accessNeedsRefresh(token: string | null): boolean {
  const exp = accessExpiryMs(token);
  if (exp == null) return true;
  return exp - Date.now() <= ACCESS_REFRESH_SKEW_MS;
}

/** Simple error with HTTP status + message from FastAPI. */
export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function parseError(res: Response): Promise<ApiError> {
  let message = `Request failed (${res.status})`;
  try {
    const body = await res.json();
    if (typeof body.detail === "string") message = body.detail;
  } catch {
    // keep default message
  }
  return new ApiError(res.status, message);
}

async function parseBody<T>(res: Response): Promise<T> {
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

/** Shared fetch wrapper. Set withAuth=true to send the JWT. */
async function request<T>(
  path: string,
  options: RequestInit = {},
  withAuth = false,
  allowRefresh = true,
): Promise<T> {
  const headers = new Headers(options.headers);

  if (options.body) {
    headers.set("Content-Type", "application/json");
  }

  if (withAuth) {
    if (allowRefresh) {
      const refreshed = await ensureAccessToken();
      if (!refreshed) {
        throw new ApiError(401, "Invalid or expired token");
      }
    }
    const token = getAccessToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
  }

  const res = await fetch(path, { ...options, headers });

  if (res.status === 401 && withAuth && allowRefresh) {
    const tokens = await refreshSession();
    if (tokens) {
      return request<T>(path, options, withAuth, false);
    }
    throw await parseError(res);
  }

  if (!res.ok) {
    throw await parseError(res);
  }

  return parseBody<T>(res);
}

// ----- Auth endpoints -----

export function signup(data: {
  tenant_name: string;
  email: string;
  password: string;
}) {
  return request<{ message: string; email: string }>("/api/v1/auth/signup", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function resendOtp(email: string) {
  return request<{ message: string }>("/api/v1/auth/resend-otp", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

/** Login OTP only — errors if the email is not registered. */
export function requestLoginOtp(email: string) {
  return request<{ message: string }>("/api/v1/auth/request-login-otp", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export async function verifyOtp(data: { email: string; otp_code: string }) {
  const tokens = await request<Tokens>("/api/v1/auth/verify-otp", {
    method: "POST",
    body: JSON.stringify(data),
  });
  saveTokens(tokens.access_token, tokens.refresh_token);
  return tokens;
}

export async function login(data: { email: string; password: string }) {
  const tokens = await request<Tokens>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify(data),
  });
  saveTokens(tokens.access_token, tokens.refresh_token);
  return tokens;
}

export async function refreshSession() {
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    const refresh = getRefreshToken();
    if (!refresh) {
      clearTokens();
      notifySessionLost();
      return null;
    }

    try {
      const tokens = await request<Tokens>(
        "/api/v1/auth/refresh",
        {
          method: "POST",
          body: JSON.stringify({ refresh_token: refresh }),
        },
        false,
        false,
      );
      saveTokens(tokens.access_token, tokens.refresh_token);
      return tokens;
    } catch {
      clearTokens();
      notifySessionLost();
      return null;
    }
  })().finally(() => {
    refreshInFlight = null;
  });

  return refreshInFlight;
}

/** Mint a new access JWT from the refresh token when missing or close to expiry. */
export async function ensureAccessToken(): Promise<boolean> {
  if (!getRefreshToken() && !getAccessToken()) return false;
  if (!accessNeedsRefresh(getAccessToken())) return true;
  const tokens = await refreshSession();
  return tokens !== null;
}

export function fetchAuthConfig() {
  return request<{ allow_public_signup: boolean }>("/api/v1/auth/config", { method: "GET" });
}

export function previewInvite(token: string) {
  return request<{ email: string; tenant_name: string }>(
    `/api/v1/auth/invites/${encodeURIComponent(token)}`,
    { method: "GET" },
  );
}

export async function acceptInvite(data: { token: string; password: string }) {
  const tokens = await request<Tokens>("/api/v1/auth/accept-invite", {
    method: "POST",
    body: JSON.stringify(data),
  });
  saveTokens(tokens.access_token, tokens.refresh_token);
  return tokens;
}

export function fetchMe() {
  return request<User>("/api/v1/auth/me", { method: "GET" }, true);
}

export function updateProfile(data: { email?: string; tenant_name?: string }) {
  return request<User>(
    "/api/v1/auth/me",
    {
      method: "PATCH",
      body: JSON.stringify(data),
    },
    true,
  );
}

export function changePassword(data: {
  current_password: string;
  new_password: string;
}) {
  return request<{ message: string }>(
    "/api/v1/auth/change-password",
    {
      method: "POST",
      body: JSON.stringify(data),
    },
    true,
  );
}

export async function logout() {
  const refresh = getRefreshToken();
  try {
    if (refresh) {
      await request(
        "/api/v1/auth/logout",
        {
          method: "POST",
          body: JSON.stringify({ refresh_token: refresh }),
        },
        true,
      );
    }
  } finally {
    clearTokens();
  }
}

// ----- Connections -----

export type DatabaseType = "postgres" | "mysql" | "mssql" | "csv" | "excel";

export type Connection = {
  id: string;
  name: string;
  db_type: DatabaseType;
  tenant_id: string;
  last_discover_status?: string | null;
  discover_scope?: SchemaDiscoverScope | null;
};

export type Dataset = {
  id: string;
  connection_id: string;
  name: string;
  display_name: string;
  source_type: string;
  status: string;
  row_count: number;
  column_count: number;
  sheet_name?: string | null;
  ingestion_error?: string | null;
};

export type DatasetUploadResult = {
  connection_id: string;
  datasets: Dataset[];
  discovery_status: string;
  semantic_counts: Record<string, number>;
};

export type ConnectionTestResult = {
  ok: boolean;
  message: string;
};

export function listConnections() {
  return request<Connection[]>("/api/v1/connections", { method: "GET" }, true);
}

export function createConnection(data: {
  name: string;
  db_type: DatabaseType;
  connection_url: string;
}) {
  return request<Connection>(
    "/api/v1/connections",
    {
      method: "POST",
      body: JSON.stringify(data),
    },
    true,
  );
}

export function updateConnection(
  connectionId: string,
  data: {
    name?: string;
    db_type?: DatabaseType;
    connection_url?: string;
  },
) {
  return request<Connection>(
    `/api/v1/connections/${connectionId}`,
    {
      method: "PATCH",
      body: JSON.stringify(data),
    },
    true,
  );
}

export function deleteConnection(connectionId: string) {
  return request<void>(
    `/api/v1/connections/${connectionId}`,
    { method: "DELETE" },
    true,
  );
}

export function testConnection(connectionId: string) {
  return request<ConnectionTestResult>(
    `/api/v1/connections/${connectionId}/test`,
    { method: "POST" },
    true,
  );
}

export function listDatasets(connectionId?: string) {
  const qs = connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : "";
  return request<{ datasets: Dataset[] }>(`/api/v1/datasets${qs}`, { method: "GET" }, true);
}

export async function uploadDataset(name: string, file: File, sheets?: string) {
  const form = new FormData();
  form.append("name", name);
  form.append("file", file);
  if (sheets) form.append("sheets", sheets);

  const token = getAccessToken();
  const res = await fetch("/api/v1/datasets/upload", {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? "Upload failed");
  }
  return (await res.json()) as DatasetUploadResult;
}

export type DocumentRecord = {
  id: string;
  tenant_id: string;
  filename: string;
  original_filename?: string | null;
  content_type?: string | null;
  status: string;
  error_message?: string | null;
  chunk_count: number;
  file_size_bytes: number;
  created_at?: string | null;
  updated_at?: string | null;
};

export async function uploadDocument(file: File): Promise<DocumentRecord> {
  const form = new FormData();
  form.append("file", file);
  const token = getAccessToken();
  const res = await fetch("/api/v1/documents/upload", {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? "Document upload failed");
  }
  return (await res.json()) as DocumentRecord;
}

export function listDocuments() {
  return request<{ documents: DocumentRecord[] }>("/api/v1/documents", { method: "GET" }, true);
}

export type SchemaTable = {
  table_name: string;
  schema_name?: string;
  columns: {
    name: string;
    data_type: string;
    nullable: boolean;
    is_pk?: boolean;
  }[];
  foreign_keys?: {
    column: string;
    referred_table: string;
    referred_column: string;
  }[];
};

export type SchemaDiscoverScope = "default" | "all" | "selected";

export type SchemaDiscoverResult = {
  connection_id: string;
  tables: SchemaTable[];
  schemas_used?: string[];
  status?: string;
  run_id?: string;
};

export type SchemaDiscoveryStatus = {
  connection_id: string;
  run_id?: string;
  status: string;
  scope?: string;
  warnings: string[];
  tables_discovered: number;
  error_message?: string;
};

export type SchemaCatalogResult = {
  connection_id: string;
  schemas: string[];
  default_schema: string;
};

/** List schemas/databases available for discovery. */
export function listConnectionSchemas(connectionId: string) {
  return request<SchemaCatalogResult>(
    `/api/v1/connections/${connectionId}/schemas`,
    { method: "GET" },
    true,
  );
}

/** Introspect tables/columns and cache them for the BI agent. */
export function discoverConnection(
  connectionId: string,
  options?: { scope?: SchemaDiscoverScope; schemas?: string[] },
) {
  const body = {
    scope: options?.scope ?? "default",
    schemas: options?.schemas ?? [],
  };
  return request<SchemaDiscoverResult>(
    `/api/v1/connections/${connectionId}/discover`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
    true,
  );
}

/** Poll the status of a background (scope=all) schema discovery run. */
export function getDiscoveryStatus(connectionId: string) {
  return request<SchemaDiscoveryStatus>(
    `/api/v1/connections/${connectionId}/discover/status`,
    { method: "GET" },
    true,
  );
}

/** Read cached schema (Redis, then platform DB). */
export function getConnectionSchema(connectionId: string) {
  return request<SchemaDiscoverResult>(
    `/api/v1/connections/${connectionId}/schema`,
    { method: "GET" },
    true,
  );
}

/** Build a provider URL from form fields (password is URL-encoded). */
export function buildConnectionUrl(input: {
  db_type: DatabaseType;
  host: string;
  port: string;
  database: string;
  username: string;
  password: string;
}): string {
  const user = encodeURIComponent(input.username);
  const pass = encodeURIComponent(input.password);
  const host = input.host.trim();
  const port = input.port.trim();
  const database = input.database.trim().replace(/^\/+/, "");

  const scheme =
    input.db_type === "postgres"
      ? "postgresql"
      : input.db_type === "mysql"
        ? "mysql"
        : "mssql";

  return `${scheme}://${user}:${pass}@${host}:${port}/${database}`;
}

export type ParsedConnectionUrl = {
  db_type: DatabaseType;
  host: string;
  port: string;
  database: string;
  username: string;
  password: string;
};

/** Parse a DSN into form fields. Throws if the URL is invalid or unsupported. */
export function parseConnectionUrl(raw: string): ParsedConnectionUrl {
  const trimmed = raw.trim();
  let db_type: DatabaseType;
  let normalized = trimmed;

  if (/^postgresql(\+[\w]+)?:\/\//i.test(trimmed) || /^postgres:\/\//i.test(trimmed)) {
    db_type = "postgres";
    normalized = trimmed.replace(/^postgres(ql)?(\+[\w]+)?:\/\//i, "http://");
  } else if (/^mysql(\+[\w]+)?:\/\//i.test(trimmed)) {
    db_type = "mysql";
    normalized = trimmed.replace(/^mysql(\+[\w]+)?:\/\//i, "http://");
  } else if (/^mssql(\+[\w]+)?:\/\//i.test(trimmed)) {
    db_type = "mssql";
    normalized = trimmed.replace(/^mssql(\+[\w]+)?:\/\//i, "http://");
  } else {
    throw new Error(
      "URL must start with postgresql://, mysql://, or mssql://",
    );
  }

  let parsed: URL;
  try {
    parsed = new URL(normalized);
  } catch {
    throw new Error("Invalid connection URL");
  }

  const defaultPort =
    db_type === "postgres" ? "5432" : db_type === "mysql" ? "3306" : "1433";

  return {
    db_type,
    host: parsed.hostname || "localhost",
    port: parsed.port || defaultPort,
    database: decodeURIComponent(parsed.pathname.replace(/^\/+/, "")),
    username: decodeURIComponent(parsed.username),
    password: decodeURIComponent(parsed.password),
  };
}

/** Prefer a pasted URL; otherwise build from form fields. */
export function resolveConnectionUrl(input: {
  mode: "url" | "fields";
  connection_url: string;
  db_type: DatabaseType;
  host: string;
  port: string;
  database: string;
  username: string;
  password: string;
}): { db_type: DatabaseType; connection_url: string } {
  if (input.mode === "url") {
    const parsed = parseConnectionUrl(input.connection_url);
    return {
      db_type: parsed.db_type,
      connection_url: buildConnectionUrl(parsed),
    };
  }

  return {
    db_type: input.db_type,
    connection_url: buildConnectionUrl(input),
  };
}

// ----- Conversations -----

export type ConversationSummary = {
  id: string;
  title: string;
  tenant_id: string;
  connection_id?: string | null;
  dashboard_id?: string | null;
  created_at?: string;
  updated_at?: string;
  archived_at?: string | null;
};

export type ConversationMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  metadata_json?: Record<string, unknown> | string;
  metadata?: {
    viz?: {
      rows?: Record<string, unknown>[];
      row_count?: number;
      chart_suggestion?: string | null;
      column_kinds?: Record<string, string>;
    };
    [key: string]: unknown;
  };
  created_at?: string;
};

export type ConversationDocument = { id: string; filename: string };

/** What the chat is pointed at (the @ picker). Sticky per conversation:
 *  "auto" lets the agent decide, "documents" answers only from files
 *  (``document_ids``, or every file in the chat when empty), "database"
 *  only from the connection. */
export type SourceScope = {
  source: "auto" | "documents" | "database";
  document_ids: string[];
};

/** Which sources an answer actually used. */
export type AnsweredFrom = {
  database: boolean;
  documents: string[];
  history?: boolean;
  /** Database connection(s) that answered; the agent may pick another one. */
  connection_ids?: string[];
};

export type ConversationDetail = ConversationSummary & {
  messages: ConversationMessage[];
  /** Files attached in this conversation; later questions can use them. */
  documents?: ConversationDocument[];
  source_scope?: SourceScope | null;
};

/** Stop a conversation using a file (the uploaded file itself is kept). */
export function removeConversationDocument(conversationId: string, documentId: string) {
  return request<ConversationDocument[]>(
    `/api/v1/conversations/${conversationId}/documents/${documentId}`,
    { method: "DELETE" },
    true,
  );
}

export function listConversations(options?: { archived?: boolean }) {
  const query = options?.archived ? "?archived=true" : "";
  return request<ConversationSummary[]>(
    `/api/v1/conversations${query}`,
    { method: "GET" },
    true,
  );
}

export function createConversation(data?: {
  title?: string;
  connection_id?: string;
}) {
  return request<ConversationSummary>(
    "/api/v1/conversations",
    {
      method: "POST",
      body: JSON.stringify(data ?? {}),
    },
    true,
  );
}

export function getConversation(conversationId: string) {
  return request<ConversationDetail>(
    `/api/v1/conversations/${conversationId}`,
    { method: "GET" },
    true,
  );
}

export function updateConversation(
  conversationId: string,
  data: { title?: string; connection_id?: string },
) {
  return request<ConversationSummary>(
    `/api/v1/conversations/${conversationId}`,
    {
      method: "PATCH",
      body: JSON.stringify(data),
    },
    true,
  );
}

export function deleteConversation(conversationId: string) {
  return request<void>(
    `/api/v1/conversations/${conversationId}`,
    { method: "DELETE" },
    true,
  );
}

export function archiveConversation(conversationId: string) {
  return request<ConversationSummary>(
    `/api/v1/conversations/${conversationId}/archive`,
    { method: "POST" },
    true,
  );
}

export function unarchiveConversation(conversationId: string) {
  return request<ConversationSummary>(
    `/api/v1/conversations/${conversationId}/unarchive`,
    { method: "POST" },
    true,
  );
}

// ----- Dashboards -----

export type DashboardSummary = {
  id: string;
  name: string;
  connection_id: string;
  conversation_id?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type DashboardFilter = {
  field: string;
  operator?: "eq" | "neq" | "gt" | "gte" | "lt" | "lte" | "in" | "like";
  value?: string | number | string[] | null;
};

export type DashboardDetail = {
  id: string;
  name: string;
  connection_id: string;
  conversation_id?: string | null;
  title?: string;
  widgets: DashboardWidget[];
  layout?: { cols?: number; rows?: number };
  filters?: DashboardFilter[];
};

export type WorkspaceSearchResult = {
  query: string;
  chats: {
    id: string;
    title: string;
    archived: boolean;
    updated_at: string | null;
    /** Matching message text; null when the title matched. */
    snippet: string | null;
  }[];
  dashboards: {
    id: string;
    name: string;
    updated_at: string | null;
    /** Matching widget title; null when the name matched. */
    snippet: string | null;
  }[];
};

/** Search the user's chat titles and messages, dashboard names and widget titles. */
export function searchWorkspace(query: string, signal?: AbortSignal) {
  return request<WorkspaceSearchResult>(
    `/api/v1/search?q=${encodeURIComponent(query)}`,
    { method: "GET", signal },
    true,
  );
}

export function listDashboards() {
  return request<DashboardSummary[]>(
    "/api/v1/dashboards",
    { method: "GET" },
    true,
  );
}

export function deleteDashboard(dashboardId: string) {
  return request<void>(
    `/api/v1/dashboards/${dashboardId}`,
    { method: "DELETE" },
    true,
  );
}

export function getDashboard(dashboardId: string) {
  return request<DashboardDetail>(
    `/api/v1/dashboards/${dashboardId}`,
    { method: "GET" },
    true,
  ).then((data) => ({
    ...data,
    title: data.title ?? data.name,
    widgets: (data.widgets ?? []).map(normalizeDashboardWidget),
  }));
}

export function refreshDashboard(dashboardId: string) {
  return request<DashboardDetail>(
    `/api/v1/dashboards/${dashboardId}/refresh`,
    { method: "POST" },
    true,
  ).then((data) => ({
    ...data,
    title: data.title ?? data.name,
    widgets: (data.widgets ?? []).map(normalizeDashboardWidget),
  }));
}

export function updateDashboardFilters(
  dashboardId: string,
  filters: DashboardFilter[],
) {
  return request<DashboardDetail>(
    `/api/v1/dashboards/${dashboardId}`,
    {
      method: "PATCH",
      body: JSON.stringify({ filters }),
    },
    true,
  ).then((data) => ({
    ...data,
    title: data.title ?? data.name,
    widgets: (data.widgets ?? []).map(normalizeDashboardWidget),
  }));
}

// ----- Semantic layer -----

export type SemanticMetric = {
  id: string;
  tenant_id: string;
  name: string;
  expression: string;
  status?: string | null;
  domain?: string | null;
};

export type SemanticDimension = {
  id: string;
  tenant_id: string;
  name: string;
  expression: string;
  status?: string | null;
  domain?: string | null;
};

export type SemanticCatalog = {
  metrics: SemanticMetric[];
  dimensions: SemanticDimension[];
};

export function fetchSemanticCatalog(connectionId: string) {
  const qs = `?connection_id=${encodeURIComponent(connectionId)}`;
  return request<SemanticCatalog>(`/api/v1/semantic${qs}`, { method: "GET" }, true);
}

export function discoverSemanticLayer(connectionId: string) {
  return request<{
    connection_id: string;
    metrics: number;
    dimensions: number;
    entities: number;
    relationships: number;
  }>(
    `/api/v1/semantic/discover/${connectionId}`,
    { method: "POST" },
    true,
  );
}

export type SemanticStatus = "suggested" | "confirmed";

export function updateMetric(
  metricId: string,
  patch: {
    name?: string;
    expression?: string;
    status?: SemanticStatus;
    /** null or "" clears a curated domain back to auto-derived. */
    domain?: string | null;
  },
) {
  return request<SemanticMetric>(
    `/api/v1/semantic/metrics/${metricId}`,
    { method: "PATCH", body: JSON.stringify(patch) },
    true,
  );
}

export function deleteMetric(metricId: string) {
  return request<void>(
    `/api/v1/semantic/metrics/${metricId}`,
    { method: "DELETE" },
    true,
  );
}

export function updateDimension(
  dimensionId: string,
  patch: {
    name?: string;
    expression?: string;
    status?: SemanticStatus;
    /** null or "" clears a curated domain back to auto-derived. */
    domain?: string | null;
  },
) {
  return request<SemanticDimension>(
    `/api/v1/semantic/dimensions/${dimensionId}`,
    { method: "PATCH", body: JSON.stringify(patch) },
    true,
  );
}

export function deleteDimension(dimensionId: string) {
  return request<void>(
    `/api/v1/semantic/dimensions/${dimensionId}`,
    { method: "DELETE" },
    true,
  );
}

// ----- Live chat (WebSocket) -----

export type ChatProgressEvent = {
  type: string;
  step?: string;
  message?: string;
  intent?: string;
  sql?: string | null;
  answer?: string;
  error?: string;
  row_count?: number;
  [key: string]: unknown;
};

export type WidgetPosition = {
  x: number;
  y: number;
  w: number;
  h: number;
};

export type DashboardWidget = {
  id: string;
  type: "kpi" | "chart" | "table" | "pie" | "ai_insight" | "text";
  title: string;
  chart?: "bar" | "line" | "pie" | null;
  rows?: Record<string, unknown>[];
  value?: unknown;
  error?: string;
  sql?: string;
  content?: string;
  insight_status?: string | null;
  config?: Record<string, unknown>;
  position?: WidgetPosition;
  value_kind?: string | null;
  aggregation?: string | null;
  metric?: string | null;
  dimension?: string | null;
  result?: Record<string, unknown> | null;
  /** Business-domain grouping for a whole-database dashboard. */
  section?: string | null;
  purpose?: string | null;
  polarity?: string | null;
  measure_labels?: Record<string, string> | null;
  unit?: string | null;
  note?: string | null;
  data_warning?: string | null;
  period_kind?: string | null;
  stale?: boolean;
};

function configString(config: Record<string, unknown>, key: string): string | undefined {
  const v = config[key];
  return typeof v === "string" && v ? v : undefined;
}

function normalizeDashboardWidget(
  raw: DashboardWidget,
): DashboardWidget {
  const config = raw.config ?? {};
  return {
    ...raw,
    content:
      raw.content ??
      (typeof config.content === "string" ? config.content : undefined),
    insight_status:
      raw.insight_status ??
      (typeof config.insight_status === "string"
        ? config.insight_status
        : undefined),
    value_kind:
      raw.value_kind ??
      (typeof config.value_kind === "string" ? config.value_kind : undefined),
    aggregation:
      raw.aggregation ??
      (typeof config.aggregation === "string" ? config.aggregation : undefined),
    result:
      raw.result ??
      (config.result && typeof config.result === "object"
        ? (config.result as DashboardWidget["result"])
        : undefined),
    // Saved dashboards keep presentation metadata in config; older API
    // responses returned only that, so fall back to it.
    section: raw.section ?? configString(config, "section"),
    metric: raw.metric ?? configString(config, "metric"),
    dimension: raw.dimension ?? configString(config, "dimension"),
    purpose: raw.purpose ?? configString(config, "purpose"),
    polarity: raw.polarity ?? configString(config, "polarity"),
    unit: raw.unit ?? configString(config, "unit"),
    note: raw.note ?? configString(config, "note"),
    data_warning: raw.data_warning ?? configString(config, "data_warning"),
    period_kind: raw.period_kind ?? configString(config, "period_kind"),
    measure_labels:
      raw.measure_labels ??
      (config.measure_labels && typeof config.measure_labels === "object"
        ? (config.measure_labels as Record<string, string>)
        : undefined),
  };
}

export type DashboardSpec = {
  title: string;
  widgets: DashboardWidget[];
};

export type ChatDoneEvent = {
  type: "done";
  answer: string;
  sql: string | null;
  rows: Record<string, unknown>[];
  steps: string[];
  error: string | null;
  intent: string | null;
  run_id: string | null;
  chart_suggestion: string | null;
  dashboard: DashboardSpec | null;
  conversation_id: string;
  rag_chunks?: unknown[];
  rag_context?: string | null;
  dataset_id?: string | null;
  dataset_display_name?: string | null;
  dataset_clarification?: Record<string, unknown> | null;
  /** Grounding metadata; column_kinds maps result columns to their measure
   *  kind (amount, count, percentage, ratio, average, ...). */
  grounding?: { column_kinds?: Record<string, string> } | null;
  answered_from?: AnsweredFrom | null;
  /** Files that joined the chat this turn (attached, @-picked, or picked by
   *  the agent in Auto mode). */
  documents_added?: ConversationDocument[];
  /** The connection that answered (may differ from the one selected). */
  connection_id?: string | null;
};

export type AskChatHandlers = {
  onProgress?: (event: ChatProgressEvent) => void;
  onReady?: (event: { user_id: string; tenant_id: string }) => void;
  signal?: AbortSignal;
};

function chatWebSocketUrl(token: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/v1/ws/chat?token=${encodeURIComponent(token)}`;
}

const PROGRESS_TYPES = new Set([
  "intent_detected",
  "sql_generated",
  "query_executed",
  "insight_ready",
  "dashboard_update",
  "error",
]);

/** Open an authenticated chat socket, ask one question, resolve on ``done``. */
export async function askViaWebSocket(
  input: {
    question: string;
    connectionId: string;
    conversationId?: string;
    documentIds?: string[];
    scope?: SourceScope;
  },
  handlers: AskChatHandlers = {},
): Promise<ChatDoneEvent> {
  const ok = await ensureAccessToken();
  if (!ok) {
    throw new ApiError(401, "Not signed in");
  }
  const token = getAccessToken();
  if (!token) {
    throw new ApiError(401, "Not signed in");
  }

  return new Promise<ChatDoneEvent>((resolve, reject) => {
    let settled = false;
    const ws = new WebSocket(chatWebSocketUrl(token));

    const fail = (err: Error) => {
      if (settled) return;
      settled = true;
      try {
        ws.close();
      } catch {
        // ignore
      }
      reject(err);
    };

    const finish = (event: ChatDoneEvent) => {
      if (settled) return;
      settled = true;
      try {
        ws.close();
      } catch {
        // ignore
      }
      resolve(event);
    };

    const onAbort = () => {
      fail(new DOMException("Aborted", "AbortError"));
    };
    handlers.signal?.addEventListener("abort", onAbort);

    ws.onerror = () => {
      fail(new ApiError(0, "Chat connection failed"));
    };

    ws.onclose = (ev) => {
      handlers.signal?.removeEventListener("abort", onAbort);
      if (settled) return;
      if (ev.code === 4401) {
        fail(new ApiError(401, "Chat authentication failed"));
        return;
      }
      fail(new ApiError(0, "Chat connection closed unexpectedly"));
    };

    ws.onmessage = (msg) => {
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(String(msg.data)) as Record<string, unknown>;
      } catch {
        fail(new ApiError(0, "Invalid chat message from server"));
        return;
      }

      const type = String(data.type || "");
      if (type === "ready") {
        handlers.onReady?.({
          user_id: String(data.user_id || ""),
          tenant_id: String(data.tenant_id || ""),
        });
        ws.send(
          JSON.stringify({
            type: "ask",
            question: input.question,
            connection_id: input.connectionId,
            conversation_id: input.conversationId ?? null,
            document_ids: input.documentIds ?? [],
            ...(input.scope ? { scope: input.scope } : {}),
          }),
        );
        return;
      }

      if (type === "done") {
        finish(data as ChatDoneEvent);
        return;
      }

      if (type === "error") {
        handlers.onProgress?.(data as ChatProgressEvent);
        // Protocol errors have no run_id; workflow errors still end with ``done``.
        if (!data.run_id) {
          fail(new ApiError(400, String(data.error || "Chat error")));
        }
        return;
      }

      if (PROGRESS_TYPES.has(type)) {
        handlers.onProgress?.(data as ChatProgressEvent);
      }
    };
  });
}


// ----- Memory (what the assistant remembers about the user) -----

export type MemoryItem = {
  id: string;
  /** "fact": a note about the user; "conversation": one chat's summary. */
  kind: "fact" | "conversation";
  content: string;
  conversation_id?: string | null;
  conversation_title?: string | null;
  updated_at?: string | null;
};

export type MemoryOverview = { enabled: boolean; items: MemoryItem[] };

export function getMemories() {
  return request<MemoryOverview>("/api/v1/memories", { method: "GET" }, true);
}

export function setMemoryEnabled(enabled: boolean) {
  return request<MemoryOverview>(
    "/api/v1/memories/settings",
    { method: "PUT", body: JSON.stringify({ enabled }) },
    true,
  );
}

export function deleteMemory(memoryId: string) {
  return request<void>(`/api/v1/memories/${memoryId}`, { method: "DELETE" }, true);
}

export function clearMemories() {
  return request<void>("/api/v1/memories", { method: "DELETE" }, true);
}

// ----- Data dictionary (per-connection table/column descriptions) -----

export type DataDictionaryOrigin = "import" | "ai" | "manual";

export type DataDictionaryRow = {
  schema_name: string;
  table_name: string;
  /** "" = description of the table itself. */
  column_name: string;
  description: string;
  origin: DataDictionaryOrigin;
};

export type DataDictionaryEntry = DataDictionaryRow & {
  id: string;
  connection_id: string;
};

export type DataDictionaryPreview = {
  filename: string;
  connection_id: string;
  entries: (DataDictionaryRow & {
    action: "create" | "update" | "unchanged";
    existing_description?: string | null;
  })[];
  skipped: { row: number | null; reason: string }[];
};

export function listDataDictionary(connectionId: string) {
  const qs = `?connection_id=${encodeURIComponent(connectionId)}`;
  return request<DataDictionaryEntry[]>(
    `/api/v1/semantic/data-dictionary${qs}`,
    { method: "GET" },
    true,
  );
}

export function updateDataDictionaryEntry(entryId: string, description: string) {
  return request<DataDictionaryEntry>(
    `/api/v1/semantic/data-dictionary/${entryId}`,
    { method: "PATCH", body: JSON.stringify({ description }) },
    true,
  );
}

export function deleteDataDictionaryEntry(entryId: string) {
  return request<void>(
    `/api/v1/semantic/data-dictionary/${entryId}`,
    { method: "DELETE" },
    true,
  );
}

// --- Semantic model import (KPI + relationship sheets) ---------------------------------

export type SemanticModelKpi = {
  name: string;
  formula: string;
  description: string;
  domain: string;
  synonyms: string[];
};

export type SemanticModelKpiPreview = SemanticModelKpi & {
  action: "create" | "update" | "link" | "unchanged" | "conflict" | "invalid";
  origin: "import" | "ai";
  expression: string;
  error: string | null;
  warnings: string[];
  existing_name: string | null;
  existing_expression: string | null;
  row: number | null;
};

export type SemanticModelRelationship = {
  from_schema: string;
  from_table: string;
  from_column: string;
  to_schema: string;
  to_table: string;
  to_column: string;
  cardinality: string;
  description: string;
};

export type SemanticModelRelationshipPreview = SemanticModelRelationship & {
  action: "create" | "confirm" | "unchanged" | "invalid";
  origin: "import" | "inferred";
  join: string;
  error: string | null;
  warnings: string[];
  note: string | null;
  row: number | null;
};

export type SemanticRuleKind = "rule" | "setting" | "vocabulary";

export type SemanticModelRule = {
  kind: SemanticRuleKind;
  name: string;
  category: string;
  rule: string;
  guidance: string;
  terms: string[];
  origin: "import" | "ai";
};

export type SemanticModelRulePreview = SemanticModelRule & {
  action: "create" | "update" | "unchanged";
  warnings: string[];
  row: number | null;
};

export type SemanticRule = SemanticModelRule & { id: string; connection_id: string };

export type SemanticModelExample = {
  question: string;
  tables: string[];
  domain: string;
  notes: string;
  origin: "import" | "ai";
};

export type SemanticModelExamplePreview = SemanticModelExample & {
  action: "create" | "unchanged";
  warnings: string[];
  row: number | null;
};

export type SemanticExample = SemanticModelExample & { id: string; connection_id: string };

export type SemanticModelPreview = {
  filename: string;
  connection_id: string;
  kpis: SemanticModelKpiPreview[];
  relationships: SemanticModelRelationshipPreview[];
  rules: SemanticModelRulePreview[];
  dictionary: DataDictionaryPreview["entries"];
  examples: SemanticModelExamplePreview[];
  skipped: { row: number | null; reason: string }[];
  notes: string[];
};

export type SemanticModelSavePayload = {
  kpis: SemanticModelKpi[];
  relationships: SemanticModelRelationship[];
  rules: SemanticModelRule[];
  dictionary: DataDictionaryRow[];
  examples: SemanticModelExample[];
  overwrite_dictionary: boolean;
};

export type SemanticModelSaveResult = {
  kpis: Record<string, number>;
  relationships: Record<string, number>;
  rules: Record<string, number>;
  dictionary: Record<string, number>;
  examples: Record<string, number>;
};

export type ExampleEvaluation = {
  total: number;
  passed: number;
  results: {
    question: string;
    status: "pass" | "wrong_tables" | "fallback" | "error" | "no_answer";
    expected_tables: string[];
    tables_used: string[];
    missing_tables: string[];
    row_count: number | null;
    error: string | null;
  }[];
};

/**
 * Preview a semantic-model import; nothing is saved. With a file, every sheet
 * is read and (with generateMissing) AI drafts what the file lacks; with no
 * file, AI drafts the whole model from the connection's schema.
 */
export async function previewSemanticModelImport(
  connectionId: string,
  file: File | null,
  generateMissing: boolean,
): Promise<SemanticModelPreview> {
  if (!(await ensureAccessToken())) {
    throw new ApiError(401, "Invalid or expired token");
  }
  const form = new FormData();
  if (file) form.append("file", file);
  const token = getAccessToken();
  const qs = `?connection_id=${encodeURIComponent(connectionId)}&generate_missing=${generateMissing}`;
  const res = await fetch(`/api/v1/semantic/model-import/preview${qs}`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  });
  if (!res.ok) {
    throw await parseError(res);
  }
  return (await res.json()) as SemanticModelPreview;
}

export function saveSemanticModelImport(connectionId: string, payload: SemanticModelSavePayload) {
  return request<SemanticModelSaveResult>(
    "/api/v1/semantic/model-import",
    {
      method: "POST",
      body: JSON.stringify({ connection_id: connectionId, ...payload }),
    },
    true,
  );
}

export function listSemanticExamples(connectionId: string) {
  return request<SemanticExample[]>(
    `/api/v1/semantic/examples?connection_id=${encodeURIComponent(connectionId)}`,
    { method: "GET" },
    true,
  );
}

export function deleteSemanticExample(exampleId: string) {
  return request<void>(`/api/v1/semantic/examples/${exampleId}`, { method: "DELETE" }, true);
}

/** Ask the assistant each saved example question and check the answers. */
export function evaluateSemanticExamples(connectionId: string) {
  return request<ExampleEvaluation>(
    `/api/v1/semantic/examples/evaluate?connection_id=${encodeURIComponent(connectionId)}`,
    { method: "POST" },
    true,
  );
}

export function listSemanticRules(connectionId: string) {
  return request<SemanticRule[]>(
    `/api/v1/semantic/rules?connection_id=${encodeURIComponent(connectionId)}`,
    { method: "GET" },
    true,
  );
}

export function deleteSemanticRule(ruleId: string) {
  return request<void>(`/api/v1/semantic/rules/${ruleId}`, { method: "DELETE" }, true);
}

// ---------------------------------------------------------------- AI usage

export type UsageRange = "1h" | "3h" | "12h" | "1d" | "1w" | "30d" | "custom";
export type UsageScope = "me" | "organization";

export type UsageTotals = {
  tokens: number;
  input_tokens: number;
  output_tokens: number;
  requests: number;
};

export type UsagePoint = UsageTotals & { t: string };

export type FeatureUsage = {
  feature: string;
  label: string;
  tokens: number;
  requests: number;
  series: UsagePoint[];
};

export type UsageReport = {
  scope: UsageScope;
  range: UsageRange;
  start: string;
  end: string;
  bucket_seconds: number;
  totals: UsageTotals;
  all_time: UsageTotals;
  series: UsagePoint[];
  features: FeatureUsage[];
  users: { user_id: string; email: string; tokens: number; requests: number }[] | null;
};

/** Token usage over a time range (no cost). Admins default to the whole organization. */
export function getAiUsage(
  params: { range: UsageRange; start?: string; end?: string; scope?: UsageScope },
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ range: params.range });
  if (params.start) query.set("start", params.start);
  if (params.end) query.set("end", params.end);
  if (params.scope) query.set("scope", params.scope);
  return request<UsageReport>(`/api/v1/usage?${query}`, { method: "GET", signal }, true);
}


// ----- Organization admin (members, departments, data access) -----

export type OrgRole = "admin" | "analyst" | "viewer";

export type InviteResult = {
  email_sent: boolean;
  /** Present only when the email couldn't be sent — share it by hand. */
  invite_url?: string | null;
};

export type OrgUser = {
  id: string;
  email: string;
  role: OrgRole;
  status: "active" | "invited";
  department_ids: string[];
  created_at?: string | null;
};

export type DepartmentGrant = {
  connection_id: string;
  /** null = the whole connection. */
  schema_name: string | null;
};

export type Department = {
  id: string;
  name: string;
  description?: string | null;
  member_ids: string[];
  grants: DepartmentGrant[];
};

export function listOrgUsers() {
  return request<OrgUser[]>("/api/v1/org/users", { method: "GET" }, true);
}

export function inviteOrgUser(data: { email: string; role: OrgRole; department_ids: string[] }) {
  return request<{ user: OrgUser; invite: InviteResult }>(
    "/api/v1/org/users/invite",
    { method: "POST", body: JSON.stringify(data) },
    true,
  );
}

export function updateOrgUser(userId: string, data: { role?: OrgRole; department_ids?: string[] }) {
  return request<OrgUser>(
    `/api/v1/org/users/${userId}`,
    { method: "PATCH", body: JSON.stringify(data) },
    true,
  );
}

export function resendOrgInvite(userId: string) {
  return request<InviteResult>(`/api/v1/org/users/${userId}/resend-invite`, { method: "POST" }, true);
}

export function removeOrgUser(userId: string) {
  return request<void>(`/api/v1/org/users/${userId}`, { method: "DELETE" }, true);
}

export function listDepartments() {
  return request<Department[]>("/api/v1/org/departments", { method: "GET" }, true);
}

export function createDepartment(data: { name: string; description?: string | null }) {
  return request<Department>(
    "/api/v1/org/departments",
    { method: "POST", body: JSON.stringify(data) },
    true,
  );
}

export function updateDepartment(
  departmentId: string,
  data: { name?: string; description?: string | null },
) {
  return request<Department>(
    `/api/v1/org/departments/${departmentId}`,
    { method: "PATCH", body: JSON.stringify(data) },
    true,
  );
}

export function deleteDepartment(departmentId: string) {
  return request<void>(`/api/v1/org/departments/${departmentId}`, { method: "DELETE" }, true);
}

export function setDepartmentMembers(departmentId: string, userIds: string[]) {
  return request<Department>(
    `/api/v1/org/departments/${departmentId}/members`,
    { method: "PUT", body: JSON.stringify({ user_ids: userIds }) },
    true,
  );
}

export function setDepartmentGrants(departmentId: string, grants: DepartmentGrant[]) {
  return request<Department>(
    `/api/v1/org/departments/${departmentId}/grants`,
    { method: "PUT", body: JSON.stringify({ grants }) },
    true,
  );
}

// ----- Platform (super admin) -----

export type PlatformOrg = {
  id: string;
  name: string;
  created_at?: string | null;
  disabled: boolean;
  user_count: number;
  admin_emails: string[];
};

export function listPlatformOrgs() {
  return request<PlatformOrg[]>("/api/v1/platform/orgs", { method: "GET" }, true);
}

export function createPlatformOrg(data: { name: string; admin_email: string }) {
  return request<{ org: PlatformOrg; invite: InviteResult }>(
    "/api/v1/platform/orgs",
    { method: "POST", body: JSON.stringify(data) },
    true,
  );
}

export function updatePlatformOrg(orgId: string, data: { name?: string; disabled?: boolean }) {
  return request<PlatformOrg>(
    `/api/v1/platform/orgs/${orgId}`,
    { method: "PATCH", body: JSON.stringify(data) },
    true,
  );
}

export function invitePlatformOrgAdmin(orgId: string, email: string) {
  return request<InviteResult>(
    `/api/v1/platform/orgs/${orgId}/admins`,
    { method: "POST", body: JSON.stringify({ email }) },
    true,
  );
}
