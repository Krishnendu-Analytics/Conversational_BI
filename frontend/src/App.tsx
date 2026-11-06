import { Navigate, Route, Routes } from "react-router-dom";
import { lazy, Suspense } from "react";
import { AppShell } from "@/components/AppShell";
import { AppLayout } from "@/components/AppLayout";
import { GuestOnly, RequireLogin } from "@/components/AuthGate";

const LandingPage = lazy(() =>
  import("@/pages/LandingPage").then((m) => ({ default: m.LandingPage })),
);
const LoginPage = lazy(() =>
  import("@/pages/LoginPage").then((m) => ({ default: m.LoginPage })),
);
const SignupPage = lazy(() =>
  import("@/pages/SignupPage").then((m) => ({ default: m.SignupPage })),
);
const VerifyOtpPage = lazy(() =>
  import("@/pages/VerifyOtpPage").then((m) => ({ default: m.VerifyOtpPage })),
);
const AskPage = lazy(() =>
  import("@/pages/AskPage").then((m) => ({ default: m.AskPage })),
);
const ConnectionsPage = lazy(() =>
  import("@/pages/ConnectionsPage").then((m) => ({ default: m.ConnectionsPage })),
);
const DashboardPage = lazy(() =>
  import("@/pages/DashboardPage").then((m) => ({ default: m.DashboardPage })),
);
const SemanticPage = lazy(() =>
  import("@/pages/SemanticPage").then((m) => ({ default: m.SemanticPage })),
);
const UsagePage = lazy(() =>
  import("@/pages/UsagePage").then((m) => ({ default: m.UsagePage })),
);
const AcceptInvitePage = lazy(() =>
  import("@/pages/AcceptInvitePage").then((m) => ({ default: m.AcceptInvitePage })),
);
const OrganizationPage = lazy(() =>
  import("@/pages/OrganizationPage").then((m) => ({ default: m.OrganizationPage })),
);
const PlatformPage = lazy(() =>
  import("@/pages/PlatformPage").then((m) => ({ default: m.PlatformPage })),
);
const ProfilePage = lazy(() =>
  import("@/pages/ProfilePage").then((m) => ({ default: m.ProfilePage })),
);

function RouteFallback() {
  return (
    <div className="overlay overlay-center">
      <p className="loading-copy">Loading…</p>
    </div>
  );
}

export function App() {
  return (
    <AppShell>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route path="/" element={<LandingPage />} />
          <Route
            path="/login"
            element={
              <GuestOnly>
                <LoginPage />
              </GuestOnly>
            }
          />
          <Route
            path="/signup"
            element={
              <GuestOnly>
                <SignupPage />
              </GuestOnly>
            }
          />
          <Route
            path="/verify"
            element={
              <GuestOnly>
                <VerifyOtpPage />
              </GuestOnly>
            }
          />
          <Route path="/accept-invite" element={<AcceptInvitePage />} />
          <Route
            path="/app"
            element={
              <RequireLogin>
                <AppLayout />
              </RequireLogin>
            }
          >
            <Route index element={<Navigate to="ask" replace />} />
            <Route path="ask/:conversationId?" element={<AskPage />} />
            <Route path="dashboards/:dashboardId" element={<DashboardPage />} />
            <Route path="connections" element={<ConnectionsPage />} />
            <Route path="semantic" element={<SemanticPage />} />
            <Route path="usage" element={<UsagePage />} />
            <Route path="profile" element={<ProfilePage />} />
            <Route path="organization" element={<OrganizationPage />} />
            <Route path="platform" element={<PlatformPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </AppShell>
  );
}
