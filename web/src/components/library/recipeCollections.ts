/** What the recipe collections offer, read the way the Recipes page needs it.
 *
 * Each enabled registry publishes collections, and `GET …/state` says, per
 * recipe, whether it is installed, behind, edited here, gone upstream or not
 * installed. The catalogue is the installed recipes with that state laid over
 * them, plus every recipe a collection offers that is not installed. Install
 * and Update both go through the one bulk endpoint, which answers per recipe.
 */

import { useCallback } from "react";
import { applyOciCollection, fetchOciCollections, fetchOciCollectionState } from "@/lib/api";
import { useQuery, type UseQueryResult } from "@/hooks/useQuery";
import type { Translator } from "@/lib/i18n";
import type {
  OciCollectionApplyResult,
  OciCollectionState,
  OciRecipeState,
  OciRecipeStateName,
  RecipeSummary,
} from "@/lib/types";
import type { LibraryState } from "./LibraryState";

/** Every collection's state, or only the ones that could be read: a registry
 *  that does not answer leaves the installed recipes as they are, it does not
 *  take the catalogue down with it. */
export function useCollectionStates(): UseQueryResult<OciCollectionState[]> {
  const fetchAll = useCallback(async (signal?: AbortSignal) => {
    const collections = await fetchOciCollections(undefined, undefined, signal);
    const settled = await Promise.allSettled(
      collections.map((c) => fetchOciCollectionState(c.name, c.registry, signal)),
    );
    return settled.flatMap((r) => (r.status === "fulfilled" ? [r.value] : []));
  }, []);
  return useQuery(fetchAll);
}

/** What a collection's state of a recipe is called in the library's words. */
export function libraryStateOf(state: OciRecipeStateName): LibraryState {
  switch (state) {
    case "installed":
      return "on_nodes";
    case "not_installed":
      return "available";
    case "update":
      return "update";
    case "local_edits":
      return "local";
    case "removed":
      return "removed";
  }
}

/** The one thing a row offers besides opening it. */
export type RecipeAction = "install" | "update";

export function actionOf(recipe: OciRecipeState): RecipeAction | null {
  if (recipe.state === "not_installed") return "install";
  if (recipe.state === "update") return "update";
  if (recipe.state === "local_edits" && recipe.update_available) return "update";
  return null;
}

/** A recipe a collection offers, shaped as the listing's own rows so the
 *  catalogue renders it with the same table. What is not known until it is
 *  installed — its engines — is left empty. */
export function summaryFromOffered(r: OciRecipeState): RecipeSummary {
  return {
    id: r.recipe_id,
    name: r.name,
    model: r.model,
    container: r.container,
    description: r.description,
    solo_only: r.solo_only,
    cluster_only: r.cluster_only,
    mods: [],
    defaults: {},
    is_customized: false,
    recipe_version: "",
    engine: null,
    engines: [],
    params: {},
    source: "oci",
    engine_support: [],
    serves: r.serves,
  };
}

/** Install or update ``names`` from one collection's newest version. */
export function applyRecipes(
  state: OciCollectionState,
  names: string[],
  overwriteLocal = false,
): Promise<OciCollectionApplyResult> {
  return applyOciCollection(state.collection, {
    recipes: names,
    version: state.latest_version,
    registry: state.registry,
    overwrite_local: overwriteLocal || undefined,
  });
}

/** What a bulk apply did: the counts, then each failure by name. */
export function describeOutcome(results: OciCollectionApplyResult[], t: Translator["t"]): string {
  const all = results.flatMap((r) => r.results);
  const done = all.filter((r) => r.success && r.action !== "skipped_local_edits").length;
  const skipped = all.filter((r) => r.action === "skipped_local_edits").length;
  const failed = all.filter((r) => !r.success);
  const lines = [t("collection.outcome", { done, skipped, failed: failed.length })];
  for (const f of failed) lines.push(`${f.recipe}: ${f.error ?? t("common.unknownError")}`);
  return lines.join("\n");
}
