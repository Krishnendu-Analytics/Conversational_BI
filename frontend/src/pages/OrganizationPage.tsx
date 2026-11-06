import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { Badge } from "@/components/Badge";
import {
  ApiError,
  createDepartment,
  deleteDepartment,
  getConnectionSchema,
  inviteOrgUser,
  listConnections,
  listDepartments,
  listOrgUsers,
  removeOrgUser,
  resendOrgInvite,
  setDepartmentGrants,
  setDepartmentMembers,
  updateDepartment,
  updateOrgUser,
  type Connection,
  type Department,
  type DepartmentGrant,
  type InviteResult,
  type OrgRole,
  type OrgUser,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

type Tab = "members" | "departments";

const ROLE_LABEL: Record<OrgRole, string> = {
  admin: "Admin",
  analyst: "Member",
  viewer: "Viewer",
};

const ROLE_HINT: Record<OrgRole, string> = {
  admin: "Sees every connection and manages the organization",
  analyst: "Asks questions and builds dashboards on their departments' data",
  viewer: "Read-only access to their departments' data",
};

function errorText(err: unknown, fallback: string) {
  return err instanceof ApiError ? err.message : fallback;
}

/** Link to share when the invite email couldn't be sent. */
function InviteLinkNotice({ invite, onDismiss }: { invite: InviteResult; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false);
  if (invite.email_sent || !invite.invite_url) return null;
  const url = invite.invite_url;
  return (
    <div className="org-invite-link" role="status">
      <p>
        <strong>The invite email couldn't be sent.</strong> Share this one-time link with
        them directly:
      </p>
      <div className="org-invite-link-row">
        <input readOnly value={url} onFocus={(e) => e.currentTarget.select()} aria-label="Invite link" />
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => {
            void navigator.clipboard?.writeText(url).then(() => setCopied(true));
          }}
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

/** Org admin: invite members, group them into departments, grant data access. */
export function OrganizationPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState<Tab>("members");
  const [users, setUsers] = useState<OrgUser[]>([]);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [u, d, c] = await Promise.all([listOrgUsers(), listDepartments(), listConnections()]);
      setUsers(u);
      setDepartments(d);
      setConnections(c);
      setError(null);
    } catch (err) {
      setError(errorText(err, "Could not load the organization"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  if (user && user.role !== "admin") return <Navigate to="/app/ask" replace />;

  return (
    <div className="org-page">
      <header className="org-page-header">
        <div>
          <p className="page-kicker">Admin</p>
          <h1 className="page-title">{user?.tenant_name || "Organization"}</h1>
          <p className="page-lede">
            Invite people, group them into departments, and choose which connections and
            schemas each department can query.
          </p>
        </div>
        <div className="seg" role="tablist" aria-label="Organization sections">
          {(["members", "departments"] as const).map((t) => (
            <button
              key={t}
              type="button"
              role="tab"
              className="seg-btn"
              aria-pressed={tab === t}
              aria-selected={tab === t}
              onClick={() => setTab(t)}
            >
              {t === "members" ? `Members (${users.length})` : `Departments (${departments.length})`}
            </button>
          ))}
        </div>
      </header>

      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {loading ? (
        <p className="loading-copy">Loading…</p>
      ) : tab === "members" ? (
        <MembersTab
          meId={user?.id ?? ""}
          users={users}
          departments={departments}
          onChanged={reload}
        />
      ) : (
        <DepartmentsTab
          users={users}
          departments={departments}
          connections={connections}
          onChanged={reload}
        />
      )}
    </div>
  );
}

// ----- Members -----

function MembersTab({
  meId,
  users,
  departments,
  onChanged,
}: {
  meId: string;
  users: OrgUser[];
  departments: Department[];
  onChanged: () => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<OrgRole>("analyst");
  const [deptIds, setDeptIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pendingLink, setPendingLink] = useState<InviteResult | null>(null);
  const [rowBusy, setRowBusy] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);
  const deptName = useMemo(
    () => new Map(departments.map((d) => [d.id, d.name])),
    [departments],
  );

  async function handleInvite(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setFormError(null);
    setNotice(null);
    try {
      const result = await inviteOrgUser({
        email: email.trim(),
        role,
        department_ids: role === "admin" ? [] : deptIds,
      });
      setPendingLink(result.invite);
      setNotice(
        result.invite.email_sent ? `Invite sent to ${result.user.email}.` : null,
      );
      setEmail("");
      setDeptIds([]);
      setRole("analyst");
      await onChanged();
    } catch (err) {
      setFormError(errorText(err, "Could not send the invite"));
    } finally {
      setBusy(false);
    }
  }

  async function run(userId: string, action: () => Promise<unknown>, fallback: string) {
    setRowBusy(userId);
    setRowError(null);
    try {
      await action();
      await onChanged();
    } catch (err) {
      setRowError(errorText(err, fallback));
    } finally {
      setRowBusy(null);
    }
  }

  return (
    <div className="org-grid">
      <section className="org-card">
        <h2>Invite someone</h2>
        <p className="form-hint">They'll get an email link to set a password and join.</p>
        <form className="org-invite-form" onSubmit={handleInvite}>
          <label className="field">
            <span>Email</span>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="name@company.com"
            />
          </label>
          <label className="field">
            <span>Role</span>
            <select
              className="org-select"
              value={role}
              onChange={(e) => setRole(e.target.value as OrgRole)}
            >
              {(Object.keys(ROLE_LABEL) as OrgRole[]).map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABEL[r]}
                </option>
              ))}
            </select>
            <p className="form-hint">{ROLE_HINT[role]}</p>
          </label>
          {role !== "admin" && (
            <fieldset className="org-fieldset">
              <legend>Departments</legend>
              {departments.length === 0 ? (
                <p className="form-hint">
                  No departments yet. Create one in the Departments tab, or invite now and
                  assign later. Without a department they won't see any data.
                </p>
              ) : (
                <div className="org-chips">
                  {departments.map((d) => (
                    <label key={d.id} className="org-chip">
                      <input
                        type="checkbox"
                        checked={deptIds.includes(d.id)}
                        onChange={(e) =>
                          setDeptIds((prev) =>
                            e.target.checked ? [...prev, d.id] : prev.filter((x) => x !== d.id),
                          )
                        }
                      />
                      <span>{d.name}</span>
                    </label>
                  ))}
                </div>
              )}
            </fieldset>
          )}
          {formError && (
            <p className="form-error" role="alert">
              {formError}
            </p>
          )}
          {notice && (
            <p className="form-success" role="status">
              {notice}
            </p>
          )}
          <button className="btn btn-primary" type="submit" disabled={busy}>
            {busy ? "Sending…" : "Send invite"}
          </button>
        </form>
        {pendingLink && (
          <InviteLinkNotice invite={pendingLink} onDismiss={() => setPendingLink(null)} />
        )}
      </section>

      <section className="org-card org-card-wide">
        <h2>Members</h2>
        {rowError && (
          <p className="form-error" role="alert">
            {rowError}
          </p>
        )}
        <div className="org-table-wrap">
          <table className="org-table">
            <thead>
              <tr>
                <th>Email</th>
                <th>Role</th>
                <th>Departments</th>
                <th>Status</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const isMe = u.id === meId;
                const busyRow = rowBusy === u.id;
                return (
                  <tr key={u.id}>
                    <td className="org-email">
                      {u.email}
                      {isMe && <span className="org-muted"> (you)</span>}
                    </td>
                    <td>
                      <select
                        className="org-select"
                        value={u.role}
                        disabled={isMe || busyRow}
                        title={isMe ? "You can't change your own role" : ROLE_HINT[u.role]}
                        onChange={(e) =>
                          void run(
                            u.id,
                            () => updateOrgUser(u.id, { role: e.target.value as OrgRole }),
                            "Could not change the role",
                          )
                        }
                      >
                        {(Object.keys(ROLE_LABEL) as OrgRole[]).map((r) => (
                          <option key={r} value={r}>
                            {ROLE_LABEL[r]}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      {u.role === "admin" ? (
                        <span className="org-muted">All data</span>
                      ) : (
                        <DepartmentPicker
                          departments={departments}
                          selected={u.department_ids}
                          disabled={busyRow}
                          label={
                            u.department_ids.length
                              ? u.department_ids.map((id) => deptName.get(id) ?? "?").join(", ")
                              : "None — no data access"
                          }
                          onSave={(ids) =>
                            run(
                              u.id,
                              () => updateOrgUser(u.id, { department_ids: ids }),
                              "Could not update departments",
                            )
                          }
                        />
                      )}
                    </td>
                    <td>
                      {u.status === "active" ? (
                        <Badge tone="success">Active</Badge>
                      ) : (
                        <Badge tone="warning">Invited</Badge>
                      )}
                    </td>
                    <td className="org-actions">
                      {u.status === "invited" && (
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          disabled={busyRow}
                          onClick={() =>
                            void run(
                              u.id,
                              async () => setPendingLink(await resendOrgInvite(u.id)),
                              "Could not resend the invite",
                            )
                          }
                        >
                          Resend
                        </button>
                      )}
                      {!isMe &&
                        (confirmRemove === u.id ? (
                          <>
                            <button
                              type="button"
                              className="btn btn-danger btn-sm"
                              disabled={busyRow}
                              onClick={() => {
                                setConfirmRemove(null);
                                void run(u.id, () => removeOrgUser(u.id), "Could not remove");
                              }}
                            >
                              Remove
                            </button>
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm"
                              onClick={() => setConfirmRemove(null)}
                            >
                              Cancel
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            className="btn btn-danger-ghost btn-sm"
                            disabled={busyRow}
                            onClick={() => setConfirmRemove(u.id)}
                          >
                            Remove
                          </button>
                        ))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function DepartmentPicker({
  departments,
  selected,
  disabled,
  label,
  onSave,
}: {
  departments: Department[];
  selected: string[];
  disabled: boolean;
  label: string;
  onSave: (ids: string[]) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string[]>(selected);

  useEffect(() => {
    if (!open) setDraft(selected);
  }, [open, selected]);

  if (!open) {
    return (
      <button
        type="button"
        className="org-link-btn"
        disabled={disabled}
        onClick={() => setOpen(true)}
        title="Change departments"
      >
        {label}
      </button>
    );
  }
  return (
    <div className="org-picker">
      {departments.length === 0 ? (
        <p className="form-hint">Create a department first.</p>
      ) : (
        departments.map((d) => (
          <label key={d.id} className="org-check">
            <input
              type="checkbox"
              checked={draft.includes(d.id)}
              onChange={(e) =>
                setDraft((prev) =>
                  e.target.checked ? [...prev, d.id] : prev.filter((x) => x !== d.id),
                )
              }
            />
            <span>{d.name}</span>
          </label>
        ))
      )}
      <div className="org-picker-actions">
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={() => {
            void onSave(draft).then(() => setOpen(false));
          }}
        >
          Save
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </div>
  );
}

// ----- Departments -----

function DepartmentsTab({
  users,
  departments,
  connections,
  onChanged,
}: {
  users: OrgUser[];
  departments: Department[];
  connections: Connection[];
  onChanged: () => Promise<void>;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(departments[0]?.id ?? null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (selectedId && departments.some((d) => d.id === selectedId)) return;
    setSelectedId(departments[0]?.id ?? null);
  }, [departments, selectedId]);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await createDepartment({ name: name.trim() });
      setName("");
      await onChanged();
      setSelectedId(created.id);
    } catch (err) {
      setError(errorText(err, "Could not create the department"));
    } finally {
      setBusy(false);
    }
  }

  const selected = departments.find((d) => d.id === selectedId) ?? null;

  return (
    <div className="org-dept-layout">
      <aside className="org-card org-dept-list">
        <h2>Departments</h2>
        <form className="org-inline-form" onSubmit={handleCreate}>
          <input
            className="org-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. HR, Accounts"
            aria-label="New department name"
            required
          />
          <button className="btn btn-primary btn-sm" type="submit" disabled={busy}>
            Add
          </button>
        </form>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {departments.length === 0 ? (
          <p className="form-hint">
            Create a department for each team that should see a different slice of data.
          </p>
        ) : (
          <ul className="org-dept-items">
            {departments.map((d) => (
              <li key={d.id}>
                <button
                  type="button"
                  className={d.id === selectedId ? "org-dept-item is-active" : "org-dept-item"}
                  onClick={() => setSelectedId(d.id)}
                >
                  <span>{d.name}</span>
                  <span className="org-muted">
                    {d.member_ids.length} {d.member_ids.length === 1 ? "person" : "people"} ·{" "}
                    {(() => {
                      const n = new Set(d.grants.map((g) => g.connection_id)).size;
                      return `${n} ${n === 1 ? "source" : "sources"}`;
                    })()}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      {selected ? (
        <DepartmentEditor
          key={selected.id}
          department={selected}
          users={users}
          connections={connections}
          onChanged={onChanged}
        />
      ) : (
        <section className="org-card org-empty">
          <p className="form-hint">Select or create a department to manage its people and data.</p>
        </section>
      )}
    </div>
  );
}

function DepartmentEditor({
  department,
  users,
  connections,
  onChanged,
}: {
  department: Department;
  users: OrgUser[];
  connections: Connection[];
  onChanged: () => Promise<void>;
}) {
  const [name, setName] = useState(department.name);
  const [description, setDescription] = useState(department.description ?? "");
  const [members, setMembers] = useState<string[]>(department.member_ids);
  const [grants, setGrants] = useState<DepartmentGrant[]>(department.grants);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const nonAdmins = users.filter((u) => u.role !== "admin");
  const membersDirty =
    [...members].sort().join() !== [...department.member_ids].sort().join();
  const grantKey = (gs: DepartmentGrant[]) =>
    gs.map((g) => `${g.connection_id}:${g.schema_name ?? "*"}`).sort().join();
  const grantsDirty = grantKey(grants) !== grantKey(department.grants);

  async function save(what: string, action: () => Promise<unknown>) {
    setBusy(what);
    setError(null);
    setSaved(null);
    try {
      await action();
      await onChanged();
      setSaved(what);
    } catch (err) {
      setError(errorText(err, "Could not save"));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="org-card org-dept-editor">
      <div className="org-dept-head">
        <div className="org-dept-fields">
          <label className="field">
            <span>Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
          </label>
          <label className="field">
            <span>Description</span>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional"
            />
          </label>
        </div>
        <div className="org-row-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={
              busy !== null ||
              !name.trim() ||
              (name.trim() === department.name &&
                description.trim() === (department.description ?? ""))
            }
            onClick={() =>
              void save("details", () =>
                updateDepartment(department.id, {
                  name: name.trim(),
                  description: description.trim() || null,
                }),
              )
            }
          >
            Save details
          </button>
          {confirmDelete ? (
            <>
              <button
                type="button"
                className="btn btn-danger btn-sm"
                onClick={() => void save("delete", () => deleteDepartment(department.id))}
              >
                Delete department
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => setConfirmDelete(false)}
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              className="btn btn-danger-ghost btn-sm"
              onClick={() => setConfirmDelete(true)}
            >
              Delete
            </button>
          )}
        </div>
      </div>

      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {saved && (
        <p className="form-success" role="status">
          {saved === "grants" ? "Data access saved." : saved === "members" ? "People saved." : "Saved."}
        </p>
      )}

      <div className="org-dept-sections">
        <div>
          <h3>People</h3>
          <p className="form-hint">Admins aren't listed — they already see everything.</p>
          {nonAdmins.length === 0 ? (
            <p className="form-hint">Invite members first in the Members tab.</p>
          ) : (
            <ul className="org-check-list">
              {nonAdmins.map((u) => (
                <li key={u.id}>
                  <label className="org-check">
                    <input
                      type="checkbox"
                      checked={members.includes(u.id)}
                      onChange={(e) =>
                        setMembers((prev) =>
                          e.target.checked ? [...prev, u.id] : prev.filter((x) => x !== u.id),
                        )
                      }
                    />
                    <span>{u.email}</span>
                    {u.status === "invited" && <Badge tone="warning">Invited</Badge>}
                  </label>
                </li>
              ))}
            </ul>
          )}
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!membersDirty || busy !== null}
            onClick={() =>
              void save("members", () => setDepartmentMembers(department.id, members))
            }
          >
            {busy === "members" ? "Saving…" : "Save people"}
          </button>
        </div>

        <div>
          <h3>Data access</h3>
          <p className="form-hint">
            Members can only see and query what's checked here. Pick a whole connection, or
            just some of its schemas.
          </p>
          {connections.length === 0 ? (
            <p className="form-hint">No connections yet.</p>
          ) : (
            <ul className="org-grant-list">
              {connections.map((c) => (
                <ConnectionGrantRow
                  key={c.id}
                  connection={c}
                  grants={grants.filter((g) => g.connection_id === c.id)}
                  onChange={(next) =>
                    setGrants((prev) => [
                      ...prev.filter((g) => g.connection_id !== c.id),
                      ...next,
                    ])
                  }
                />
              ))}
            </ul>
          )}
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!grantsDirty || busy !== null}
            onClick={() => void save("grants", () => setDepartmentGrants(department.id, grants))}
          >
            {busy === "grants" ? "Saving…" : "Save data access"}
          </button>
        </div>
      </div>
    </section>
  );
}

function ConnectionGrantRow({
  connection,
  grants,
  onChange,
}: {
  connection: Connection;
  grants: DepartmentGrant[];
  onChange: (next: DepartmentGrant[]) => void;
}) {
  // "schemas" with nothing ticked yet is a real UI state but grants no data,
  // so the mode is kept locally rather than derived from the grants alone.
  const [mode, setMode] = useState<"off" | "whole" | "schemas">(
    grants.length === 0 ? "off" : grants.some((g) => g.schema_name === null) ? "whole" : "schemas",
  );
  const [schemas, setSchemas] = useState<string[] | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const picked = grants.filter((g) => g.schema_name !== null).map((g) => g.schema_name as string);

  useEffect(() => {
    if (mode !== "schemas" || schemas !== null) return;
    getConnectionSchema(connection.id)
      .then((s) => {
        const names = new Set<string>(s.schemas_used ?? []);
        for (const t of s.tables ?? []) if (t.schema_name) names.add(t.schema_name);
        setSchemas([...names].sort());
      })
      .catch(() => {
        setSchemas([]);
        setSchemaError("Discover this connection's schema first to pick schemas.");
      });
  }, [mode, schemas, connection.id]);

  const toGrant = (schema_name: string | null): DepartmentGrant => ({
    connection_id: connection.id,
    schema_name,
  });

  function choose(next: "off" | "whole" | "schemas") {
    setMode(next);
    if (next === "off") onChange([]);
    else if (next === "whole") onChange([toGrant(null)]);
    else onChange(picked.map(toGrant));
  }

  return (
    <li className="org-grant">
      <label className="org-check">
        <input
          type="checkbox"
          checked={mode !== "off"}
          onChange={(e) => choose(e.target.checked ? "whole" : "off")}
        />
        <span className="org-grant-name">{connection.name}</span>
        <span className="org-muted">{connection.db_type}</span>
      </label>
      {mode !== "off" && (
        <div className="org-grant-scope">
          <div className="seg" role="group" aria-label={`${connection.name} scope`}>
            <button
              type="button"
              className="seg-btn"
              aria-pressed={mode === "whole"}
              onClick={() => choose("whole")}
            >
              Entire connection
            </button>
            <button
              type="button"
              className="seg-btn"
              aria-pressed={mode === "schemas"}
              onClick={() => choose("schemas")}
            >
              Selected schemas
            </button>
          </div>
          {mode === "whole" ? null : schemas === null ? (
            <p className="form-hint">Loading schemas…</p>
          ) : schemaError || schemas.length === 0 ? (
            <p className="form-hint">
              {schemaError ?? "No schemas discovered yet. Discover this connection first."}
            </p>
          ) : (
            <>
              <div className="org-chips">
                {schemas.map((s) => (
                  <label key={s} className="org-chip">
                    <input
                      type="checkbox"
                      checked={picked.includes(s)}
                      onChange={(e) => {
                        const next = e.target.checked
                          ? [...picked, s]
                          : picked.filter((x) => x !== s);
                        onChange(next.map(toGrant));
                      }}
                    />
                    <span>{s}</span>
                  </label>
                ))}
              </div>
              {picked.length === 0 && (
                <p className="form-hint">Pick at least one schema, or this grants nothing.</p>
              )}
            </>
          )}
        </div>
      )}
    </li>
  );
}
