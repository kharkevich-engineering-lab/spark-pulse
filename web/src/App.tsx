import { BrowserRouter, Routes, Route, useLocation, Navigate } from "react-router-dom";
import Layout from "@/components/Layout";
import { AuthProvider } from "@/lib/auth";
import { ConfigProvider, useConfig } from "@/lib/config";
import RecipesPage from "@/pages/RecipesPage";
import InferencePage from "@/pages/InferencePage";
import BenchmarkingPage from "@/pages/BenchmarkingPage";
import MemoryPage from "@/pages/MemoryPage";
import CachePage from "@/pages/CachePage";
import ModelsPage from "@/pages/ModelsPage";
import ImagesPage from "@/pages/ImagesPage";
import MCPPage from "@/pages/MCPPage";
import SettingsPage from "@/pages/SettingsPage";
import LoginPage from "@/pages/LoginPage";
import NotFoundPage from "@/pages/NotFoundPage";
import OciRegistryPage from "@/pages/OciRegistryPage";
import ClusterPage from "@/pages/ClusterPage";
import { ErrorBoundary, DefaultErrorFallback } from "@/components/ErrorBoundary";
import { initCsrfToken } from "@/lib/api";

// Initialize CSRF token from meta tag (no-op if meta tag is absent)
initCsrfToken();

// Wrapper that conditionally renders the Benchmarking page based on config
function BenchmarkingRoute() {
  const { config } = useConfig();
  const enabled = config?.benchmarking_enabled ?? false;
  return enabled ? <BenchmarkingPage /> : <Navigate to="/" replace />;
}

/** The application's pages, as data.
 *
 * One list, so "which paths exist" cannot drift from "which paths render".
 * The not-found page needs that question answered — without it an unknown
 * path matched no route and left an empty shell inside the sidebar, which
 * reads as a broken page rather than as a wrong address — and a second,
 * hand-maintained list of paths would have gone stale the first time somebody
 * added a page.
 */
const PAGES: { path: string; element: React.ReactNode }[] = [
  { path: "/", element: <RecipesPage /> },
  { path: "/jobs", element: <InferencePage /> },
  { path: "/cluster", element: <ClusterPage /> },
  { path: "/benchmarking", element: <BenchmarkingRoute /> },
  { path: "/monitoring", element: <MemoryPage /> },
  { path: "/models", element: <ModelsPage /> },
  { path: "/images", element: <ImagesPage /> },
  { path: "/cache", element: <CachePage /> },
  { path: "/mcp", element: <MCPPage /> },
  { path: "/oci", element: <OciRegistryPage /> },
  { path: "/settings", element: <SettingsPage /> },
];

/** Every address this application answers, login included. */
export const KNOWN_PATHS = ["/login", ...PAGES.map((p) => p.path)];

function AppRoutes() {
  const location = useLocation();
  const isLoginPage = location.pathname === "/login";
  // Rendered outside Layout: a not-found page framed by the application's own
  // navigation invites the reader to believe the page half loaded.
  if (!KNOWN_PATHS.includes(location.pathname)) return <NotFoundPage />;

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
            {PAGES.map(({ path, element }) => (
              <Route key={path} path={path} element={element} />
            ))}
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
        <ConfigProvider>
          <ErrorBoundary fallback={<DefaultErrorFallback />}>
            <AppRoutes />
          </ErrorBoundary>
        </ConfigProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
