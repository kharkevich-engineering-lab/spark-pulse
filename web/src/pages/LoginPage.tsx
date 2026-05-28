/** Login page — redirects to OIDC provider for authentication. */

import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { Copyright, Zap } from "lucide-react";
import { SiGithub, SiPypi } from "@icons-pack/react-simple-icons";

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
            <Zap className="h-8 w-8 text-primary" />
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
      <footer className="p-6 text-center text-xs text-text-muted space-y-2">
        <a
          href="https://kharkevich.com"
          target="_blank"
          rel="noopener"
          className="inline-flex items-center gap-1.5 hover:text-text transition-colors"
        >
          <Copyright size={12} />
          {new Date().getFullYear()} Kharkevich Engineering Lab
        </a>
        <div className="flex items-center justify-center gap-4">
          <a
            href="https://github.com/kharkevich-engineering-lab/spark-pulse"
            target="_blank"
            rel="noopener"
            className="inline-flex items-center gap-1.5 hover:text-text transition-colors"
          >
            <SiGithub size={12} />
            GitHub
          </a>
          <a
            href="https://pypi.org/project/spark-pulse/"
            target="_blank"
            rel="noopener"
            className="inline-flex items-center gap-1.5 hover:text-text transition-colors"
          >
            <SiPypi size={12} />
            PyPI
          </a>
        </div>
      </footer>
    </div>
  );
}
