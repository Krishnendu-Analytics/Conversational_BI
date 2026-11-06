import { NavLink, Outlet, useLocation, useNavigate, useParams } from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { WorkspaceSidebar } from "@/components/WorkspaceSidebar";
import { Badge } from "@/components/Badge";
import { ThemeToggle } from "@/components/ThemeToggle";
import { useAuth } from "@/lib/auth";
import {
  ApiError,
  archiveConversation,
  deleteConversation,
  deleteDashboard,
  listConversations,
  listDashboards,
  searchWorkspace,
  unarchiveConversation,
  type ConversationSummary,
  type DashboardSummary,
  type WorkspaceSearchResult,
} from "@/lib/api";

const NAV = [
  {
    to: "/app/ask",
    label: "Ask",
    end: false,
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
        <path
          d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4v8Z"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
  {
    to: "/app/connections",
    label: "Connections",
    end: false,
    adminOnly: false,
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
        <path
          d="M4 7h6M14 7h6M7 4v6M17 14v6M4 17h6M14 17h6"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
        />
        <rect x="8" y="8" width="8" height="8" rx="2" stroke="currentColor" strokeWidth="1.75" />
      </svg>
    ),
  },
  {
    to: "/app/semantic",
    label: "Semantic",
    end: false,
    viewerHidden: true,
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
        <path
          d="M4 6h16M4 12h10M4 18h16"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
        />
      </svg>
    ),
  },
  {
    to: "/app/usage",
    label: "AI usage",
    end: false,
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
        <path
          d="M4 4v16h16"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path
          d="m7 15 4-5 3 3 5-6"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
  {
    to: "/app/organization",
    label: "Organization",
    end: false,
    adminOnly: true,
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
        <circle cx="9" cy="8" r="3.25" stroke="currentColor" strokeWidth="1.75" />
        <path
          d="M3 19c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
        />
        <path
          d="M16 5.2a3 3 0 0 1 0 5.6M18 13.8c1.9.7 3 2.5 3 5.2"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
        />
      </svg>
    ),
  },
  {
    to: "/app/platform",
    label: "Platform",
    end: false,
    superAdminOnly: true,
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
        <rect x="4" y="3.5" width="16" height="17" rx="2" stroke="currentColor" strokeWidth="1.75" />
        <path
          d="M8 8h2M14 8h2M8 12h2M14 12h2M10 20.5v-4h4v4"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
        />
      </svg>
    ),
  },
] as const;

const COLLAPSED_KEY = "sidebar-collapsed";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function SidebarToggleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="3" y="4" width="18" height="16" rx="2.5" stroke="currentColor" strokeWidth="1.75" />
      <path d="M9 4v16" stroke="currentColor" strokeWidth="1.75" />
    </svg>
  );
}

/**
 * Authenticated product chrome: one sidebar (navigation, chats and
 * dashboards, account) + main stage. The sidebar collapses to an icon strip
 * so the stage gets the full width; on phones the chat list is a drawer.
 */
export function AppLayout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const params = useParams();
  const [busy, setBusy] = useState(false);
  const [confirmLogout, setConfirmLogout] = useState(false);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [archivedConversations, setArchivedConversations] = useState<ConversationSummary[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [archiveBusyId, setArchiveBusyId] = useState<string | null>(null);
  const [dashboards, setDashboards] = useState<DashboardSummary[]>([]);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [query, setQuery] = useState("");
  const searchInput = useRef<HTMLInputElement>(null);
  const [searchResults, setSearchResults] = useState<WorkspaceSearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const activeConversationId = location.pathname.startsWith("/app/ask")
    ? params.conversationId ?? null
    : null;
  const activeDashboardId = location.pathname.startsWith("/app/dashboards")
    ? params.dashboardId ?? null
    : null;

  const refreshWorkspace = useCallback(async () => {
    // Fetch independently — a failure in one list must never block the
    // other from updating (Promise.all would silently drop both).
    try {
      setConversations(await listConversations());
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setConversations([]);
      } else {
        console.error("Could not load chats", err);
      }
    }
    try {
      setDashboards(await listDashboards());
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setDashboards([]);
      } else {
        console.error("Could not load dashboards", err);
      }
    }
  }, []);

  const refreshArchived = useCallback(async () => {
    try {
      setArchivedConversations(await listConversations({ archived: true }));
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setArchivedConversations([]);
      } else {
        console.error("Could not load archived chats", err);
      }
    }
  }, []);

  useEffect(() => {
    void refreshWorkspace();
  }, [refreshWorkspace, location.pathname]);

  useEffect(() => {
    if (!showArchived) return;
    void refreshArchived();
  }, [showArchived, refreshArchived]);

  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  // Server search (titles + message text), debounced. Until it answers,
  // the sidebar filters titles locally, so typing always feels instant.
  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setSearchResults(null);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    setSearching(true);
    const timer = window.setTimeout(() => {
      searchWorkspace(q, controller.signal)
        .then((result) => setSearchResults(result))
        .catch((err) => {
          if (controller.signal.aborted) return;
          console.error("Search failed", err);
          setSearchResults(null);
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  // Cmd/Ctrl+K jumps to search (expanding a collapsed sidebar first).
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCollapsed(false);
        setDrawerOpen(true);
        window.setTimeout(() => searchInput.current?.focus(), 0);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  function toggleCollapsed() {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // Private mode / blocked storage: the toggle still works this session.
      }
      return next;
    });
  }

  function handleToggleArchived() {
    setShowArchived((prev) => !prev);
  }

  async function handleArchiveConversation(id: string) {
    setArchiveBusyId(id);
    try {
      await archiveConversation(id);
      await refreshWorkspace();
      if (id === activeConversationId) navigate("/app/ask");
    } catch (err) {
      console.error("Could not archive chat", err);
    } finally {
      setArchiveBusyId(null);
    }
  }

  async function handleUnarchiveConversation(id: string) {
    setArchiveBusyId(id);
    try {
      await unarchiveConversation(id);
      await Promise.all([refreshWorkspace(), refreshArchived()]);
    } catch (err) {
      console.error("Could not unarchive chat", err);
    } finally {
      setArchiveBusyId(null);
    }
  }

  async function handleDeleteConversation(id: string) {
    if (!window.confirm("Delete this chat permanently? This can't be undone.")) return;
    setArchiveBusyId(id);
    try {
      await deleteConversation(id);
      await Promise.all([refreshWorkspace(), refreshArchived()]);
      if (id === activeConversationId) navigate("/app/ask");
    } catch (err) {
      console.error("Could not delete chat", err);
    } finally {
      setArchiveBusyId(null);
    }
  }

  async function handleDeleteDashboard(id: string) {
    if (!window.confirm("Delete this dashboard permanently? This can't be undone.")) return;
    setArchiveBusyId(id);
    try {
      await deleteDashboard(id);
      await refreshWorkspace();
      if (id === activeDashboardId) navigate("/app/ask");
    } catch (err) {
      console.error("Could not delete dashboard", err);
    } finally {
      setArchiveBusyId(null);
    }
  }

  useEffect(() => {
    if (!confirmLogout) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && !busy) setConfirmLogout(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [confirmLogout, busy]);

  async function handleLogout() {
    setBusy(true);
    try {
      await logout();
      setConfirmLogout(false);
      navigate("/", { replace: true });
    } finally {
      setBusy(false);
    }
  }

  function handleNewChat() {
    navigate("/app/ask");
  }

  function handleSelectConversation(id: string) {
    navigate(`/app/ask/${id}`);
  }

  function handleSelectDashboard(id: string) {
    navigate(`/app/dashboards/${id}`);
  }

  const initials =
    user?.email?.slice(0, 2).toUpperCase() ?? "ME";

  return (
    <div
      className={[
        "app-layout",
        collapsed ? "is-collapsed" : "",
        drawerOpen ? "is-drawer-open" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <aside className="app-rail" aria-label="Workspace">
        <div className="app-rail-brand">
          <span className="app-rail-mark" aria-hidden title="Conversational BI">
            C
          </span>
          <span className="app-rail-wordmark">Conversational BI</span>
          <button
            type="button"
            className="app-rail-collapse"
            onClick={toggleCollapsed}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
          >
            <SidebarToggleIcon />
          </button>
        </div>

        {user?.tenant_name ? (
          <div className="app-rail-workspace">
            <span className="app-rail-workspace-name" title={user.tenant_name}>
              {user.tenant_name}
            </span>
            <Badge tone="neutral" size="sm">
              {user.role}
            </Badge>
          </div>
        ) : null}

        <button
          type="button"
          className="app-rail-new"
          onClick={handleNewChat}
          title="New chat"
          aria-label="New chat"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
          <span className="app-rail-link-label">New chat</span>
        </button>

        <label className="sidebar-search">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
            <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="1.75" />
            <path d="m20 20-3.5-3.5" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
          </svg>
          <input
            ref={searchInput}
            type="search"
            className="sidebar-search-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setQuery("");
                e.currentTarget.blur();
              }
            }}
            placeholder="Search chats & dashboards"
            aria-label="Search chats and dashboards"
          />
          {query ? null : (
            <span className="sidebar-search-kbd" aria-hidden>
              {navigator.platform.toLowerCase().includes("mac") ? "⌘K" : "Ctrl K"}
            </span>
          )}
        </label>

        <nav className="app-rail-nav">
          {NAV.filter((item) => {
            if ("adminOnly" in item && item.adminOnly && user?.role !== "admin") return false;
            if ("superAdminOnly" in item && item.superAdminOnly && !user?.is_super_admin) {
              return false;
            }
            return (
              user?.role !== "viewer" ||
              (item.to !== "/app/connections" && item.to !== "/app/semantic")
            );
          }).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                isActive ? "app-rail-link is-active" : "app-rail-link"
              }
              title={item.label}
              aria-label={item.label}
            >
              {item.icon}
              <span className="app-rail-link-label">{item.label}</span>
            </NavLink>
          ))}
          <button
            type="button"
            className="app-rail-link app-rail-drawer-btn"
            onClick={() => setDrawerOpen((open) => !open)}
            aria-expanded={drawerOpen}
            title="Chats and dashboards"
            aria-label="Chats and dashboards"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
            </svg>
            <span className="app-rail-link-label">History</span>
          </button>
        </nav>

        <WorkspaceSidebar
          conversations={conversations}
          archivedConversations={archivedConversations}
          dashboards={dashboards}
          activeConversationId={activeConversationId}
          activeDashboardId={activeDashboardId}
          showArchived={showArchived}
          archiveBusyId={archiveBusyId}
          query={query}
          searchResults={searchResults}
          searching={searching}
          onSelectConversation={handleSelectConversation}
          onSelectDashboard={handleSelectDashboard}
          onToggleArchived={handleToggleArchived}
          onArchiveConversation={(id) => void handleArchiveConversation(id)}
          onUnarchiveConversation={(id) => void handleUnarchiveConversation(id)}
          onDeleteConversation={(id) => void handleDeleteConversation(id)}
          onDeleteDashboard={(id) => void handleDeleteDashboard(id)}
        />

        <div className="app-rail-footer">
          <NavLink
            to="/app/profile"
            className={({ isActive }) =>
              isActive
                ? "app-rail-user app-rail-user-link is-active"
                : "app-rail-user app-rail-user-link"
            }
            title={user?.email ?? "Profile"}
            aria-label="Open profile"
          >
            <span className="app-rail-avatar" aria-hidden>
              {initials}
            </span>
            <span className="app-rail-link-label">Profile</span>
          </NavLink>
          <button
            type="button"
            className="app-rail-link"
            onClick={() => setConfirmLogout(true)}
            disabled={busy}
            title="Sign out"
            aria-label="Sign out"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path
                d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"
                stroke="currentColor"
                strokeWidth="1.75"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            <span className="app-rail-link-label">Sign out</span>
          </button>
          <ThemeToggle />
        </div>
      </aside>

      <main className="app-stage">
        <Outlet context={{ refreshWorkspace }} />
      </main>

      {confirmLogout ? (
        <div
          className="modal-backdrop"
          role="presentation"
          onClick={() => {
            if (!busy) setConfirmLogout(false);
          }}
        >
          <div
            className="modal-panel logout-confirm-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="logout-confirm-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="logout-confirm-title">Sign out?</h2>
            <p className="form-hint">
              You’ll need to sign in again to access this workspace.
            </p>
            <div className="form-actions">
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => setConfirmLogout(false)}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void handleLogout()}
                disabled={busy}
              >
                {busy ? "Signing out…" : "Sign out"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
