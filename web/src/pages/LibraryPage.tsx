/** Library: everything this control plane keeps on disk.
 *
 * Four pages answered one question. Models listed the snapshots and linked to
 * Cache, which listed the directories those snapshots live in; Engines listed
 * the images; OCI Registry listed the recipe collections. An operator looking
 * for a hundred gigabytes read four navigation entries to find out which of
 * them was holding it.
 *
 * One page component behind three sidebar entries, the caches a section of
 * the first — a cache is not a destination, it is a line item under the
 * catalogue it belongs to. Each address has its own title and one primary
 * action in the header, opening the dialog its tab owns. The old addresses all still work: `/engines` and `/oci` open their tab,
 * `/cache` opens Models at the caches section, and every bookmark, MCP tool
 * and Playwright spec that named them still arrives somewhere true.
 */

import { useMemo, useState } from "react";
import { ArrowDownToLine, Download, Plus } from "lucide-react";
import { useLocation } from "react-router-dom";
import { useI18n } from "@/lib/i18n";
import { fetchCache, fetchImages, fetchModels, fetchOciRegistries } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { formatSize } from "@/lib/utils";
import { Button, PageHeader } from "@/ui";
import ModelsTab from "@/components/library/ModelsTab";
import EnginesTab from "@/components/library/EnginesTab";
import RegistriesTab from "@/components/library/RegistriesTab";

export type LibraryTab = "models" | "engines" | "registries";


/** The page title per address: the sidebar names each one, so the page does
 *  too, rather than one title over three tabs the sidebar already lists. */
const TITLE_FOR: Record<LibraryTab, string> = {
  models: "models.heading",
  engines: "library.headingEngines",
  registries: "collection.pageHeading",
};

/** The one primary action per address, in the page header. */
const ACTION_FOR: Record<LibraryTab, { label: string; icon: typeof Download }> = {
  models: { label: "models.downloadModel", icon: Download },
  engines: { label: "engines.pullImage", icon: ArrowDownToLine },
  registries: { label: "oci.addRegistryButton", icon: Plus },
};

export function tabForPath(pathname: string): LibraryTab {
  if (pathname === "/engines") return "engines";
  if (pathname === "/oci") return "registries";
  return "models";
}

export default function LibraryPage() {
  const { t } = useI18n();
  const location = useLocation();

  const models = useQuery(fetchModels);
  const cache = useQuery(fetchCache);
  const images = useQuery(fetchImages);
  const registries = useQuery(fetchOciRegistries);

  const tab = tabForPath(location.pathname);
  // Which tab's primary dialog is open. Keyed by tab so changing address
  // never leaves a dialog open over a page it does not belong to.
  const [actionFor, setActionFor] = useState<LibraryTab | null>(null);
  const actionOpen = actionFor === tab;
  const setActionOpen = (open: boolean) => setActionFor(open ? tab : null);
  const action = ACTION_FOR[tab];
  const atCaches = location.pathname === "/cache";

  const modelBytes = useMemo(
    () => (models.data ?? []).reduce((sum, m) => sum + m.size_bytes, 0),
    [models.data],
  );
  const imageBytes = useMemo(
    () => (images.data ?? []).reduce((sum, i) => sum + (i.present ? i.size_bytes : 0), 0),
    [images.data],
  );
  // Every node that answered, summed. A node that could not be asked is not a
  // zero — it is unknown — so it contributes nothing here and says so in its
  // own section rather than dragging the headline figure down silently.
  const cacheBytes = useMemo(
    () =>
      (cache.data?.nodes ?? [])
        .filter((node) => node.reachable)
        .reduce((sum, node) => sum + node.total_bytes, 0),
    [cache.data],
  );

  return (
    <div>
      <PageHeader
        eyebrow={t("nav.library")}
        title={t(TITLE_FOR[tab])}
        description={
          tab === "models"
            ? t("library.onDisk", {
                total: formatSize(modelBytes + imageBytes + cacheBytes),
                cache: formatSize(cacheBytes),
              })
            : undefined
        }
        actions={
          <Button variant="primary" icon={action.icon} onClick={() => setActionOpen(true)}>
            {t(action.label)}
          </Button>
        }
      />

      {/* Only the tab on screen is mounted: each one opens streams and asks
          every node questions, and three of those running at once for two
          nobody is reading is how a page comes to take a second to settle. */}
      {tab === "models" && (
        <ModelsTab
          models={models}
          cache={cache}
          scrollToCaches={atCaches}
          downloadOpen={actionOpen}
          onDownloadOpenChange={setActionOpen}
        />
      )}
      {tab === "engines" && (
        <EnginesTab images={images} pullOpen={actionOpen} onPullOpenChange={setActionOpen} />
      )}
      {tab === "registries" && (
        <RegistriesTab registries={registries} addOpen={actionOpen} onAddOpenChange={setActionOpen} />
      )}

    </div>
  );
}
