/** Recipes as a console lists them: one row each, the deciding facts in columns.
 *
 * What decides a deploy — which engines run it, one node or several, what it
 * serves, whether something is in its way — is a column, so twenty recipes can
 * be compared at a glance. The state column says only what is *not* ordinary:
 * a recipe that simply runs here leaves it blank rather than repeating "runs
 * here" down the whole table. The description lives in the drawer, where it
 * can be read whole instead of cut off mid-sentence. Under 1200px the page
 * keeps `RecipeCard`.
 */

import { Download, RefreshCw, RotateCcw, Rocket } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { Button, IconButton, Table, THead, Th, Tr, Td, EmptyRow } from "@/ui";
import { LibraryStateChip, type LibraryState } from "@/components/library/LibraryState";
import ServesChip from "./ServesChip";
import type { RecipeSummary } from "@/lib/types";

/** Where a row stands in the library, and the one thing it offers besides
 *  being opened. A row a collection offers and nothing holds (`offered`)
 *  cannot be opened or deployed: it is installed first. */
export interface RecipeLibrary {
  state: LibraryState;
  offered?: boolean;
  action?: "install" | "update";
  busy?: boolean;
  onAction?: () => void;
}

function usableEngines(r: RecipeSummary): string[] {
  if (r.engine_support?.length) return r.engine_support.filter((e) => e.supported && e.enabled).map((e) => e.engine);
  return r.engines ?? [];
}

export interface RecipeRowState {
  running?: boolean;
  queued?: boolean;
  clusterBlocked?: boolean;
  notHere?: string;
  tight?: string;
}

export interface RecipesTableProps {
  recipes: RecipeSummary[];
  stateOf: (r: RecipeSummary) => RecipeRowState;
  onSelect: (r: RecipeSummary) => void;
  onReset: (r: RecipeSummary) => void;
  /** The library state and action of a row; absent, the table is a plain list. */
  libraryOf?: (r: RecipeSummary) => RecipeLibrary | undefined;
  /** Shown as a single row when the list (after search) is empty. */
  empty?: React.ReactNode;
  testId?: string;
}

function StateCell({ state }: { state: RecipeRowState }) {
  const { t } = useI18n();
  if (state.running)
    return (
      <span className="inline-flex items-center gap-1.5 font-medium text-success">
        <span className="h-1.5 w-1.5 rounded-full bg-success" />
        {t("recipeCard.running")}
      </span>
    );
  if (state.queued) return <span className="font-medium text-blue2">{t("recipeCard.queued")}</span>;
  if (state.clusterBlocked) return <span className="text-muted">{t("recipeCard.clusterOnly")}</span>;
  if (state.notHere)
    return (
      <span className="text-muted" title={state.notHere}>
        {t("recipeCard.notHere", { reason: state.notHere })}
      </span>
    );
  if (state.tight)
    return (
      <span className="text-warn" title={state.tight}>
        {t("recipeCard.tight")}
      </span>
    );
  return null;
}

export default function RecipesTable({ recipes, stateOf, onSelect, onReset, libraryOf, empty, testId = "recipes-table" }: RecipesTableProps) {
  const { t } = useI18n();

  return (
    <Table data-testid={testId}>
      <THead>
        <Th>{t("recipesTable.colRecipe")}</Th>
        <Th>{t("recipesTable.colEngines")}</Th>
        <Th>{t("recipesTable.colNodes")}</Th>
        <Th>{t("recipesTable.colServes")}</Th>
        <Th>{t("recipesTable.colState")}</Th>
        <Th srOnly>{t("runs.actions")}</Th>
      </THead>
      <tbody>
        {recipes.length === 0 && empty && <EmptyRow colSpan={6}>{empty}</EmptyRow>}
        {recipes.map((r) => {
          const state = stateOf(r);
          const engines = usableEngines(r);
          const solo = r.solo_only || !r.cluster_only;
          const cluster = r.cluster_only || !r.solo_only;
          const library = libraryOf?.(r);
          const offered = Boolean(library?.offered);
          const blocked = Boolean(state.clusterBlocked) && !offered;
          const open = blocked || offered ? undefined : () => onSelect(r);
          return (
            <Tr
              key={r.id}
              data-testid={`recipe-row-${r.id}`}
              interactive={Boolean(open)}
              muted={blocked || Boolean(state.notHere)}
              onClick={open}
            >
              <Td className="max-w-[420px]">
                <div className="flex min-w-0 items-center gap-2">
                  {offered ? (
                    <span className="truncate font-semibold">{r.name}</span>
                  ) : (
                    <button
                      type="button"
                      disabled={blocked}
                      onClick={(e) => {
                        e.stopPropagation();
                        onSelect(r);
                      }}
                      className="truncate text-left font-semibold hover:text-blue2 disabled:hover:text-text"
                    >
                      {r.name}
                    </button>
                  )}
                  {r.is_customized && <span className="shrink-0 text-[11.5px] text-amber">{t("recipeCard.custom")}</span>}
                </div>
                <p className="truncate font-mono text-[11.5px] text-muted" title={r.model}>
                  {r.model}
                </p>
              </Td>
              <Td mono muted className="whitespace-nowrap">
                {engines.length ? engines.join(" · ") : "—"}
              </Td>
              <Td muted className="whitespace-nowrap">
                {solo && cluster
                  ? t("recipesTable.soloOrCluster")
                  : solo
                    ? t("recipeCard.solo")
                    : t("recipeCard.cluster")}
              </Td>
              <Td>
                <ServesChip serves={r.serves} />
              </Td>
              <Td className="text-[12.5px]">
                {library && <LibraryStateChip state={library.state} heldAs="installed" />}
                <StateCell state={state} />
              </Td>
              <Td actions>
                {r.is_customized && (
                  <IconButton size="sm" icon={RotateCcw} label={t("recipeCard.resetToOriginal")} onClick={() => onReset(r)} />
                )}
                {library?.action ? (
                  <Button
                    size="sm"
                    variant={library.action === "install" ? "primary" : undefined}
                    icon={library.action === "install" ? Download : RefreshCw}
                    loading={library.busy}
                    disabled={library.busy}
                    aria-label={t(library.action === "install" ? "recipesTable.installFor" : "recipesTable.updateFor", {
                      name: r.name,
                    })}
                    onClick={library.onAction}
                  >
                    {t(library.action === "install" ? "oci.install" : "oci.update")}
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    icon={Rocket}
                    disabled={blocked}
                    aria-label={t("recipesTable.deployFor", { name: r.name })}
                    onClick={() => onSelect(r)}
                  >
                    {t("recipes.deploy")}
                  </Button>
                )}
              </Td>
            </Tr>
          );
        })}
      </tbody>
    </Table>
  );
}
