import { useEffect, useState, type FormEvent } from "react";
import { MemorySettings } from "@/components/MemorySettings";
import { ApiError, changePassword, updateProfile } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { isStrongPassword, PASSWORD_POLICY } from "@/lib/password";

/** Account settings: profile details + password change. */
export function ProfilePage() {
  const { user, setUser, refreshUser } = useAuth();

  const [email, setEmail] = useState(user?.email ?? "");
  const [tenantName, setTenantName] = useState(user?.tenant_name ?? "");
  const [profileBusy, setProfileBusy] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [profileSuccess, setProfileSuccess] = useState<string | null>(null);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordSuccess, setPasswordSuccess] = useState<string | null>(null);

  useEffect(() => {
    setEmail(user?.email ?? "");
    setTenantName(user?.tenant_name ?? "");
  }, [user?.email, user?.tenant_name]);

  async function handleProfileSave(e: FormEvent) {
    e.preventDefault();
    if (!user) return;

    const nextEmail = email.trim().toLowerCase();
    const nextTenant = tenantName.trim();
    const payload: { email?: string; tenant_name?: string } = {};

    if (nextEmail && nextEmail !== user.email) payload.email = nextEmail;
    if (nextTenant && nextTenant !== (user.tenant_name ?? "")) {
      payload.tenant_name = nextTenant;
    }

    if (!payload.email && !payload.tenant_name) {
      setProfileError("No changes to save");
      setProfileSuccess(null);
      return;
    }

    setProfileBusy(true);
    setProfileError(null);
    setProfileSuccess(null);

    try {
      const updated = await updateProfile(payload);
      setUser(updated);
      setProfileSuccess("Profile updated");
    } catch (err) {
      setProfileError(
        err instanceof ApiError ? err.message : "Could not update profile",
      );
    } finally {
      setProfileBusy(false);
    }
  }

  async function handlePasswordChange(e: FormEvent) {
    e.preventDefault();
    setPasswordError(null);
    setPasswordSuccess(null);

    if (!isStrongPassword(newPassword)) {
      setPasswordError(PASSWORD_POLICY);
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError("New passwords do not match");
      return;
    }

    setPasswordBusy(true);
    try {
      const result = await changePassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordSuccess(result.message || "Password updated successfully.");
    } catch (err) {
      setPasswordError(
        err instanceof ApiError ? err.message : "Could not change password",
      );
    } finally {
      setPasswordBusy(false);
    }
  }

  if (!user) {
    return (
      <div className="profile-page">
        <p className="loading-copy">Loading profile…</p>
      </div>
    );
  }

  return (
    <div className="profile-page">
      <header className="profile-page-header">
        <div>
          <p className="page-kicker">Account</p>
          <h1 className="page-title">Profile</h1>
          <p className="page-lede">
            Update workspace details or change your password.
          </p>
        </div>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => void refreshUser()}
        >
          Refresh
        </button>
      </header>

      <div className="profile-grid">
        <section className="profile-card">
          <h2>Account details</h2>
          <p className="form-hint">
            Workspace ID{" "}
            <code className="profile-code">{user.tenant_id.slice(0, 8)}</code>
            {" · "}
            Role <code className="profile-code">{user.role}</code>
          </p>
          {user.role !== "admin" && (
            <p className="form-hint">
              Departments:{" "}
              {user.departments?.length
                ? user.departments.map((d) => d.name).join(", ")
                : "none yet. Ask your organization admin for access to data."}
            </p>
          )}

          <form className="auth-form" onSubmit={(e) => void handleProfileSave(e)}>
            <label className="field">
              <span>Email</span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                required
                disabled={profileBusy}
              />
            </label>

            <label className="field">
              <span>Organization name</span>
              <input
                type="text"
                value={tenantName}
                onChange={(e) => setTenantName(e.target.value)}
                minLength={2}
                maxLength={120}
                required
                disabled={profileBusy || user.role !== "admin"}
                title={user.role !== "admin" ? "Only an organization admin can rename it" : undefined}
              />
            </label>

            {profileError ? (
              <p className="form-error" role="alert">
                {profileError}
              </p>
            ) : null}
            {profileSuccess ? (
              <p className="form-success" role="status">
                {profileSuccess}
              </p>
            ) : null}

            <div className="form-actions">
              <button
                type="submit"
                className="btn btn-primary"
                disabled={profileBusy}
              >
                {profileBusy ? "Saving…" : "Save changes"}
              </button>
            </div>
          </form>
        </section>

        <section className="profile-card">
          <h2>Change password</h2>
          <p className="form-hint">
            Your current session stays active. The new password applies the next
            time you sign in.
          </p>

          <form
            className="auth-form"
            onSubmit={(e) => void handlePasswordChange(e)}
          >
            <label className="field">
              <span>Current password</span>
              <input
                type="password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                autoComplete="current-password"
                required
                disabled={passwordBusy}
              />
            </label>

            <label className="field">
              <span>New password</span>
              <input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                autoComplete="new-password"
                minLength={8}
                maxLength={128}
                required
                disabled={passwordBusy}
              />
              <p className="form-hint">{PASSWORD_POLICY}</p>
            </label>

            <label className="field">
              <span>Confirm new password</span>
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                autoComplete="new-password"
                minLength={8}
                maxLength={128}
                required
                disabled={passwordBusy}
              />
            </label>

            {passwordError ? (
              <p className="form-error" role="alert">
                {passwordError}
              </p>
            ) : null}
            {passwordSuccess ? (
              <p className="form-success" role="status">
                {passwordSuccess}
              </p>
            ) : null}

            <div className="form-actions">
              <button
                type="submit"
                className="btn btn-primary"
                disabled={passwordBusy}
              >
                {passwordBusy ? "Updating…" : "Update password"}
              </button>
            </div>
          </form>
        </section>

        <MemorySettings />
      </div>
    </div>
  );
}
