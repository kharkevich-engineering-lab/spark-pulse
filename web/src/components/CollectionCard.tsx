/** Collection card — displays an OCI recipe collection with install/view actions. */

import { Package } from "lucide-react";
import BaseCard from "./BaseCard";
import type { OciCollection } from "@/lib/types";
import { useI18n } from "@/lib/i18n";

export default function CollectionCard({
  collection,
  onView,
}: {
  collection: OciCollection;
  installed: boolean;
  onView: () => void;
  onInstall: () => void;
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
