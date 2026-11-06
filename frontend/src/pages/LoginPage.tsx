import { Link, useNavigate } from "react-router-dom";
import { useState, type FormEvent } from "react";
import { Panel } from "@/components/Panel";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { usePublicSignup } from "@/lib/signupConfig";

type LoginMode = "password" | "otp";

export function LoginPage() {
  const { login, requestLoginOtp, verifyOtp } = useAuth();
  const navigate = useNavigate();
  const publicSignup = usePublicSignup();

  const [mode, setMode] = useState<LoginMode>("password");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  function switchMode(next: LoginMode) {
    setMode(next);
    setError(null);
    setInfo(null);
    setOtp("");
    setOtpSent(false);
  }

  async function handlePasswordLogin(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);

    try {
      await login({ email: email.trim(), password });
      navigate("/app/ask", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleSendOtp() {
    if (!email.trim()) {
      setError("Enter your email first");
      return;
    }

    setBusy(true);
    setError(null);
    setInfo(null);

    try {
      const result = await requestLoginOtp(email.trim());
      setOtpSent(true);
      setInfo(result.message);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send OTP");
    } finally {
      setBusy(false);
    }
  }

  async function handleOtpLogin(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);

    try {
      await verifyOtp({ email: email.trim(), otp_code: otp.trim() });
      navigate("/app/ask", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "OTP login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay overlay-auth">
      <Panel>
        <p className="brand">Conversational BI</p>
        <h2>Sign in</h2>
        <p className="form-hint">Use your password or a one-time code.</p>

        <div className="mode-tabs" role="tablist" aria-label="Sign in method">
          <button
            type="button"
            role="tab"
            className={`mode-tab ${mode === "password" ? "active" : ""}`}
            aria-selected={mode === "password"}
            onClick={() => switchMode("password")}
          >
            Password
          </button>
          <button
            type="button"
            role="tab"
            className={`mode-tab ${mode === "otp" ? "active" : ""}`}
            aria-selected={mode === "otp"}
            onClick={() => switchMode("otp")}
          >
            OTP
          </button>
        </div>

        {mode === "password" ? (
          <form className="auth-form" onSubmit={handlePasswordLogin}>
            <label className="field">
              <span>Email</span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </label>

            <label className="field">
              <span>Password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </label>

            {error && <p className="form-error">{error}</p>}

            <button className="btn btn-primary" type="submit" disabled={busy}>
              {busy ? "Working…" : "Enter workspace"}
            </button>
          </form>
        ) : (
          <form className="auth-form" onSubmit={handleOtpLogin}>
            <label className="field">
              <span>Email</span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </label>

            {otpSent && (
              <label className="field">
                <span>OTP code</span>
                <input
                  value={otp}
                  onChange={(e) => setOtp(e.target.value)}
                  required
                  minLength={4}
                  maxLength={10}
                  placeholder="••••••"
                />
              </label>
            )}

            {error && <p className="form-error">{error}</p>}
            {info && <p className="form-success">{info}</p>}

            {!otpSent ? (
              <button
                className="btn btn-primary"
                type="button"
                disabled={busy}
                onClick={() => void handleSendOtp()}
              >
                {busy ? "Sending…" : "Send OTP"}
              </button>
            ) : (
              <>
                <button className="btn btn-primary" type="submit" disabled={busy}>
                  {busy ? "Working…" : "Enter with OTP"}
                </button>
                <button
                  className="btn btn-ghost"
                  type="button"
                  disabled={busy}
                  onClick={() => void handleSendOtp()}
                >
                  Resend OTP
                </button>
              </>
            )}
          </form>
        )}

        {publicSignup ? (
          <p className="switch-link">
            New here? <Link to="/signup">Create a workspace</Link>
          </p>
        ) : publicSignup === false ? (
          <p className="switch-link">New here? Ask your organization admin for an invite.</p>
        ) : null}
      </Panel>
    </div>
  );
}
