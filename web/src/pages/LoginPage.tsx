/** Login page — redirects to OIDC provider for authentication. */

import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { isAuthEnabled, loadConfig } from "@/lib/config";
import { LogIn } from "lucide-react";

export default function LoginPage() {
  const { isAuthenticated, login } = useAuth();
  const navigate = useNavigate();
  const [authReady, setAuthReady] = useState(false);

  useEffect(() => {
    // Load config on mount
    loadConfig().then(() => setAuthReady(true)).catch(() => setAuthReady(true));
  }, []);

  useEffect(() => {
    if (!authReady) return;
    // If already authenticated, redirect to home
    if (isAuthenticated) {
      navigate("/", { replace: true });
    }
  }, [isAuthenticated, navigate, authReady]);

  // Still loading config — show loading state
  if (!authReady) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <div className="text-center">
          <div className="mx-auto mb-4 h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent"></div>
          <p className="text-muted-foreground">Loading...</p>
        </div>
      </div>
    );
  }

  // If auth is not enabled, redirect to home
  if (!isAuthEnabled()) {
    navigate("/", { replace: true });
    return null;
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="text-center">
        <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-primary/10">
          <LogIn className="h-8 w-8 text-primary" />
        </div>
        <h1 className="mb-2 text-2xl font-bold text-foreground">Sign In</h1>
        <p className="mb-6 text-muted-foreground">
          Authenticate with your organization&apos;s identity provider
        </p>
        <button
          onClick={login}
          className="px-6 py-2.5 rounded-lg bg-primary hover:bg-primary-hover text-white font-medium transition-colors"
        >
          Sign in with SSO
        </button>
      </div>
    </div>
  );
}
