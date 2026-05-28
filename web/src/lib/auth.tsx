/** Frontend authentication context — token management and user info. */

import { useState, useEffect, createContext, useContext, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { loadConfig, useConfig } from "@/lib/config";

interface User {
  name?: string;
  email?: string;
  sub?: string;
  [key: string]: unknown;
}

interface AuthState {
  token: string | null;
  user: User | null;
  loading: boolean;
  error: string | null;
}

interface AuthContextValue extends AuthState {
  login: () => void;
  logout: () => Promise<void>;
  isAuthenticated: boolean;
  isConfigLoaded: boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({
    token: null,
    user: null,
    loading: true,
    error: null,
  });

  const [configLoaded, setConfigLoaded] = useState(false);
  const navigate = useNavigate();
  const { config } = useConfig();

  // Load runtime config on startup
  useEffect(() => {
    const init = async () => {
      try {
        await loadConfig();
      } finally {
        setConfigLoaded(true);
      }
    };

    void init();
  }, []);

  // When config loads, check auth status
  useEffect(() => {
    if (!configLoaded || config === null) return;

    if (!config.auth_enabled) {
      // Auth disabled — treat user as "in" (no token needed)
      setState({ token: "disabled", user: null, loading: false, error: null });
    } else {
      // Auth enabled — check if user has a valid session
      fetchUser();
    }
  }, [config, configLoaded]);

  const fetchUser = async () => {
    try {
      const res = await fetch("/auth/me", { credentials: "include" });
      if (res.ok) {
        const data = await res.json();
        setState({ token: "cookie", user: data.user || {}, loading: false, error: null });
      } else {
        setState({ token: null, user: null, loading: false, error: null });
      }
    } catch {
      setState({ token: null, user: null, loading: false, error: null });
    }
  };

  const login = useCallback(() => {
    window.location.href = "/auth/login";
  }, []);

  const logout = useCallback(async () => {
    try {
      await fetch("/auth/logout", {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // Ignore logout errors
    }
    setState({ token: null, user: null, loading: false, error: null });
    navigate("/login", { replace: true });
  }, [navigate]);

  return (
    <AuthContext.Provider
      value={{
        ...state,
        login,
        logout,
        isAuthenticated: !!state.token && !state.loading,
        isConfigLoaded: configLoaded,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
