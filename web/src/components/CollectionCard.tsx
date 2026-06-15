/** Collection card — displays an OCI recipe collection with install/view actions. */

import { Package } from "lucide-react";
import BaseCard from "./BaseCard";
import type { OciCollection } from "@/lib/types";

export default function CollectionCard({
  collection,
  onView,
}: {
  collection: OciCollection;
  installed: boolean;
  onView: () => void;
  onInstall: () => void;
}) {
  return (
    <BaseCard
      icon={
        <div className="flex items-center gap-2 shrink-0">
          <Package size={16} className="text-primary" />
          <span className="text-xs text-text-muted font-mono">{collection.version}</span>
        </div>
      }
      title={collection.name}
      description={collection.description || "No description available"}
      badges={
        <>
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted">
            {collection.recipe_count} recipes
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
