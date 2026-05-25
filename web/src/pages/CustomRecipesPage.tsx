/** Page for managing custom recipes and mods. */

import { useState, useEffect, useCallback } from "react";
import {
  listCustomRecipes,
  getCustomRecipeContent,
  saveCustomRecipe,
  deleteCustomRecipe,
  listCustomMods,
  getCustomModFiles,
  saveCustomModFiles,
  deleteCustomMod,
} from "@/lib/api";
import type { CustomRecipeInfo, CustomModInfo, ModFileMap } from "@/lib/types";
import { Loader2, Plus, FileText, Box } from "lucide-react";
import { AlertModal } from "@/components/Modal";
import CustomRecipeModal from "@/components/CustomRecipeModal";
import CustomModManager from "@/components/CustomModManager";
import NewRecipeModal from "@/components/NewRecipeModal";
import NewModModal from "@/components/NewModModal";

type Tab = "recipes" | "mods";

export default function CustomRecipesPage() {
  const [tab, setTab] = useState<Tab>("recipes");
  const [recipes, setRecipes] = useState<CustomRecipeInfo[]>([]);
  const [mods, setMods] = useState<CustomModInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);

  // Recipe modal state
  const [selectedRecipe, setSelectedRecipe] = useState<CustomRecipeInfo | null>(null);
  const [showRecipeModal, setShowRecipeModal] = useState(false);
  const [recipeContent, setRecipeContent] = useState("");

  // New recipe modal state
  const [showNewRecipe, setShowNewRecipe] = useState(false);

  // Mod modal state
  const [selectedMod, setSelectedMod] = useState<CustomModInfo | null>(null);
  const [showModModal, setShowModModal] = useState(false);
  const [modFiles, setModFiles] = useState<ModFileMap>({});

  // New mod modal state
  const [showNewMod, setShowNewMod] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [r, m] = await Promise.all([listCustomRecipes(), listCustomMods()]);
      setRecipes(r);
      setMods(m);
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to load" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleOpenRecipe = async (recipe: CustomRecipeInfo) => {
    setSelectedRecipe(recipe);
    try {
      const { content } = await getCustomRecipeContent(recipe.id);
      setRecipeContent(content);
      setShowRecipeModal(true);
    } catch {
      setAlertModal({ title: "Error", message: "Failed to load recipe" });
    }
  };

  const handleSaveRecipe = async (_id: string, _content: string) => {
    if (recipeContent) {
      await saveCustomRecipe(_id, recipeContent);
      await refresh();
    }
  };

  const handleNewRecipeSave = async (_id: string, _name: string, _content: string) => {
    await refresh();
  };

  const handleDeleteRecipe = async (_id: string) => {
    await deleteCustomRecipe(_id);
    await refresh();
  };

  const handleOpenMod = async (mod: CustomModInfo) => {
    setSelectedMod(mod);
    try {
      const { files } = await getCustomModFiles(mod.id);
      setModFiles(files);
      setShowModModal(true);
    } catch {
      setAlertModal({ title: "Error", message: "Failed to load mod" });
    }
  };

  const handleSaveMod = async (_id: string, fileMap: ModFileMap) => {
    await saveCustomModFiles(_id, fileMap);
    await refresh();
  };

  const handleDeleteMod = async (_id: string) => {
    await deleteCustomMod(_id);
    await refresh();
  };

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">My Recipes & Mods</h2>
        <p className="text-text-muted mt-1">Create and manage custom recipes and mods outside spark-vllm-docker</p>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-2 border-b border-border">
        <button
          onClick={() => setTab("recipes")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            tab === "recipes"
              ? "border-primary text-primary"
              : "border-transparent text-text-muted hover:text-text"
          }`}
        >
          <FileText size={14} className="inline mr-1.5" />
          Recipes ({recipes.length})
        </button>
        <button
          onClick={() => setTab("mods")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            tab === "mods"
              ? "border-primary text-primary"
              : "border-transparent text-text-muted hover:text-text"
          }`}
        >
          <Box size={14} className="inline mr-1.5" />
          Mods ({mods.length})
        </button>
      </div>

      {loading && <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>}

      {/* Recipes tab */}
      {tab === "recipes" && !loading && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm text-text-muted">
              {recipes.length === 0 ? "No custom recipes yet. Upload a YAML file to get started." : `${recipes.length} custom recipe${recipes.length > 1 ? "s" : ""}`}
            </p>
            <button
              onClick={() => setShowNewRecipe(true)}
              className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium flex items-center gap-1.5 transition-colors"
            >
              <Plus size={14} />
              New Recipe
            </button>
          </div>
          {recipes.length > 0 && (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {recipes.map((r) => (
                <div
                  key={r.id}
                  onClick={() => handleOpenRecipe(r)}
                  className="p-5 rounded-xl bg-surface border border-border hover:border-primary/50 cursor-pointer transition-colors group"
                >
                  <div className="flex items-start justify-between mb-2">
                    <h3 className="font-semibold text-base group-hover:text-primary transition-colors">{r.name}</h3>
                  </div>
                  <p className="text-xs text-text-muted font-mono">{r.filename}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Mods tab */}
      {tab === "mods" && !loading && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm text-text-muted">
              {mods.length === 0 ? "No custom mods yet." : `${mods.length} custom mod${mods.length > 1 ? "s" : ""}`}
            </p>
            <button
              onClick={() => setShowNewMod(true)}
              className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium flex items-center gap-1.5 transition-colors"
            >
              <Plus size={14} />
              New Mod
            </button>
          </div>
          {mods.length > 0 && (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {mods.map((m) => (
                <div
                  key={m.id}
                  onClick={() => handleOpenMod(m)}
                  className="p-5 rounded-xl bg-surface border border-border hover:border-primary/50 cursor-pointer transition-colors group"
                >
                  <div className="flex items-start justify-between mb-2">
                    <h3 className="font-semibold text-base group-hover:text-primary transition-colors">{m.name}</h3>
                  </div>
                  {m.description && <p className="text-xs text-text-muted mt-1 line-clamp-2">{m.description}</p>}
                  <div className="flex items-center gap-2 mt-3 text-xs text-text-muted">
                    {m.has_run_sh && (
                      <span className="flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-success/15 text-success">
                        <span className="w-1.5 h-1.5 rounded-full bg-success" />run.sh
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Modals */}
      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />
      )}

      {showRecipeModal && selectedRecipe && (
        <CustomRecipeModal
          open={showRecipeModal}
          recipe={selectedRecipe}
          onClose={() => { setShowRecipeModal(false); setSelectedRecipe(null); }}
          onSave={handleSaveRecipe}
          onDelete={handleDeleteRecipe}
          onError={(msg) => setAlertModal({ title: "Error", message: msg })}
        />
      )}

      {showModModal && selectedMod && (
        <CustomModManager
          open={showModModal}
          mod={selectedMod}
          files={modFiles}
          onClose={() => { setShowModModal(false); setSelectedMod(null); }}
          onSave={handleSaveMod}
          onDelete={handleDeleteMod}
          onError={(msg) => setAlertModal({ title: "Error", message: msg })}
        />
      )}

      {showNewRecipe && (
        <NewRecipeModal
          open={showNewRecipe}
          onClose={() => setShowNewRecipe(false)}
          onSave={handleNewRecipeSave}
          onError={(msg) => setAlertModal({ title: "Error", message: msg })}
        />
      )}

      {showNewMod && (
        <NewModModal
          open={showNewMod}
          onClose={() => setShowNewMod(false)}
          onSave={async () => {
            await refresh();
          }}
          onError={(msg) => setAlertModal({ title: "Error", message: msg })}
        />
      )}
    </div>
  );
}
