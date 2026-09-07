/** Login page — redirects to OIDC provider for authentication. */

import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { PulseIcon } from "@/components/BrandIcons";
import { BrandFooter } from "@/components/BrandFooter";

export default function LoginPage() {
  const { isAuthenticated, login } = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    // If already authenticated, redirect to home
    if (isAuthenticated) {
      navigate("/", { replace: true });
    }
  }, [isAuthenticated, navigate]);

  return (
    <div className="flex min-h-screen flex-col bg-background">
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-primary/10">
            <PulseIcon className="text-primary" size={32} />
          </div>
          <div className="mb-2 text-2xl font-bold text-foreground">Spark Pulse</div>
          <div className="mb-6 text-sm text-text-muted">Sign in to continue</div>

          <button
            onClick={login}
            className="px-6 py-2.5 rounded-lg bg-primary hover:bg-primary-hover text-white font-medium transition-colors"
            type="button"
          >
            Sign In
          </button>
        </div>
      </div>
      <BrandFooter />
    </div>
  );
}
