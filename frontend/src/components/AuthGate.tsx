import { useEffect, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { ensureAccessToken } from "@/lib/api";

function Loading() {
  return (
    <div className="overlay overlay-center">
      <p className="loading-copy">Loading…</p>
    </div>
  );
}

/** Only show children if the user is logged in. */
export function RequireLogin({ children }: { children: ReactNode }) {
  const { isLoggedIn, loading } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    if (!loading && !isLoggedIn) {
      navigate("/login", { replace: true });
    }
  }, [loading, isLoggedIn, navigate]);

  useEffect(() => {
    if (!isLoggedIn) return;
    void ensureAccessToken();
  }, [isLoggedIn, location.pathname]);

  if (loading || !isLoggedIn) return <Loading />;
  return children;
}

/** Redirect into the product if already logged in (login / signup / verify only). */
export function GuestOnly({ children }: { children: ReactNode }) {
  const { isLoggedIn, loading } = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (!loading && isLoggedIn) {
      navigate("/app/ask", { replace: true });
    }
  }, [loading, isLoggedIn, navigate]);

  if (loading || isLoggedIn) return <Loading />;
  return children;
}
