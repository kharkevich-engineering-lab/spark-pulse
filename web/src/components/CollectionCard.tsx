/** Collection card — one OCI recipe collection, opening its collection view.
 *
 * The card installs nothing itself: what to install, update or leave alone is
 * decided per recipe in the view it opens, where each one's state is shown. It
 * says how many are installed, because that is the one fact a list of
 * collections needs to tell the one in use from the ones that are not. */

import { Package } from "lucide-react";
import BaseCard from "./BaseCard";
import type { OciCollection } from "@/lib/types";
import { useI18n } from "@/lib/i18n";

export default function CollectionCard({
  collection,
  installedCount = 0,
  onView,
}: {
  collection: OciCollection;
  /** Recipes installed from this collection, counted from the sidecars. */
  installedCount?: number;
  onView: () => void;
}) {
  const { t, plural } = useI18n();
  return (
    <BaseCard
      icon={
        <div className="flex items-center gap-2 shrink-0">
          <Package size={16} className="text-blue2" />
          <span className="text-xs text-text-muted font-mono">{collection.display_version}</span>
        </div>
      }
      title={collection.name}
      description={collection.description || t("oci.noDescription")}
      badges={
        <>
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted">
            {plural("oci.recipeCount", collection.recipe_count)}
          </span>
          {installedCount > 0 && (
            <span
              data-testid="collection-installed-count"
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-good"
            >
              {plural("collection.installedCount", installedCount)}
            </span>
          )}
          {collection.vendor && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted">
              {collection.vendor}
            </span>
          )}
          {collection.license && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted">
              {collection.license}
            </span>
          )}
        </>
      }
      onClick={onView}
    />
  );
}
