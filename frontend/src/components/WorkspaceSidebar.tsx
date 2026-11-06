import type {
  ConversationSummary,
  DashboardSummary,
  WorkspaceSearchResult,
} from "@/lib/api";

type Props = {
  conversations: ConversationSummary[];
  archivedConversations: ConversationSummary[];
  dashboards: DashboardSummary[];
  activeConversationId?: string | null;
  activeDashboardId?: string | null;
  showArchived: boolean;
  archiveBusyId?: string | null;
  /** Filters chats and dashboards by title (case-insensitive). */
  query?: string;
  /** Server matches for ``query`` (titles and content); null until they arrive. */
  searchResults?: WorkspaceSearchResult | null;
  searching?: boolean;
  onSelectConversation: (id: string) => void;
  onSelectDashboard: (id: string) => void;
  onToggleArchived: () => void;
  onArchiveConversation: (id: string) => void;
  onUnarchiveConversation: (id: string) => void;
  onDeleteConversation: (id: string) => void;
  onDeleteDashboard: (id: string) => void;
};

function ArchiveIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="3" y="4" width="18" height="4" rx="1" stroke="currentColor" strokeWidth="1.75" />
      <path d="M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8" stroke="currentColor" strokeWidth="1.75" />
      <path d="M10 13h4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  );
}

function UnarchiveIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="3" y="4" width="18" height="4" rx="1" stroke="currentColor" strokeWidth="1.75" />
      <path d="M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8" stroke="currentColor" strokeWidth="1.75" />
      <path d="M12 17v-5m0 0-2 2m2-2 2 2" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function DeleteIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m-9 0 1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"
        stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** ``text`` with the first case-insensitive match of ``query`` in <mark>. */
function Highlight({ text, query }: { text: string; query: string }) {
  const at = query ? text.toLowerCase().indexOf(query.toLowerCase()) : -1;
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <mark>{text.slice(at, at + query.length)}</mark>
      {text.slice(at + query.length)}
    </>
  );
}

/** Chats and dashboards: the scrolling middle of the app sidebar. */
export function WorkspaceSidebar({
  conversations,
  archivedConversations,
  dashboards,
  activeConversationId,
  activeDashboardId,
  showArchived,
  archiveBusyId,
  query = "",
  searchResults = null,
  searching = false,
  onSelectConversation,
  onSelectDashboard,
  onToggleArchived,
  onArchiveConversation,
  onUnarchiveConversation,
  onDeleteConversation,
  onDeleteDashboard,
}: Props) {
  const needle = query.trim().toLowerCase();
  const matches = (title: string) => !needle || title.toLowerCase().includes(needle);
  const visibleChats = (showArchived ? archivedConversations : conversations).filter((c) =>
    matches(c.title),
  );
  const visibleDashboards = dashboards.filter((d) => matches(d.name));

  if (needle && searchResults) {
    return (
      <div className="workspace-sidebar" aria-label="Search results" role="region">
        <section className="workspace-sidebar-section">
          <h2 className="workspace-sidebar-heading">Chats</h2>
          <ul className="workspace-sidebar-list">
            {searchResults.chats.length === 0 ? (
              <li className="workspace-sidebar-empty">No chats match "{query.trim()}"</li>
            ) : (
              searchResults.chats.map((chat) => (
                <li key={chat.id}>
                  <button
                    type="button"
                    className={
                      chat.id === activeConversationId
                        ? "workspace-sidebar-item workspace-search-hit is-active"
                        : "workspace-sidebar-item workspace-search-hit"
                    }
                    onClick={() => onSelectConversation(chat.id)}
                    title={chat.title}
                  >
                    <span className="workspace-sidebar-item-label">
                      {chat.title}
                      {chat.archived ? <span className="workspace-search-tag">Archived</span> : null}
                    </span>
                    {chat.snippet ? (
                      <span className="workspace-search-snippet">
                        <Highlight text={chat.snippet} query={query.trim()} />
                      </span>
                    ) : null}
                  </button>
                </li>
              ))
            )}
          </ul>
        </section>
        <section className="workspace-sidebar-section">
          <h2 className="workspace-sidebar-heading">Dashboards</h2>
          <ul className="workspace-sidebar-list">
            {searchResults.dashboards.length === 0 ? (
              <li className="workspace-sidebar-empty">No dashboards match "{query.trim()}"</li>
            ) : (
              searchResults.dashboards.map((dash) => (
                <li key={dash.id}>
                  <button
                    type="button"
                    className={
                      dash.id === activeDashboardId
                        ? "workspace-sidebar-item workspace-search-hit is-active"
                        : "workspace-sidebar-item workspace-search-hit"
                    }
                    onClick={() => onSelectDashboard(dash.id)}
                    title={dash.name}
                  >
                    <span className="workspace-sidebar-item-label">{dash.name}</span>
                    {dash.snippet ? (
                      <span className="workspace-search-snippet">
                        Widget: <Highlight text={dash.snippet} query={query.trim()} />
                      </span>
                    ) : null}
                  </button>
                </li>
              ))
            )}
          </ul>
        </section>
      </div>
    );
  }

  return (
    <div className="workspace-sidebar" aria-label="Chats and dashboards" role="region">

      <section className="workspace-sidebar-section">
        <div className="workspace-sidebar-heading-row">
          <h2 className="workspace-sidebar-heading">
            {showArchived ? "Archived chats" : "Chats"}
          </h2>
          <button
            type="button"
            className="workspace-sidebar-toggle"
            onClick={onToggleArchived}
          >
            {showArchived ? "Back to chats" : "Archived"}
          </button>
        </div>
        <ul className="workspace-sidebar-list">
          {visibleChats.length === 0 ? (
            <li className="workspace-sidebar-empty">
              {needle
                ? searching
                  ? "Searching messages…"
                  : `No chats match "${query.trim()}"`
                : showArchived
                  ? "No archived chats"
                  : "No chats yet"}
            </li>
          ) : (
            visibleChats.map((chat) => (
              <li key={chat.id} className="workspace-sidebar-row">
                <button
                  type="button"
                  className={
                    chat.id === activeConversationId
                      ? "workspace-sidebar-item is-active"
                      : "workspace-sidebar-item"
                  }
                  onClick={() => onSelectConversation(chat.id)}
                  title={chat.title}
                >
                  <span className="workspace-sidebar-item-label">{chat.title}</span>
                </button>
                <span className="workspace-sidebar-row-actions">
                  {showArchived ? (
                    <button
                      type="button"
                      className="workspace-sidebar-icon-btn"
                      title="Unarchive"
                      aria-label={`Unarchive ${chat.title}`}
                      disabled={archiveBusyId === chat.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        onUnarchiveConversation(chat.id);
                      }}
                    >
                      <UnarchiveIcon />
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="workspace-sidebar-icon-btn"
                      title="Archive"
                      aria-label={`Archive ${chat.title}`}
                      disabled={archiveBusyId === chat.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        onArchiveConversation(chat.id);
                      }}
                    >
                      <ArchiveIcon />
                    </button>
                  )}
                  <button
                    type="button"
                    className="workspace-sidebar-icon-btn workspace-sidebar-icon-btn-danger"
                    title="Delete"
                    aria-label={`Delete ${chat.title}`}
                    disabled={archiveBusyId === chat.id}
                    onClick={(e) => {
                      e.stopPropagation();
                      onDeleteConversation(chat.id);
                    }}
                  >
                    <DeleteIcon />
                  </button>
                </span>
              </li>
            ))
          )}
        </ul>
      </section>

      <section className="workspace-sidebar-section">
        <h2 className="workspace-sidebar-heading">Dashboards</h2>
        <ul className="workspace-sidebar-list">
          {visibleDashboards.length === 0 ? (
            <li className="workspace-sidebar-empty">
              {needle ? `No dashboards match "${query.trim()}"` : "No dashboards yet"}
            </li>
          ) : (
            visibleDashboards.map((dash) => (
              <li key={dash.id} className="workspace-sidebar-row">
                <button
                  type="button"
                  className={
                    dash.id === activeDashboardId
                      ? "workspace-sidebar-item is-active"
                      : "workspace-sidebar-item"
                  }
                  onClick={() => onSelectDashboard(dash.id)}
                  title={dash.name}
                >
                  <span className="workspace-sidebar-item-label">{dash.name}</span>
                </button>
                <span className="workspace-sidebar-row-actions">
                  <button
                    type="button"
                    className="workspace-sidebar-icon-btn workspace-sidebar-icon-btn-danger"
                    title="Delete"
                    aria-label={`Delete ${dash.name}`}
                    disabled={archiveBusyId === dash.id}
                    onClick={(e) => {
                      e.stopPropagation();
                      onDeleteDashboard(dash.id);
                    }}
                  >
                    <DeleteIcon />
                  </button>
                </span>
              </li>
            ))
          )}
        </ul>
      </section>
    </div>
  );
}
