import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useState, type FormEvent } from "react";
import { Panel } from "@/components/Panel";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export function VerifyOtpPage() {
  const { verifyOtp, resendOtp } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const emailFromSignup = searchParams.get("email") ?? "";

  const [email, setEmail] = useState(emailFromSignup);
  const [otp, setOtp] = useState("");
  const [busy, setBusy] = useState(false);
  const [resending, setResending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);

    try {
      await verifyOtp({ email: email.trim(), otp_code: otp.trim() });
      navigate("/app/ask", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Verification failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleResend() {
    if (!email.trim()) {
      setError("Enter your email first");
      return;
    }

    setResending(true);
    setError(null);
    try {
      const result = await resendOtp(email.trim());
      setInfo(result.message);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not resend OTP");
    } finally {
      setResending(false);
    }
  }

  return (
    <div className="overlay overlay-auth">
      <Panel>
        <p className="brand">Conversational BI</p>
        <h2>Verify your email</h2>
        <p className="form-hint">Enter the OTP from signup to finish creating your account.</p>

        <form className="auth-form" onSubmit={handleSubmit}>
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

          {error && <p className="form-error">{error}</p>}
          {info && <p className="form-success">{info}</p>}

          <button className="btn btn-primary" type="submit" disabled={busy}>
            {busy ? "Working…" : "Verify & enter"}
          </button>
        </form>

        <div className="cta-row tight">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => void handleResend()}
            disabled={resending}
          >
            {resending ? "Sending…" : "Resend OTP"}
          </button>
          <Link className="text-link" to="/login">
            Back to sign in
          </Link>
        </div>
      </Panel>
    </div>
  );
}
