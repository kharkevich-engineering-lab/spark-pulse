import { BrowserRouter, Routes, Route, useLocation } from "react-router-dom";
import Layout from "@/components/Layout";
import { AuthProvider } from "@/lib/auth";
import RecipesPage from "@/pages/RecipesPage";
import JobsPage from "@/pages/JobsPage";
import MemoryPage from "@/pages/MemoryPage";
import CachePage from "@/pages/CachePage";
import MCPPage from "@/pages/MCPPage";
import SettingsPage from "@/pages/SettingsPage";
import LoginPage from "@/pages/LoginPage";
import CustomRecipesPage from "@/pages/CustomRecipesPage";

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
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/monitoring" element={<MemoryPage />} />
            <Route path="/cache" element={<CachePage />} />
            <Route path="/mcp" element={<MCPPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/custom-recipes" element={<CustomRecipesPage />} />
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
