import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useEffect, useState, type FormEvent } from "react";
import { Panel } from "@/components/Panel";
import { ApiError, previewInvite } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { isStrongPassword, PASSWORD_POLICY } from "@/lib/password";

/** Invite link landing: set a password and join the organization. */
export function AcceptInvitePage() {
  const { acceptInvite } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";

  const [invite, setInvite] = useState<{ email: string; tenant_name: string } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) {
      setLoadError("This invite link is incomplete. Open the link from your email again.");
      return;
    }
    previewInvite(token)
      .then(setInvite)
      .catch((err) =>
        setLoadError(
          err instanceof ApiError ? err.message : "Could not open this invite link",
        ),
      );
  }, [token]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!isStrongPassword(password)) {
      setError(PASSWORD_POLICY);
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setBusy(true);
    try {
      await acceptInvite({ token, password });
      navigate("/app/ask", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not accept the invite");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay overlay-auth">
      <Panel>
        <p className="brand">Conversational BI</p>
        {loadError ? (
          <>
            <h2>Invite unavailable</h2>
            <p className="form-error" role="alert">
              {loadError}
            </p>
            <p className="form-hint">Ask your organization admin to send a new invite.</p>
            <p className="switch-link">
              Already joined? <Link to="/login">Sign in</Link>
            </p>
          </>
        ) : !invite ? (
          <p className="loading-copy">Checking your invite…</p>
        ) : (
          <>
            <h2>Join {invite.tenant_name}</h2>
            <p className="form-hint">
              Set a password for <strong>{invite.email}</strong> to finish joining.
            </p>
            <form className="auth-form" onSubmit={handleSubmit}>
              <label className="field">
                <span>Password</span>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  minLength={8}
                  maxLength={128}
                  required
                  autoComplete="new-password"
                />
                <p className="form-hint">{PASSWORD_POLICY}</p>
              </label>
              <label className="field">
                <span>Confirm password</span>
                <input
                  type="password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  required
                  autoComplete="new-password"
                />
              </label>
              {error && (
                <p className="form-error" role="alert">
                  {error}
                </p>
              )}
              <button className="btn btn-primary" type="submit" disabled={busy}>
                {busy ? "Joining…" : "Join organization"}
              </button>
            </form>
          </>
        )}
      </Panel>
    </div>
  );
}
