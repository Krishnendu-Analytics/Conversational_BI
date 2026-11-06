import { Link, Navigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { usePublicSignup } from "@/lib/signupConfig";

/** Public marketing surface. Authenticated users enter the product. */
export function LandingPage() {
  const { isLoggedIn, loading } = useAuth();
  const publicSignup = usePublicSignup();

  if (loading) {
    return (
      <div className="overlay overlay-center">
        <p className="loading-copy">Loading…</p>
      </div>
    );
  }

  if (isLoggedIn) {
    return <Navigate to="/app/ask" replace />;
  }

  return (
    <div className="marketing">
      <header className="marketing-nav">
        <p className="marketing-logo">Conversational BI</p>
        <nav className="marketing-nav-actions" aria-label="Primary">
          <Link className={publicSignup ? "btn btn-ghost" : "btn btn-primary"} to="/login">
            Sign in
          </Link>
          {publicSignup ? (
            <Link className="btn btn-primary" to="/signup">
              Create workspace
            </Link>
          ) : null}
        </nav>
      </header>

      <section className="marketing-hero">
        <div className="marketing-hero-copy">
          <p className="marketing-eyebrow">Enterprise analytics</p>
          <h1 className="marketing-brand">Conversational BI</h1>
          <p className="marketing-tagline">
            Ask your database in plain English. Get SQL, results, and charts in
            one workspace.
          </p>
          <div className="cta-row">
            {publicSignup ? (
              <Link className="btn btn-primary" to="/signup">
                Start analyzing
              </Link>
            ) : null}
            <Link className={publicSignup ? "btn btn-ghost" : "btn btn-primary"} to="/login">
              Sign in
            </Link>
          </div>
        </div>

        <div className="marketing-preview" aria-hidden>
          <div className="marketing-preview-chat">
            <div className="preview-bubble preview-user">
              Revenue by region last quarter?
            </div>
            <div className="preview-bubble preview-assistant">
              <span className="preview-line" />
              <span className="preview-line short" />
            </div>
          </div>
          <div className="marketing-preview-viz">
            <div className="preview-kpi" />
            <div className="preview-kpi" />
            <div className="preview-chart" />
          </div>
        </div>
      </section>

      <section className="marketing-features" aria-label="Product flow">
        <article>
          <h2>Connect</h2>
          <p>Wire Postgres, MySQL, or SQL Server with least-privilege access.</p>
        </article>
        <article>
          <h2>Ask</h2>
          <p>Natural language questions with document context when you need it.</p>
        </article>
        <article>
          <h2>Visualize</h2>
          <p>KPIs and charts update beside the conversation as answers land.</p>
        </article>
      </section>

      <footer className="marketing-footer">
        <span>Conversational BI</span>
        <span className="marketing-footer-meta">Secure · Multi-tenant · SQL-backed</span>
      </footer>
    </div>
  );
}
