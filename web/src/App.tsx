import { BrowserRouter, Routes, Route, useLocation, Navigate } from "react-router-dom";
import Layout from "@/components/Layout";
import { AuthProvider } from "@/lib/auth";
import RecipesPage from "@/pages/RecipesPage";
import InferencePage from "@/pages/InferencePage";
import BenchmarkingPage from "@/pages/BenchmarkingPage";
import MemoryPage from "@/pages/MemoryPage";
import CachePage from "@/pages/CachePage";
import MCPPage from "@/pages/MCPPage";
import SettingsPage from "@/pages/SettingsPage";
import LoginPage from "@/pages/LoginPage";
import { isBenchmarkingEnabled } from "@/lib/config";
import { initCsrfToken } from "@/lib/api";

// Initialize CSRF token from meta tag (no-op if meta tag is absent)
initCsrfToken();

// Wrapper that conditionally renders the Benchmarking page based on config
function BenchmarkingRoute() {
  return isBenchmarkingEnabled() ? <BenchmarkingPage /> : <Navigate to="/" replace />;
}

// Inner component that conditionally renders Layout based on route
function AppRoutes() {
  const location = useLocation();
  const isLoginPage = location.pathname === "/login";

  return (
    <>
      {/* Login page renders outside Layout — no sidebar */}
      <Routes>
        <Route path="/login" element={<LoginPage />} />
      </Routes>
      {/* All other pages render inside Layout with sidebar */}
      {!isLoginPage && (
        <Layout>
          <Routes>
            <Route path="/" element={<RecipesPage />} />
            <Route path="/jobs" element={<InferencePage />} />
            <Route path="/benchmarking" element={<BenchmarkingRoute />} />
            <Route path="/monitoring" element={<MemoryPage />} />
            <Route path="/cache" element={<CachePage />} />
            <Route path="/mcp" element={<MCPPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </Layout>
      )}
    </>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}
