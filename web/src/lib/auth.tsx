/** Frontend authentication context — token management and user info. */

import { useState, useEffect, createContext, useContext, useCallback } from "react";

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
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({
    token: null,
    user: null,
    loading: true,
    error: null,
  });

  // Check for token in URL hash or query params (from OIDC callback)
  useEffect(() => {
    const url = new URL(window.location.href);
    const token = url.searchParams.get("token");
    if (token) {
      localStorage.setItem("spark-pulse-token", token);
      // Clean URL
      url.searchParams.delete("token");
      window.history.replaceState({}, "", url.toString());
      fetchUser(token);
    } else {
      const stored = localStorage.getItem("spark-pulse-token");
      if (stored) {
        fetchUser(stored);
      } else {
        setState((s) => ({ ...s, loading: false }));
      }
    }
  }, []);

  const fetchUser = async (token: string) => {
    try {
      const res = await fetch("/auth/me", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        const data = await res.json();
        setState({ token, user: data.user || {}, loading: false, error: null });
      } else {
        localStorage.removeItem("spark-pulse-token");
        setState({ token: null, user: null, loading: false, error: "Session expired" });
      }
    } catch {
      setState({ token: null, user: null, loading: false, error: "Auth check failed" });
    }
  };

  const login = useCallback(() => {
    window.location.href = "/auth/login";
  }, []);

  const logout = useCallback(async () => {
    try {
      const token = localStorage.getItem("spark-pulse-token");
      if (token) {
        await fetch("/auth/logout", {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        });
      }
    } catch {
      // Ignore logout errors
    }
    localStorage.removeItem("spark-pulse-token");
    setState({ token: null, user: null, loading: false, error: null });
  }, []);

  return (
    <AuthContext.Provider
      value={{
        ...state,
        login,
        logout,
        isAuthenticated: !!state.token && !state.loading,
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
