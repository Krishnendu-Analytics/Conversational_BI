import { Link, useNavigate, useOutletContext, useParams } from "react-router-dom";
import { useCallback, useEffect, useRef, useState, startTransition } from "react";
import {
  AskComposer,
  type MentionOption,
  type ScopeChip,
} from "@/components/AskComposer";
import { ChatMarkdown } from "@/components/ChatMarkdown";
import { DashboardGrid, type DashboardSpec } from "@/components/DashboardGrid";
import { shouldRenderDashboardInVizPane } from "@/lib/dashboardLayout";
import { InsightDashboard } from "@/components/InsightDashboard";
import {
  ApiError,
  askViaWebSocket,
  createConversation,
  getConversation,
  getDashboard,
  listConnections,
  listDatasets,
  listDocuments,
  uploadDocument,
  removeConversationDocument,
  type AnsweredFrom,
  type ConversationDocument,
  type SourceScope,
  type Connection,
  type Dataset,
  type DocumentRecord,
  type ConversationDetail,
} from "@/lib/api";

type MessageRole = "user" | "assistant";

type Message = {
  id: string;
  role: MessageRole;
  content: string;
  attachments?: string[];
  steps?: string[];
  pending?: boolean;
  answeredFrom?: AnsweredFrom | null;
  /** Connection that answered, for the "Answered from" tag. */
  connectionId?: string | null;
};

const AUTO_SCOPE: SourceScope = { source: "auto", document_ids: [] };

type OutletContext = {
  refreshWorkspace?: () => Promise<void>;
};

const SUGGESTED_QUESTIONS = [
  "Create a full insight dashboard",
  "What were total sales last quarter?",
  "Which customers placed the most orders?",
];

const STEP_LABELS: Record<string, string> = {
  classify_intent: "Understanding your question",
  generate_sql: "Generating SQL",
  execute_query: "Running query",
  describe_schema: "Reading schema",
  retrieve_docs: "Searching documents",
  build_dashboard: "Building dashboard",
  edit_dashboard: "Updating dashboard",
  recommend_chart: "Choosing a chart",
  generate_insight: "Preparing insight",
  supervisor: "Planning which agents to use",
  answer_history: "Reading your past chats",
  intent_detected: "Understanding your question",
  sql_generated: "Generating SQL",
  query_executed: "Running query",
  insight_ready: "Preparing insight",
  dashboard_update: "Updating dashboard",
};

function labelForProgress(event: {
  type?: string;
  step?: string;
  message?: string;
}) {
  if (event.message) return event.message;
  const key = event.step || event.type || "";
  return STEP_LABELS[key] || key || "Working…";
}

// crypto.randomUUID only exists in secure contexts (https or localhost), so
// plain-http deployments need a fallback or every send silently fails.
function createId() {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : Date.now().toString(36) + Math.random().toString(36).slice(2);
}

type SavedViz = {
  rows: Record<string, unknown>[];
  chartSuggestion: string | null;
  columnKinds: Record<string, string>;
  question: string;
};

/** Result rows saved with the latest answer that had any, so a reopened
 *  chat redraws its chart panel instead of showing it empty. */
function latestSavedViz(detail: ConversationDetail): SavedViz | null {
  for (let i = detail.messages.length - 1; i >= 0; i--) {
    const msg = detail.messages[i];
    const viz = msg.role === "assistant" ? msg.metadata?.viz : undefined;
    if (!viz || !Array.isArray(viz.rows) || viz.rows.length === 0) continue;
    const asked = detail.messages
      .slice(0, i)
      .reverse()
      .find((m) => m.role === "user");
    return {
      rows: viz.rows,
      chartSuggestion: viz.chart_suggestion ?? null,
      columnKinds: viz.column_kinds ?? {},
      question: asked?.content ?? "",
    };
  }
  return null;
}

function messagesFromConversation(detail: ConversationDetail): Message[] {
  return detail.messages.map((msg) => ({
    id: msg.id,
    role: msg.role,
    content: msg.content,
    answeredFrom: (msg.metadata?.answered_from as AnsweredFrom | undefined) ?? null,
    connectionId: detail.connection_id,
  }));
}


type ParsedFraudTable = {
  headers: string[];
  rows: Record<string, string>[];
  disclaimer: string | null;
};

/** Parse only the Markdown table emitted by the fraud-prediction route. */
function parseFraudPredictionTable(content: string): ParsedFraudTable | null {
  if (!/fraud prediction results/i.test(content) || !content.includes('|')) return null;

  const lines = content.split(/\r?\n/).map((line) => line.trim());
  const tableStart = lines.findIndex((line) => line.startsWith('|') && line.endsWith('|'));
  if (tableStart < 0) return null;

  const splitCells = (line: string) =>
    line.slice(1, -1).split('|').map((cell) => cell.trim().replace(/\\\|/g, '|'));
  const headers = splitCells(lines[tableStart]);
  if (!headers.length) return null;

  let rowIndex = tableStart + 1;
  if (rowIndex < lines.length && /^\|?\s*:?-{3,}/.test(lines[rowIndex])) rowIndex += 1;

  const rows: Record<string, string>[] = [];
  for (; rowIndex < lines.length; rowIndex += 1) {
    const line = lines[rowIndex];
    if (!line.startsWith('|') || !line.endsWith('|')) break;
    const cells = splitCells(line);
    if (cells.length !== headers.length) continue;
    rows.push(Object.fromEntries(headers.map((header, index) => [header, cells[index]])));
  }

  if (!rows.length) return null;
  const disclaimerMatch = content.match(/\*This is a model-generated risk assessment,[\s\S]*?fraudulent\.\*/i);
  return { headers, rows, disclaimer: disclaimerMatch?.[0].replace(/^\*|\*$/g, '') ?? null };
}

function FraudPredictionAnswer({ content }: { content: string }) {
  const parsed = parseFraudPredictionTable(content);
  if (!parsed) return <ChatMarkdown content={content} />;

  return (
    <div className="fraud-prediction-answer">
      <h3>Fraud prediction results</h3>
      <div style={{ maxWidth: "100%", overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", margin: "8px 0 12px" }}>
          <thead>
            <tr>
              {parsed.headers.map((header) => (
                <th key={header} style={{ textAlign: "left", padding: "8px", borderBottom: "1px solid var(--border, #d1d5db)" }}>
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {parsed.rows.map((row, index) => (
              <tr key={`${row[parsed.headers[0]] ?? "claim"}-${index}`}>
                {parsed.headers.map((header) => (
                  <td key={header} style={{ padding: "8px", borderBottom: "1px solid var(--border, #e5e7eb)" }}>
                    {row[header]}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {parsed.disclaimer ? <p><em>{parsed.disclaimer}</em></p> : null}
    </div>
  );
}

/** "Answered from Sales DB · policy.pdf" under an answer. */
function AnsweredFromTag({
  from,
  connectionName,
  nameOf,
}: {
  from: AnsweredFrom;
  connectionName?: string;
  nameOf: (connectionId: string) => string | undefined;
}) {
  const databases = from.connection_ids?.length
    ? from.connection_ids.map((id) => nameOf(id) || "Database")
    : from.database
      ? [connectionName || "Database"]
      : [];
  const parts = [
    ...(from.history ? [{ kind: "history", label: "Your chat history" }] : []),
    ...databases.map((label) => ({ kind: "database", label })),
    ...from.documents.map((name) => ({ kind: "document", label: name })),
  ];
  if (parts.length === 0) return null;
  return (
    <p className="answered-from">
      <span className="answered-from-label">Answered from</span>
      {parts.map((part) => (
        <span key={`${part.kind}:${part.label}`} className={`answered-from-source is-${part.kind}`}>
          {part.label}
        </span>
      ))}
    </p>
  );
}

/** Primary analyst surface: conversation + live visualization. */
export function AskPage() {
  const { conversationId: routeConversationId } = useParams<{
    conversationId?: string;
  }>();
  const navigate = useNavigate();
  const { refreshWorkspace } = useOutletContext<OutletContext>();

  const [loading, setLoading] = useState(true);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [selectedConnectionId, setSelectedConnectionId] = useState<string>("");
  const [activeDatasetName, setActiveDatasetName] = useState<string | null>(null);
  const [connectionDatasets, setConnectionDatasets] = useState<Dataset[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [linkedDashboardId, setLinkedDashboardId] = useState<string | null>(null);
  const [linkedDashboardSpec, setLinkedDashboardSpec] = useState<DashboardSpec | null>(
    null,
  );
  const [mainTab, setMainTab] = useState<"chat" | "dashboard">("chat");
  const [activeInsight, setActiveInsight] = useState<{
    title: string;
    queryLabel: string;
    rows: Record<string, unknown>[];
    chartSuggestion: string | null;
    columnKinds: Record<string, string>;
    error: string | null;
  } | null>(null);
  const [activeDashboard, setActiveDashboard] = useState<{
    spec: DashboardSpec;
    queryLabel: string;
    error: string | null;
  } | null>(null);
  const [mobilePane, setMobilePane] = useState<"chat" | "viz">("chat");
  const threadRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef(0);
  const conversationIdRef = useRef<string | null>(routeConversationId ?? null);
  const inFlightAskRef = useRef(false);

  const hasConnections = connections.length > 0;
  const showDashboardTab =
    linkedDashboardId !== null || linkedDashboardSpec !== null;

  const clearVisualization = useCallback(() => {
    setActiveInsight(null);
    setActiveDashboard(null);
    setMobilePane("chat");
  }, []);

  const refreshConnections = useCallback(async () => {
    const rows = await listConnections();
    setConnections(rows);
    if (rows.length > 0) {
      setSelectedConnectionId((prev) =>
        prev && rows.some((r) => r.id === prev) ? prev : rows[0].id,
      );
    }
  }, []);

  // Files attached anywhere in this chat — searched on every later question.
  const [conversationDocs, setConversationDocs] = useState<ConversationDocument[]>([]);
  const [removingDocId, setRemovingDocId] = useState<string | null>(null);
  // Where the chat is pointed (@ picker). Sticky: stays until the user
  // changes it, and is saved with the conversation.
  const [scope, setScope] = useState<SourceScope>(AUTO_SCOPE);
  // Earlier uploads (not yet in this chat) the user @-mentioned; they join
  // the chat with the next question.
  const [libraryDocs, setLibraryDocs] = useState<ConversationDocument[]>([]);
  const [pendingDocs, setPendingDocs] = useState<ConversationDocument[]>([]);

  const refreshLibraryDocs = useCallback(async () => {
    try {
      const { documents } = await listDocuments();
      setLibraryDocs(
        documents
          .filter((d) => d.status === "ready")
          .map((d) => ({ id: d.id, filename: d.original_filename || d.filename })),
      );
    } catch {
      setLibraryDocs([]);
    }
  }, []);

  useEffect(() => {
    void refreshLibraryDocs();
  }, [refreshLibraryDocs]);

  const knownDocs = [...conversationDocs, ...pendingDocs];
  const docName = (id: string) =>
    knownDocs.find((d) => d.id === id)?.filename ??
    libraryDocs.find((d) => d.id === id)?.filename ??
    "Document";
  const connectionName = (id: string) => connections.find((c) => c.id === id)?.name;

  const mentionOptions: MentionOption[] = [
    { kind: "auto", id: "auto", label: "Auto", detail: "Let the analyst choose" },
    ...connections.map((conn) => ({
      kind: "database" as const,
      id: conn.id,
      label: conn.name,
      detail: conn.db_type,
    })),
    ...knownDocs.map((doc) => ({
      kind: "document" as const,
      id: doc.id,
      label: doc.filename || "Document",
      detail: "in this chat",
    })),
    ...libraryDocs
      .filter((doc) => !knownDocs.some((k) => k.id === doc.id))
      .map((doc) => ({
        kind: "document" as const,
        id: doc.id,
        label: doc.filename || "Document",
        detail: "uploaded earlier",
      })),
  ];

  const scopeChips: ScopeChip[] =
    scope.source === "database"
      ? [
          {
            kind: "database",
            id: selectedConnectionId,
            label: connectionName(selectedConnectionId) ?? "Database",
          },
        ]
      : scope.source === "documents"
        ? scope.document_ids.length > 0
          ? scope.document_ids.map((id) => ({ kind: "document" as const, id, label: docName(id) }))
          : [{ kind: "document", id: "all", label: "All files in this chat" }]
        : [];

  function handleMention(option: MentionOption) {
    if (option.kind === "auto") {
      setScope(AUTO_SCOPE);
      return;
    }
    if (option.kind === "database") {
      setSelectedConnectionId(option.id);
      setScope({ source: "database", document_ids: [] });
      return;
    }
    // Files add up: "@policy.pdf @rates.xlsx" asks both.
    setScope((prev) => ({
      source: "documents",
      document_ids:
        prev.source === "documents"
          ? [...prev.document_ids.filter((id) => id !== option.id), option.id]
          : [option.id],
    }));
    if (!conversationDocs.some((d) => d.id === option.id)) {
      setPendingDocs((prev) =>
        prev.some((d) => d.id === option.id)
          ? prev
          : [...prev, { id: option.id, filename: option.label }],
      );
    }
  }

  function handleRemoveScopeChip(chip: ScopeChip) {
    if (chip.kind === "database" || chip.id === "all") {
      setScope(AUTO_SCOPE);
      return;
    }
    setPendingDocs((prev) => prev.filter((d) => d.id !== chip.id));
    setScope((prev) => {
      const rest = prev.document_ids.filter((id) => id !== chip.id);
      return rest.length > 0 ? { ...prev, document_ids: rest } : AUTO_SCOPE;
    });
  }

  async function handleRemoveDocument(doc: ConversationDocument) {
    const convId = conversationIdRef.current;
    if (!convId) return;
    setRemovingDocId(doc.id);
    try {
      setConversationDocs(await removeConversationDocument(convId, doc.id));
      setScope((prev) => {
        if (!prev.document_ids.includes(doc.id)) return prev;
        const rest = prev.document_ids.filter((id) => id !== doc.id);
        return rest.length > 0 || prev.source !== "documents"
          ? { ...prev, document_ids: rest }
          : AUTO_SCOPE;
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not remove the file from this chat");
    } finally {
      setRemovingDocId(null);
    }
  }

  const loadConversation = useCallback(
    async (id: string) => {
      const detail = await getConversation(id);
      if (
        inFlightAskRef.current &&
        conversationIdRef.current === detail.id
      ) {
        return;
      }
      conversationIdRef.current = detail.id;
      setMessages(messagesFromConversation(detail));
      setConversationDocs(detail.documents ?? []);
      setScope(detail.source_scope ?? AUTO_SCOPE);
      setPendingDocs([]);
      const saved = latestSavedViz(detail);
      if (saved) {
        setActiveInsight({
          title: "Query result",
          queryLabel: saved.question,
          rows: saved.rows,
          chartSuggestion: saved.chartSuggestion,
          columnKinds: saved.columnKinds,
          error: null,
        });
      }
      setLinkedDashboardId(detail.dashboard_id ?? null);
      if (detail.connection_id) {
        setSelectedConnectionId(detail.connection_id);
      }
      if (detail.dashboard_id) {
        try {
          const dash = await getDashboard(detail.dashboard_id);
          setLinkedDashboardSpec({
            title: dash.title ?? dash.name,
            widgets: dash.widgets,
            layout: dash.layout,
            filters: dash.filters,
          });
        } catch {
          setLinkedDashboardSpec(null);
        }
      } else {
        setLinkedDashboardSpec(null);
      }
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await refreshConnections();
        if (cancelled) return;

        if (routeConversationId) {
          const skipReload =
            inFlightAskRef.current &&
            conversationIdRef.current === routeConversationId;
          if (!skipReload) {
            // Switching to a different chat: the visualization panel belongs
            // to the previous conversation, so don't carry it over.
            if (conversationIdRef.current !== routeConversationId) {
              clearVisualization();
            }
            await loadConversation(routeConversationId);
          }
          return;
        }

        // Stale /ask effect must not wipe bubbles after we created an id.
        if (inFlightAskRef.current) return;

        conversationIdRef.current = null;
        setMessages([]);
        setConversationDocs([]);
        setScope(AUTO_SCOPE);
        setPendingDocs([]);
        setLinkedDashboardId(null);
        setLinkedDashboardSpec(null);
        clearVisualization();
        setMainTab("chat");
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof ApiError ? err.message : "Could not load workspace data",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [routeConversationId, refreshConnections, loadConversation, clearVisualization]);

  useEffect(() => {
    const conn = connections.find((c) => c.id === selectedConnectionId);
    if (!conn || (conn.db_type !== "csv" && conn.db_type !== "excel")) {
      setConnectionDatasets([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const { datasets } = await listDatasets(conn.id);
        if (!cancelled) setConnectionDatasets(datasets);
      } catch {
        if (!cancelled) setConnectionDatasets([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [connections, selectedConnectionId]);

  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  async function ensureConversationId(): Promise<string> {
    if (conversationIdRef.current) return conversationIdRef.current;

    const created = await createConversation({
      connection_id: selectedConnectionId || undefined,
    });
    conversationIdRef.current = created.id;
    navigate(`/app/ask/${created.id}`, { replace: true });
    await refreshWorkspace?.();
    return created.id;
  }

  async function handleAsk(question: string, files: File[] = []) {
    setError(null);
    const runId = ++abortRef.current;

    const connectionId = selectedConnectionId;
    if (!connectionId) {
      setError("Add a database connection before asking.");
      return;
    }

    inFlightAskRef.current = true;

    const userMessage: Message = {
      id: createId(),
      role: "user",
      content: question,
      attachments: files.map((f) => f.name),
    };

    const pendingId = createId();
    const pendingMessage: Message = {
      id: pendingId,
      role: "assistant",
      content: "",
      steps: files.length
        ? ["Uploading documents", "Understanding your question"]
        : ["Understanding your question"],
      pending: true,
    };

    startTransition(() => {
      setMessages((prev) => [...prev, userMessage, pendingMessage]);
      setBusy(true);
      setMobilePane("chat");
      setMainTab("chat");
    });

    try {
      const uploadedDocs: DocumentRecord[] = [];
      for (const file of files) {
        if (abortRef.current !== runId) return;
        const doc = await uploadDocument(file);
        uploadedDocs.push(doc);
        if (doc.status === "failed") {
          throw new ApiError(
            400,
            doc.error_message || `Document ${doc.filename} failed to process`,
          );
        }
      }

      const convId = await ensureConversationId();

      // Attaching files points the chat at them: the next questions are about
      // those files until the user picks another source with @.
      const askScope: SourceScope =
        uploadedDocs.length > 0
          ? {
              source: "documents",
              document_ids: [
                ...(scope.source === "documents" ? scope.document_ids : []),
                ...uploadedDocs.map((d) => d.id),
              ],
            }
          : scope;
      const joining = pendingDocs.filter((d) => !conversationDocs.some((c) => c.id === d.id));
      setScope(askScope);

      const done = await askViaWebSocket(
        {
          question,
          connectionId,
          conversationId: convId,
          documentIds: [...uploadedDocs.map((d) => d.id), ...joining.map((d) => d.id)],
          scope: askScope,
        },
        {
          onProgress: (event) => {
            if (abortRef.current !== runId) return;
            const label = labelForProgress(event);
            setMessages((prev) =>
              prev.map((msg) => {
                if (msg.id !== pendingId) return msg;
                const steps = [...(msg.steps || [])];
                if (!steps.includes(label)) steps.push(label);
                return { ...msg, steps };
              }),
            );
            if (
              event.type === "dashboard_update" &&
              event.dashboard &&
              typeof event.dashboard === "object" &&
              Array.isArray((event.dashboard as DashboardSpec).widgets)
            ) {
              const spec = event.dashboard as DashboardSpec;
              setActiveDashboard({
                spec,
                queryLabel: question,
                error: null,
              });
              setActiveInsight(null);
              setLinkedDashboardSpec(spec);
              setMainTab("dashboard");
              setMobilePane("viz");
            }
          },
        },
      );

      if (abortRef.current !== runId) return;

      if (done.dataset_display_name) {
        setActiveDatasetName(done.dataset_display_name);
      }
      // The agent answered from another database: follow it, so follow-ups
      // and the connection picker stay on the database being discussed.
      const answeredOn = done.connection_id || connectionId;
      if (answeredOn !== connectionId && connections.some((c) => c.id === answeredOn)) {
        setSelectedConnectionId(answeredOn);
      }

      if (done.conversation_id && done.conversation_id !== conversationIdRef.current) {
        conversationIdRef.current = done.conversation_id;
        navigate(`/app/ask/${done.conversation_id}`, { replace: true });
      }
      await refreshWorkspace?.();

      const answerText =
        done.answer && done.answer.trim()
          ? done.answer
          : done.error != null && done.error !== ""
            ? `I hit a problem: ${done.error}`
            : "No answer returned.";

      const insightTitle = done.intent === "fraud_prediction"
        ? "Fraud prediction results"
        : files.length
          ? "Document analysis"
          : done.intent === "greeting"
          ? "Greeting"
          : done.intent === "schema"
            ? "Schema"
            : done.intent === "rag"
              ? "Documents"
              : done.intent === "dashboard" || done.intent === "edit_dashboard"
                ? "Dashboard"
                : "Query result";

      if (
        done.dashboard &&
        Array.isArray(done.dashboard.widgets) &&
        done.dashboard.widgets.length > 0
      ) {
        setActiveDashboard({
          spec: done.dashboard,
          queryLabel: question,
          error:
            done.error != null && done.error !== "" ? String(done.error) : null,
        });
        setActiveInsight(null);
        setLinkedDashboardSpec(done.dashboard);
        setMainTab("dashboard");
      } else {
        setActiveDashboard(null);
        setActiveInsight({
          title: insightTitle,
          queryLabel: question,
          rows: Array.isArray(done.rows) && done.rows.length > 0
            ? (done.intent === "fraud_prediction"
                ? done.rows.map((row: Record<string, unknown>) =>
                    Object.fromEntries(
                      Object.entries(row).filter(([key]) =>
                        !["mapped_feature_count", "expected_feature_count", "features_mapped", "features mapped"].includes(key.trim().toLowerCase())
                      )
                    )
                  )
                : done.rows)
            : done.intent === "fraud_prediction"
              ? (parseFraudPredictionTable(answerText)?.rows ?? [])
              : [],
          chartSuggestion: done.chart_suggestion ?? null,
          columnKinds: done.grounding?.column_kinds ?? {},
          error:
            done.error != null && done.error !== "" ? String(done.error) : null,
        });
      }
      setMobilePane("viz");
      const added = [
        ...uploadedDocs.map((d) => ({ id: d.id, filename: d.filename })),
        ...joining,
        ...(done.documents_added ?? []),
      ];
      if (added.length > 0) {
        setConversationDocs((prev) => [
          ...prev,
          ...added.filter((d) => !prev.some((p) => p.id === d.id)),
        ]);
        setPendingDocs([]);
      }
      if (uploadedDocs.length > 0) void refreshLibraryDocs();

      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === pendingId
            ? {
                ...msg,
                pending: false,
                content: answerText,
                answeredFrom: done.answered_from ?? null,
                connectionId: answeredOn,
                steps: done.steps?.length
                  ? done.steps.map((s) => STEP_LABELS[s] || s)
                  : msg.steps,
              }
            : msg,
        ),
      );
    } catch (err) {
      if (abortRef.current !== runId) return;
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Chat failed";
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === pendingId
            ? {
                ...msg,
                pending: false,
                content: `Could not get an answer: ${message}`,
              }
            : msg,
        ),
      );
      setError(message);
    } finally {
      if (abortRef.current === runId) {
        inFlightAskRef.current = false;
        setBusy(false);
      }
    }
  }

  const showViz = activeInsight !== null || activeDashboard !== null;

  return (
    <div
      className={`analyst${
        mainTab === "dashboard" && linkedDashboardSpec ? " analyst-dashboard-full" : ""
      }`}
    >
      <div className="analyst-mobile-tabs" role="tablist" aria-label="Workspace panes">
        <button
          type="button"
          role="tab"
          aria-selected={mobilePane === "chat"}
          className={mobilePane === "chat" ? "is-active" : undefined}
          onClick={() => setMobilePane("chat")}
        >
          Chat
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mobilePane === "viz"}
          className={mobilePane === "viz" ? "is-active" : undefined}
          onClick={() => setMobilePane("viz")}
        >
          Charts
        </button>
      </div>

      <section
        className={`analyst-chat${mobilePane === "chat" ? " is-mobile-visible" : ""}`}
        aria-label="Conversation"
      >
        <header className="analyst-chat-bar">
          <div>
            <p className="analyst-chat-kicker">Analyst</p>
            <h1 className="analyst-chat-title">Ask</h1>
          </div>
          <div className="analyst-chat-actions">
            {hasConnections && !loading ? (
              <label className="connection-picker">
                <span className="visually-hidden">Database connection</span>
                <select
                  value={selectedConnectionId}
                  onChange={(e) => setSelectedConnectionId(e.target.value)}
                  disabled={busy}
                  aria-label="Database connection"
                >
                  {connections.map((conn) => (
                    <option key={conn.id} value={conn.id}>
                      {conn.name} ({conn.db_type})
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            {activeDatasetName ? (
              <span className="connection-meta" title="Active file dataset">
                Dataset: {activeDatasetName}
              </span>
            ) : connectionDatasets.length === 1 ? (
              <span className="connection-meta" title="File dataset on this connection">
                Dataset: {connectionDatasets[0].display_name} ({connectionDatasets[0].status})
              </span>
            ) : connectionDatasets.length > 1 ? (
              <span className="connection-meta" title="Multiple datasets — the agent may ask which to use">
                {connectionDatasets.length} datasets
              </span>
            ) : null}
          </div>
        </header>

        {showDashboardTab ? (
          <div className="chat-dashboard-tabs" role="tablist" aria-label="Main view">
            <button
              type="button"
              role="tab"
              aria-selected={mainTab === "chat"}
              className={mainTab === "chat" ? "is-active" : undefined}
              onClick={() => setMainTab("chat")}
            >
              Chat
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mainTab === "dashboard"}
              className={mainTab === "dashboard" ? "is-active" : undefined}
              onClick={() => setMainTab("dashboard")}
            >
              Dashboard
            </button>
          </div>
        ) : null}

        {mainTab === "dashboard" && linkedDashboardSpec ? (
          <div className="analyst-dashboard-pane">
            <DashboardGrid dashboard={linkedDashboardSpec} />
          </div>
        ) : (
          <>
            <div className="analyst-thread" ref={threadRef} aria-live="polite">
              {loading ? (
                <div className="analyst-status">
                  <p className="loading-copy">Loading workspace…</p>
                </div>
              ) : error && messages.length === 0 ? (
                <div className="analyst-status">
                  <p className="form-error" role="alert">
                    {error}
                  </p>
                </div>
              ) : !hasConnections ? (
                <div className="analyst-empty">
                  <p className="analyst-brand">Conversational BI</p>
                  <h2>Connect a data source</h2>
                  <p>
                    Add at least one database connection so the analyst has somewhere
                    to query.
                  </p>
                  <Link to="/app/connections" className="btn btn-primary">
                    Open connections
                  </Link>
                </div>
              ) : messages.length === 0 ? (
                <div className="analyst-empty">
                  <p className="analyst-brand">Conversational BI</p>
                  <h2>What do you want to know?</h2>
                  <p>
                    Ask in plain English. Attach documents when you need context
                    beyond the database.
                  </p>
                  <div className="ask-suggestions">
                    {SUGGESTED_QUESTIONS.map((question) => (
                      <button
                        key={question}
                        type="button"
                        className="suggestion-chip"
                        onClick={() => void handleAsk(question)}
                        disabled={busy}
                      >
                        {question}
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="analyst-messages">
                  {messages.map((message) => (
                    <article
                      key={message.id}
                      className={`chat-msg chat-msg-${message.role}`}
                    >
                      {message.role === "assistant" ? (
                        <div className="chat-msg-avatar" aria-hidden>
                          A
                        </div>
                      ) : null}
                      <div className="chat-msg-body">
                        {message.attachments && message.attachments.length > 0 ? (
                          <ul className="chat-msg-files">
                            {message.attachments.map((name) => (
                              <li key={name}>{name}</li>
                            ))}
                          </ul>
                        ) : null}
                        {message.pending && message.steps ? (
                          <div className="chat-reasoning">
                            <ol>
                              {message.steps.map((step, idx) => (
                                <li
                                  key={`${step}-${idx}`}
                                  className={
                                    idx === message.steps!.length - 1
                                      ? "is-current"
                                      : "is-done"
                                  }
                                >
                                  {step}
                                </li>
                              ))}
                            </ol>
                          </div>
                        ) : null}
                        {message.content ? (
                          message.role === "assistant" ? (
                            <FraudPredictionAnswer content={message.content} />
                          ) : (
                            <p className="chat-msg-text">{message.content}</p>
                          )
                        ) : null}
                        {!message.pending && message.answeredFrom ? (
                          <AnsweredFromTag
                            nameOf={connectionName}
                            from={message.answeredFrom}
                            connectionName={
                              message.connectionId ? connectionName(message.connectionId) : undefined
                            }
                          />
                        ) : null}
                        {!message.pending &&
                        message.role === "assistant" &&
                        message.content &&
                        showViz ? (
                          <button
                            type="button"
                            className="chat-open-viz"
                            onClick={() => setMobilePane("viz")}
                          >
                            View charts →
                          </button>
                        ) : null}
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </div>

            {hasConnections && !loading ? (
              <div className="analyst-composer-wrap">
                {conversationDocs.length > 0 ? (
                  <div className="chat-docs" aria-label="Files in this chat">
                    <span className="chat-docs-label">Files in this chat</span>
                    {conversationDocs.map((doc) => (
                      <span key={doc.id} className="chat-doc-chip" title={doc.filename}>
                        <span className="chat-doc-name">{doc.filename || "Document"}</span>
                        <button
                          type="button"
                          className="chat-doc-remove"
                          onClick={() => void handleRemoveDocument(doc)}
                          disabled={busy || removingDocId === doc.id}
                          aria-label={`Stop using ${doc.filename} in this chat`}
                          title="Stop using this file in this chat"
                        >
                          ×
                        </button>
                      </span>
                    ))}
                  </div>
                ) : null}
                <AskComposer
                  onSubmit={(q, files) => void handleAsk(q, files)}
                  busy={busy}
                  placeholder="Ask about your data — type @ to pick a database or file…"
                  mentionOptions={mentionOptions}
                  onMention={handleMention}
                  scopeChips={scopeChips}
                  onRemoveScopeChip={handleRemoveScopeChip}
                />
              </div>
            ) : null}
          </>
        )}
      </section>

      <aside
        className={`analyst-viz${showViz ? " is-active" : ""}${
          mobilePane === "viz" ? " is-mobile-visible" : ""
        }`}
        aria-label="Visualization"
      >
        {showViz && activeDashboard && shouldRenderDashboardInVizPane(mainTab) ? (
          <DashboardGrid
            dashboard={activeDashboard.spec}
            queryLabel={activeDashboard.queryLabel}
            error={activeDashboard.error}
          />
        ) : showViz && activeInsight && shouldRenderDashboardInVizPane(mainTab) ? (
          <InsightDashboard
            title={activeInsight.title}
            queryLabel={activeInsight.queryLabel}
            rows={activeInsight.rows}
            chartSuggestion={activeInsight.chartSuggestion}
            columnKinds={activeInsight.columnKinds}
            error={activeInsight.error}
          />
        ) : (
          <div className="viz-idle">
            <div className="viz-idle-frame" aria-hidden>
              <span />
              <span />
              <span />
            </div>
            <h2>Charts appear here</h2>
            <p>
              Ask a question or attach a document. Metrics and charts render in
              this panel as the analyst responds.
            </p>
          </div>
        )}
      </aside>
    </div>
  );
}
