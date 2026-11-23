/** Models: what is cached on the nodes, and where models come from.
 *
 * `/cache` is still a bookmark: it opens the catalogue scrolled to the caches.
 * One primary action follows the section: *Download model* on the catalogue,
 * *Add source* on the sources.
 */

import { useMemo, useState } from "react";
import { Download, Plus } from "lucide-react";
import { useLocation } from "react-router-dom";
import { useI18n } from "@/lib/i18n";
import { fetchCache, fetchImages, fetchModelSources, fetchModels } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { formatSize } from "@/lib/utils";
import LibraryKindPage from "@/components/library/LibraryKindPage";
import ModelsTab from "@/components/library/ModelsTab";
import ModelSources from "@/components/library/ModelSources";

export default function ModelsPage() {
  const { t } = useI18n();
  const location = useLocation();
  const models = useQuery(fetchModels);
  const cache = useQuery(fetchCache);
  const images = useQuery(fetchImages);
  const sources = useQuery(fetchModelSources);
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [addTick, setAddTick] = useState(0);

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
    <LibraryKindPage
      title={t("models.heading")}
      description={t("library.onDisk", {
        total: formatSize(modelBytes + imageBytes + cacheBytes),
        cache: formatSize(cacheBytes),
      })}
      action={{ label: t("models.downloadModel"), icon: Download, onClick: () => setDownloadOpen(true) }}
      sourcesAction={{ label: t("models.addSource"), icon: Plus, onClick: () => setAddTick((n) => n + 1) }}
      sourcesCount={sources.data?.length}
      catalogue={
        <ModelsTab
          models={models}
          cache={cache}
          scrollToCaches={location.pathname === "/cache"}
          downloadOpen={downloadOpen}
          onDownloadOpenChange={setDownloadOpen}
        />
      }
      sources={<ModelSources addTick={addTick} />}
    />
  );
}
