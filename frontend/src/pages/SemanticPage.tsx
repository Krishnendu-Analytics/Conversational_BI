import { useEffect, useState } from "react";
import {
  ApiError,
  listConnections,
  type Connection,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { SemanticModelImportPanel } from "@/components/SemanticModelImportPanel";

/** Semantic model imports for a connection. */
export function SemanticPage() {
  const { user } = useAuth();
  const [connections, setConnections] = useState<Connection[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows = await listConnections();
        if (!cancelled) {
          setConnections(rows);
          const firstId = rows[0]?.id ?? "";
          if (firstId) setConnectionId(firstId);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load semantic layer");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (user?.role === "viewer") {
    return (
      <div className="profile-page">
        <p className="form-hint">Viewers cannot manage the semantic layer.</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="overlay overlay-center">
        <p className="loading-copy">Loading semantic layer…</p>
      </div>
    );
  }

  return (
    <div className="profile-page semantic-page">
      <header className="profile-header">
        <div>
          <h1>Semantic layer</h1>
          <p className="form-hint">
            Everything below applies to the selected connection's semantic model.
          </p>
        </div>
      </header>

      <section className="profile-card">
        <div className="semantic-toolbar">
          <label className="field semantic-connection-field">
            <span>Connection</span>
            <select
              value={connectionId}
              onChange={(e) => setConnectionId(e.target.value)}
              disabled={connections.length === 0}
            >
              {connections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        {error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
      </section>

      {connectionId ? (
        <SemanticModelImportPanel
          connectionId={connectionId}
          canEdit={user?.role === "admin"}
        />
      ) : null}
    </div>
  );
}
