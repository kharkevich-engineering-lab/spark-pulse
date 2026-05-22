import { BrowserRouter, Routes, Route } from "react-router-dom";
import Layout from "@/components/Layout";
import { AuthProvider } from "@/lib/auth";
import RecipesPage from "@/pages/RecipesPage";
import JobsPage from "@/pages/JobsPage";
import MemoryPage from "@/pages/MemoryPage";
import CachePage from "@/pages/CachePage";
import MCPPage from "@/pages/MCPPage";
import SettingsPage from "@/pages/SettingsPage";
import ModsPage from "@/pages/ModsPage";

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Layout>
          <Routes>
            <Route path="/" element={<RecipesPage />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/mods" element={<ModsPage />} />
            <Route path="/monitoring" element={<MemoryPage />} />
            <Route path="/cache" element={<CachePage />} />
            <Route path="/mcp" element={<MCPPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </Layout>
      </AuthProvider>
    </BrowserRouter>
  );
}
