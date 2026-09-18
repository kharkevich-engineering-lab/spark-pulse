import { BrowserRouter, Routes, Route, useLocation, Navigate } from "react-router-dom";
import Layout from "@/components/Layout";
import { AuthProvider } from "@/lib/auth";
import { ConfigProvider, useConfig } from "@/lib/config";
import { I18nProvider } from "@/lib/i18n";
import RecipesPage from "@/pages/RecipesPage";
import RunsPage from "@/pages/RunsPage";
import LibraryPage from "@/pages/LibraryPage";
import MCPPage from "@/pages/MCPPage";
import SettingsPage from "@/pages/SettingsPage";
import LoginPage from "@/pages/LoginPage";
import NotFoundPage from "@/pages/NotFoundPage";
import FleetPage from "@/pages/FleetPage";
import { ErrorBoundary, DefaultErrorFallback } from "@/components/ErrorBoundary";
import { initCsrfToken } from "@/lib/api";

// Initialize CSRF token from meta tag (no-op if meta tag is absent)
initCsrfToken();

/** `/benchmarking` is Runs, opened on its Benchmarks tab.
 *
 * The page it used to render is that tab now. The route is kept because
 * bookmarks, the MCP tools and the docs all name it — and because a benchmark
 * is something you do to a run, so the two were never separate destinations.
 * With the feature off there is no tab to open, and the route goes home. */
function BenchmarkingRoute() {
  const { config, configLoaded } = useConfig();
  // Wait for the answer rather than assume one. `/api/config` arrives a tick
  // after the first render, so deciding on a null config sent *every* direct
  // navigation home — a bookmark, a link in the docs, the URL the MCP tools
  // hand back — and the route only ever worked when it was reached from
  // inside the app. Nothing renders while the question is open, which is not
  // the same as answering it no.
  if (!configLoaded) return null;
  return config?.benchmarking_enabled ? (
    <RunsPage initialTab="benchmarks" />
  ) : (
    <Navigate to="/" replace />
  );
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
  { path: "/jobs", element: <RunsPage /> },
  { path: "/cluster", element: <FleetPage /> },
  { path: "/benchmarking", element: <BenchmarkingRoute /> },
  // Monitoring is a tab of Fleet; the route deep-links to it.
  { path: "/monitoring", element: <FleetPage /> },
  // Four addresses, one page. Library is tabbed, and each tab keeps the
  // route it had as a page so nothing an operator bookmarked stops resolving;
  // `/cache` opens the Models tab at the caches section.
  { path: "/models", element: <LibraryPage /> },
  { path: "/engines", element: <LibraryPage /> },
  { path: "/cache", element: <LibraryPage /> },
  { path: "/mcp", element: <MCPPage /> },
  { path: "/oci", element: <LibraryPage /> },
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
      <I18nProvider>
        <AuthProvider>
          <ConfigProvider>
            <ErrorBoundary fallback={<DefaultErrorFallback />}>
              <AppRoutes />
            </ErrorBoundary>
          </ConfigProvider>
        </AuthProvider>
      </I18nProvider>
    </BrowserRouter>
  );
}
