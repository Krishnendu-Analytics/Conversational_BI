/**
 * Auth state shared across the app.
 * Pages call useAuth() to signup / login / logout / read the current user.
 */
import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import * as api from "./api";
import type { User } from "./api";

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  isLoggedIn: boolean;
  signup: typeof api.signup;
  resendOtp: typeof api.resendOtp;
  requestLoginOtp: typeof api.requestLoginOtp;
  verifyOtp: (data: { email: string; otp_code: string }) => Promise<void>;
  login: (data: { email: string; password: string }) => Promise<void>;
  acceptInvite: (data: { token: string; password: string }) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
  setUser: (user: User | null) => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // Restore session from refresh token if the access JWT is missing or expired.
  async function loadUser() {
    if (!api.getAccessToken() && !api.getRefreshToken()) {
      setUser(null);
      return;
    }

    const ok = await api.ensureAccessToken();
    if (!ok) {
      setUser(null);
      return;
    }

    try {
      setUser(await api.fetchMe());
    } catch {
      setUser(null);
    }
  }

  useEffect(() => {
    const stop = api.onSessionLost(() => setUser(null));
    loadUser().finally(() => setLoading(false));
    return stop;
  }, []);

  const value: AuthContextValue = {
    user,
    loading,
    isLoggedIn: user !== null,
    signup: api.signup,
    resendOtp: api.resendOtp,
    requestLoginOtp: api.requestLoginOtp,
    verifyOtp: async (data) => {
      await api.verifyOtp(data);
      await loadUser();
    },
    login: async (data) => {
      await api.login(data);
      await loadUser();
    },
    acceptInvite: async (data) => {
      await api.acceptInvite(data);
      await loadUser();
    },
    logout: async () => {
      await api.logout();
      setUser(null);
    },
    refreshUser: async () => {
      await loadUser();
    },
    setUser,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
