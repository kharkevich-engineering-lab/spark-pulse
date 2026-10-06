/** One collection: each recipe's state, and at most one thing to do about it.
 *
 * This replaced a Browse tab and an Installed tab that disagreed. Browse
 * decided "installed" by comparing the collection's display name with the
 * installed file's stem, and since the slug rule those are different strings,
 * so it offered Install on every recipe — including the ones Installed listed.
 * The state now comes from the server (`GET /api/oci/collections/{name}/state`),
 * which matches by identity, and the view only renders it:
 *
 * - not installed → Install;
 * - installed and current → a quiet mark, nothing to press;
 * - update → Update;
 * - local edits → marked; Update only when upstream changed too, and it asks
 *   first, because it overwrites the edit;
 * - removed upstream → marked, nothing to press: uninstalling is the Recipes
 *   page's job, beside everything else that removes a recipe.
 *
 * Update all leaves local edits alone and says how many it left; Install all
 * asks before it writes. Both go through one bulk call that answers per
 * recipe, so a partial failure names what failed.
 */

import { useCallback, useMemo, useState } from "react";
import { Download, RefreshCw } from "lucide-react";
import { applyOciCollection, fetchOciCollectionState } from "@/lib/api";
import type { OciCollection, OciCollectionApplyResult, OciRecipeState, OciRecipeStateName } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { useI18n, type Translator } from "@/lib/i18n";
import { AlertModal, Button, ConfirmModal, ErrorLine, Spinner, Tabs, TONE_TEXT, type StatusTone } from "@/ui";
import ServesChip from "@/components/ServesChip";
import { cn } from "@/lib/utils";

type Filter = "all" | "installed" | "updates" | "not_installed";

const STATE_TONE: Record<OciRecipeStateName, StatusTone> = {
  installed: "good",
  update: "warn",
  local_edits: "warn",
  not_installed: "muted",
  removed: "muted",
};

/** Does this recipe belong under a filter pill? */
export function inFilter(recipe: OciRecipeState, filter: Filter): boolean {
  switch (filter) {
    case "installed":
      return recipe.state !== "not_installed";
    case "updates":
      return recipe.update_available;
    case "not_installed":
      return recipe.state === "not_installed";
    default:
      return true;
  }
}

/** The one action a recipe offers, if any. */
export function actionFor(recipe: OciRecipeState): "install" | "update" | null {
  if (recipe.state === "not_installed") return "install";
  if (recipe.state === "update") return "update";
  if (recipe.state === "local_edits" && recipe.update_available) return "update";
  return null;
}

/** What a bulk apply did, in one short paragraph: the counts, then each
 *  failure by name — a bulk action that reported only "some failed" would
 *  leave the operator to find which. */
export function describeOutcome(result: OciCollectionApplyResult, t: Translator["t"]): string {
  const done = result.results.filter(
    (r) => r.success && r.action !== "skipped_local_edits",
  ).length;
  const skipped = result.results.filter((r) => r.action === "skipped_local_edits").length;
  const failed = result.results.filter((r) => !r.success);
  const lines = [t("collection.outcome", { done, skipped, failed: failed.length })];
  for (const f of failed) lines.push(`${f.recipe}: ${f.error ?? t("common.unknownError")}`);
  return lines.join("\n");
}

export interface CollectionViewProps {
  collection: OciCollection;
  /** Something was installed or updated; the caller's counts are stale. */
  onChanged?: () => void;
}

export default function CollectionView({ collection, onChanged }: CollectionViewProps) {
  const { t } = useI18n();
  const fetchState = useCallback(
    (signal?: AbortSignal) => fetchOciCollectionState(collection.name, collection.registry, signal),
    [collection.name, collection.registry],
  );
  const { data: state, loading, error, refetch } = useQuery(fetchState);

  const [filter, setFilter] = useState<Filter>("all");
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [bulk, setBulk] = useState<"install" | "update" | null>(null);
  const [overwriteTarget, setOverwriteTarget] = useState<OciRecipeState | null>(null);
  const [confirmInstallAll, setConfirmInstallAll] = useState(false);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);

  const recipes = useMemo(() => state?.recipes ?? [], [state]);
  const updates = recipes.filter((r) => r.state === "update");
  const editedBehind = recipes.filter((r) => r.state === "local_edits" && r.update_available);
  const notInstalled = recipes.filter((r) => r.state === "not_installed");
  const shown = recipes.filter((r) => inFilter(r, filter));

  const apply = async (names: string[], overwrite = false) => {
    const result = await applyOciCollection(collection.name, {
      recipes: names,
      version: state?.latest_version,
      registry: state?.registry,
      overwrite_local: overwrite || undefined,
    });
    refetch();
    onChanged?.();
    return result;
  };

  const runOne = async (recipe: OciRecipeState, overwrite = false) => {
    setBusy((prev) => new Set(prev).add(recipe.name));
    try {
      const result = await apply([recipe.name], overwrite);
      const failed = result.results.find((r) => !r.success);
      if (failed) {
        setAlert({ title: t("collection.failed"), message: describeOutcome(result, t) });
      }
    } catch (e) {
      setAlert({
        title: t("collection.failed"),
        message: e instanceof Error ? e.message : t("common.unknownError"),
      });
    } finally {
      setBusy((prev) => {
        const next = new Set(prev);
        next.delete(recipe.name);
        return next;
      });
    }
  };

  const runBulk = async (kind: "install" | "update") => {
    // Update all sends the edited recipes too, without overwrite: the server
    // skips them and says so per recipe, which is what the outcome counts.
    const names =
      kind === "install"
        ? notInstalled.map((r) => r.name)
        : [...updates, ...editedBehind].map((r) => r.name);
    setBulk(kind);
    try {
      const result = await apply(names);
      setAlert({
        title: kind === "install" ? t("collection.installAllDone") : t("collection.updateAllDone"),
        message: describeOutcome(result, t),
      });
    } catch (e) {
      setAlert({
        title: t("collection.failed"),
        message: e instanceof Error ? e.message : t("common.unknownError"),
      });
    } finally {
      setBulk(null);
    }
  };

  const onAction = (recipe: OciRecipeState) => {
    if (recipe.state === "local_edits") setOverwriteTarget(recipe);
    else void runOne(recipe);
  };

  if (loading) {
    return (
      <p className="flex items-center gap-2 px-6 py-6 text-[13px] text-muted">
        <Spinner size="sm" />
        {t("oci.loadingRecipes")}
      </p>
    );
  }
  if (error || !state) {
    return (
      <div className="px-6 py-6">
        <ErrorLine>{error ?? t("common.unknownError")}</ErrorLine>
      </div>
    );
  }

  const behind = state.installed_version && state.installed_version !== state.latest_version;

  return (
    <div className="space-y-5 px-6 py-5" data-testid="collection-view">
      <div className="space-y-3">
        <p className="font-mono text-[13px]" data-testid="collection-versions">
          {behind ? (
            <>
              <span className="text-muted">{state.installed_version}</span>
              {" → "}
              <span className="text-warn">{state.latest_version}</span>{" "}
              <span className="text-muted font-sans">{t("collection.available")}</span>
            </>
          ) : (
            <span className="text-muted">{state.latest_version}</span>
          )}
        </p>
        {(updates.length > 0 || notInstalled.length > 0) && (
          <div className="flex flex-wrap gap-2">
            {updates.length > 0 && (
              <Button
                size="sm"
                variant="primary"
                icon={RefreshCw}
                loading={bulk === "update"}
                disabled={bulk !== null}
                onClick={() => void runBulk("update")}
              >
                {t("collection.updateAll", { count: updates.length })}
              </Button>
            )}
            {notInstalled.length > 0 && (
              <Button
                size="sm"
                icon={Download}
                loading={bulk === "install"}
                disabled={bulk !== null}
                onClick={() => setConfirmInstallAll(true)}
              >
                {t("collection.installAll", { count: notInstalled.length })}
              </Button>
            )}
          </div>
        )}
        {editedBehind.length > 0 && (
          <p className="text-[13px] text-muted">
            {t("collection.editsKept", { count: editedBehind.length })}
          </p>
        )}
        {!state.checked && <p className="text-[13px] text-warn">{t("collection.notChecked")}</p>}
      </div>

      <Tabs
        label={t("collection.filter")}
        value={filter}
        onChange={(id) => setFilter(id as Filter)}
        tabs={[
          { id: "all", label: t("collection.filterAll"), count: recipes.length },
          { id: "installed", label: t("collection.filterInstalled"), count: recipes.length - notInstalled.length },
          { id: "updates", label: t("collection.filterUpdates"), count: updates.length + editedBehind.length },
          { id: "not_installed", label: t("collection.filterNotInstalled"), count: notInstalled.length },
        ]}
      />

      {shown.length === 0 ? (
        <p className="py-4 text-center text-muted text-[14px]">
          {recipes.length === 0 ? t("oci.noRecipesInCollection") : t("collection.noneHere")}
        </p>
      ) : (
        <ul className="divide-y divide-line border-y border-line">
          {shown.map((recipe) => (
            <RecipeRow
              key={recipe.recipe_id}
              recipe={recipe}
              busy={busy.has(recipe.name) || bulk !== null}
              onAction={() => onAction(recipe)}
            />
          ))}
        </ul>
      )}

      {overwriteTarget && (
        <ConfirmModal
          open
          onClose={() => setOverwriteTarget(null)}
          onConfirm={async () => {
            const recipe = overwriteTarget;
            setOverwriteTarget(null);
            await runOne(recipe, true);
          }}
          title={t("collection.overwriteTitle")}
          message={t("collection.overwriteConfirm", { name: overwriteTarget.name })}
          confirmLabel={t("oci.update")}
          confirmVariant="danger"
        />
      )}

      {confirmInstallAll && (
        <ConfirmModal
          open
          onClose={() => setConfirmInstallAll(false)}
          onConfirm={async () => {
            setConfirmInstallAll(false);
            await runBulk("install");
          }}
          title={t("collection.installAllTitle")}
          message={t("collection.installAllConfirm", {
            count: notInstalled.length,
            name: state.collection,
            version: state.latest_version,
          })}
          confirmLabel={t("oci.install")}
        />
      )}

      {alert && (
        <AlertModal open title={alert.title} message={alert.message} onClose={() => setAlert(null)} />
      )}
    </div>
  );
}

const STATE_LABEL: Record<OciRecipeStateName, string> = {
  installed: "collection.stateInstalled",
  update: "collection.stateUpdate",
  local_edits: "collection.stateLocalEdits",
  not_installed: "collection.stateNotInstalled",
  removed: "collection.stateRemoved",
};

function RecipeRow({
  recipe,
  busy,
  onAction,
}: {
  recipe: OciRecipeState;
  busy: boolean;
  onAction: () => void;
}) {
  const { t } = useI18n();
  const action = actionFor(recipe);
  const tone = STATE_TONE[recipe.state];
  return (
    <li
      data-testid={`collection-recipe-${recipe.recipe_id}`}
      data-state={recipe.state}
      className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3"
    >
      <div className="min-w-0 flex-1 basis-[220px]">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono font-semibold text-[14px] break-all">{recipe.name}</span>
          <ServesChip serves={recipe.serves} />
        </div>
        {recipe.state === "removed" ? (
          <p className="text-[13px] text-muted mt-0.5">{t("collection.removedHint")}</p>
        ) : (
          recipe.description && <p className="text-[13px] text-muted mt-0.5">{recipe.description}</p>
        )}
      </div>
      <div className="flex items-center gap-3">
        <span className={cn("inline-flex items-center gap-1.5 text-[13px]", TONE_TEXT[tone])}>
          <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-current" />
          {t(STATE_LABEL[recipe.state])}
          {recipe.state === "local_edits" && recipe.update_available && (
            <span className="text-muted">· {t("collection.stateUpdate")}</span>
          )}
        </span>
        {action && (
          <Button
            size="sm"
            variant={action === "install" ? "primary" : "ghost"}
            icon={action === "install" ? Download : RefreshCw}
            loading={busy}
            disabled={busy}
            onClick={onAction}
          >
            {action === "install" ? t("oci.install") : t("oci.update")}
          </Button>
        )}
      </div>
    </li>
  );
}
