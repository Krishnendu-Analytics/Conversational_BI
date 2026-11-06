import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { Badge } from "@/components/Badge";
import {
  ApiError,
  createPlatformOrg,
  invitePlatformOrgAdmin,
  listPlatformOrgs,
  updatePlatformOrg,
  type InviteResult,
  type PlatformOrg,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

function errorText(err: unknown, fallback: string) {
  return err instanceof ApiError ? err.message : fallback;
}

function LinkNotice({ invite, email, onDismiss }: { invite: InviteResult; email: string; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false);
  if (invite.email_sent) {
    return (
      <p className="form-success" role="status">
        Invite emailed to {email}.
      </p>
    );
  }
  const url = invite.invite_url ?? "";
  return (
    <div className="org-invite-link" role="status">
      <p>
        <strong>The invite email to {email} couldn't be sent.</strong> Share this one-time
        link instead:
      </p>
      <div className="org-invite-link-row">
        <input readOnly value={url} onFocus={(e) => e.currentTarget.select()} aria-label="Invite link" />
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => void navigator.clipboard?.writeText(url).then(() => setCopied(true))}
        >
          {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onDismiss}>
          Done
        </button>
      </div>
    </div>
  );
}

/** Super admin: create organizations and invite their admins. */
export function PlatformPage() {
  const { user } = useAuth();
  const [orgs, setOrgs] = useState<PlatformOrg[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [adminEmail, setAdminEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [lastInvite, setLastInvite] = useState<{ invite: InviteResult; email: string } | null>(null);
  const [rowBusy, setRowBusy] = useState<string | null>(null);
  const [addAdminFor, setAddAdminFor] = useState<string | null>(null);
  const [addAdminEmail, setAddAdminEmail] = useState("");

  const reload = useCallback(async () => {
    try {
      setOrgs(await listPlatformOrgs());
      setError(null);
    } catch (err) {
      setError(errorText(err, "Could not load organizations"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  if (user && !user.is_super_admin) return <Navigate to="/app/ask" replace />;

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const email = adminEmail.trim();
      const result = await createPlatformOrg({ name: name.trim(), admin_email: email });
      setLastInvite({ invite: result.invite, email });
      setName("");
      setAdminEmail("");
      await reload();
    } catch (err) {
      setError(errorText(err, "Could not create the organization"));
    } finally {
      setBusy(false);
    }
  }

  async function rowAction(orgId: string, action: () => Promise<unknown>, fallback: string) {
    setRowBusy(orgId);
    setError(null);
    try {
      await action();
      await reload();
    } catch (err) {
      setError(errorText(err, fallback));
    } finally {
      setRowBusy(null);
    }
  }

  return (
    <div className="org-page">
      <header className="org-page-header">
        <div>
          <p className="page-kicker">Platform</p>
          <h1 className="page-title">Organizations</h1>
          <p className="page-lede">
            Create an organization and invite its first admin. They take it from there:
            inviting people, creating departments and granting data access.
          </p>
        </div>
      </header>

      <div className="org-grid">
        <section className="org-card">
          <h2>New organization</h2>
          <form className="org-invite-form" onSubmit={handleCreate}>
            <label className="field">
              <span>Organization name</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                minLength={2}
                maxLength={120}
                required
                placeholder="Acme Manufacturing"
              />
            </label>
            <label className="field">
              <span>First admin's email</span>
              <input
                type="email"
                value={adminEmail}
                onChange={(e) => setAdminEmail(e.target.value)}
                required
                placeholder="it-lead@acme.com"
              />
            </label>
            <button className="btn btn-primary" type="submit" disabled={busy}>
              {busy ? "Creating…" : "Create and invite"}
            </button>
          </form>
          {lastInvite && (
            <LinkNotice
              invite={lastInvite.invite}
              email={lastInvite.email}
              onDismiss={() => setLastInvite(null)}
            />
          )}
        </section>

        <section className="org-card org-card-wide">
          <h2>All organizations</h2>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          {loading ? (
            <p className="loading-copy">Loading…</p>
          ) : (
            <div className="org-table-wrap">
              <table className="org-table">
                <thead>
                  <tr>
                    <th>Organization</th>
                    <th>Admins</th>
                    <th>People</th>
                    <th>Status</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {orgs.map((o) => {
                    const own = o.id === user?.tenant_id;
                    const busyRow = rowBusy === o.id;
                    return (
                      <tr key={o.id}>
                        <td>
                          {o.name}
                          {own && <span className="org-muted"> (yours)</span>}
                        </td>
                        <td className="org-email">
                          {o.admin_emails.length ? o.admin_emails.join(", ") : (
                            <span className="org-muted">None</span>
                          )}
                          {addAdminFor === o.id && (
                            <form
                              className="org-inline-form"
                              onSubmit={(e) => {
                                e.preventDefault();
                                const email = addAdminEmail.trim();
                                void rowAction(
                                  o.id,
                                  async () => {
                                    const invite = await invitePlatformOrgAdmin(o.id, email);
                                    setLastInvite({ invite, email });
                                    setAddAdminFor(null);
                                    setAddAdminEmail("");
                                  },
                                  "Could not invite the admin",
                                );
                              }}
                            >
                              <input
                                className="org-input"
                                type="email"
                                required
                                value={addAdminEmail}
                                onChange={(e) => setAddAdminEmail(e.target.value)}
                                placeholder="admin@company.com"
                                aria-label="Admin email"
                              />
                              <button className="btn btn-primary btn-sm" type="submit" disabled={busyRow}>
                                Invite
                              </button>
                              <button
                                type="button"
                                className="btn btn-ghost btn-sm"
                                onClick={() => setAddAdminFor(null)}
                              >
                                Cancel
                              </button>
                            </form>
                          )}
                        </td>
                        <td>{o.user_count}</td>
                        <td>
                          {o.disabled ? (
                            <Badge tone="danger">Disabled</Badge>
                          ) : (
                            <Badge tone="success">Active</Badge>
                          )}
                        </td>
                        <td className="org-actions">
                          {addAdminFor !== o.id && (
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm"
                              onClick={() => {
                                setAddAdminFor(o.id);
                                setAddAdminEmail("");
                              }}
                            >
                              Add admin
                            </button>
                          )}
                          {!own && (
                            <button
                              type="button"
                              className={o.disabled ? "btn btn-ghost btn-sm" : "btn btn-danger-ghost btn-sm"}
                              disabled={busyRow}
                              onClick={() =>
                                void rowAction(
                                  o.id,
                                  () => updatePlatformOrg(o.id, { disabled: !o.disabled }),
                                  "Could not update the organization",
                                )
                              }
                            >
                              {o.disabled ? "Enable" : "Disable"}
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
