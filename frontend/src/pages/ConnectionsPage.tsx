import { useState } from "react";
import { Link } from "react-router-dom";
import {
  ConnectionsPanel,
  type ConnectionsView,
} from "@/components/ConnectionsPanel";
import type { Connection } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/** Authenticated connections management surface. */
export function ConnectionsPage() {
  const [view, setView] = useState<ConnectionsView>("list");
  const [editing, setEditing] = useState<Connection | null>(null);

  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const isList = view === "list";

  return (
    <div className="connections-page">
      <header className="connections-page-header">
        <div>
          <p className="page-kicker">Workspace</p>
          <h1 className="page-title">
            {view === "create"
              ? "Add connection"
              : view === "edit"
                ? "Edit connection"
                : "Connections"}
          </h1>
          <p className="page-lede">
            {isAdmin
              ? "Secure database sources the analyst can query. Test before you ask."
              : "The data sources your departments give you access to."}
          </p>
        </div>
        {!isAdmin ? null : isList ? (
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              setView("create");
              setEditing(null);
            }}
          >
            Add connection
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              setView("list");
              setEditing(null);
            }}
          >
            Back to list
          </button>
        )}
      </header>

      <div className="connections-page-grid">
        <section className="connections-page-main">
          <ConnectionsPanel
            view={view}
            editing={editing}
            hideListCta
            onViewChange={(next, conn = null) => {
              setView(next);
              setEditing(conn);
            }}
          />
        </section>

        <aside className="connections-side">
          <h2>How it works</h2>
          <ol className="connections-steps">
            <li>
              <strong>Connect</strong>
              <span>Add Postgres, MySQL, or SQL Server. CREATE, UPDATE, and DELETE queries are blocked by the SQL gate.</span>
            </li>
            <li>
              <strong>Verify</strong>
              <span>Run a connection test so the analyst only trusts live sources.</span>
            </li>
            <li>
              <strong>Discover</strong>
              <span>
                Click Discover schema so Ask can generate SQL from your tables
                and columns.
              </span>
            </li>
            <li>
              <strong>Ask</strong>
              <span>
                Return to{" "}
                <Link to="/app/ask">Ask</Link> and query in plain English.
              </span>
            </li>
          </ol>
        </aside>
      </div>
    </div>
  );
}
