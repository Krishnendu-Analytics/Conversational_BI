import { Link, useNavigate } from "react-router-dom";
import { useState, type FormEvent } from "react";
import { Panel } from "@/components/Panel";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { isStrongPassword, PASSWORD_POLICY } from "@/lib/password";
import { usePublicSignup } from "@/lib/signupConfig";

export function SignupPage() {
  const { signup } = useAuth();
  const navigate = useNavigate();
  const publicSignup = usePublicSignup();

  const [tenantName, setTenantName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    if (!isStrongPassword(password)) {
      setError(PASSWORD_POLICY);
      return;
    }

    setBusy(true);

    try {
      const result = await signup({
        tenant_name: tenantName.trim(),
        email: email.trim(),
        password,
      });
      navigate(`/verify?email=${encodeURIComponent(result.email)}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Signup failed");
    } finally {
      setBusy(false);
    }
  }

  if (publicSignup === false) {
    return (
      <div className="overlay overlay-auth">
        <Panel>
          <p className="brand">Conversational BI</p>
          <h2>Access is by invitation</h2>
          <p className="form-hint">
            Your organization admin can invite you. Check your inbox for an invite
            link, or ask them to send one.
          </p>
          <p className="switch-link">
            Already have access? <Link to="/login">Sign in</Link>
          </p>
        </Panel>
      </div>
    );
  }

  return (
    <div className="overlay overlay-auth">
      <Panel>
        <p className="brand">Conversational BI</p>
        <h2>Create your workspace</h2>
        <p className="form-hint">We will email you an OTP to verify.</p>

        <form className="auth-form" onSubmit={handleSubmit}>
          <label className="field">
            <span>Workspace name</span>
            <input
              value={tenantName}
              onChange={(e) => setTenantName(e.target.value)}
              minLength={2}
              required
              placeholder="Acme Analytics"
            />
          </label>

          <label className="field">
            <span>Work email</span>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              placeholder="you@company.com"
            />
          </label>

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

          {error && <p className="form-error">{error}</p>}

          <button className="btn btn-primary" type="submit" disabled={busy}>
            {busy ? "Working…" : "Continue"}
          </button>
        </form>

        <p className="switch-link">
          Already have access? <Link to="/login">Sign in</Link>
        </p>
      </Panel>
    </div>
  );
}
