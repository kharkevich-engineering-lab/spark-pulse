/** Library: everything this control plane keeps on disk.
 *
 * Four pages answered one question. Models listed the snapshots and linked to
 * Cache, which listed the directories those snapshots live in; Engines listed
 * the images; OCI Registry listed the recipe collections. An operator looking
 * for a hundred gigabytes read four navigation entries to find out which of
 * them was holding it.
 *
 * One page, three tabs, and the caches as a section of the first — because a
 * cache is not a destination, it is a line item under the catalogue it belongs
 * to. The old addresses all still work: `/engines` and `/oci` open their tab,
 * `/cache` opens Models at the caches section, and every bookmark, MCP tool
 * and Playwright spec that named them still arrives somewhere true.
 */

import { useCallback, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useI18n } from "@/lib/i18n";
import { cleanCache, fetchCache, fetchImages, fetchModels, fetchOciRegistries } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { formatSize } from "@/lib/utils";
import { AlertModal, Button, ConfirmModal, PageHeader, Tabs } from "@/ui";
import ModelsTab from "@/components/library/ModelsTab";
import EnginesTab from "@/components/library/EnginesTab";
import RegistriesTab from "@/components/library/RegistriesTab";

export type LibraryTab = "models" | "engines" | "registries";

/** Which tab an address opens, and which address a tab restores.
 *
 * The paths are the ones that already existed: a tab that changed the URL to
 * something new would break every bookmark this merge was supposed to keep.
 */
const PATH_FOR: Record<LibraryTab, string> = {
  models: "/models",
  engines: "/engines",
  registries: "/oci",
};

export function tabForPath(pathname: string): LibraryTab {
  if (pathname === "/engines") return "engines";
  if (pathname === "/oci") return "registries";
  return "models";
}

export default function LibraryPage() {
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();

  const models = useQuery(fetchModels);
  const cache = useQuery(fetchCache);
  const images = useQuery(fetchImages);
  const registries = useQuery(fetchOciRegistries);

  const [cleaningAll, setCleaningAll] = useState(false);
  const [confirmClean, setConfirmClean] = useState(false);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);

  const tab = tabForPath(location.pathname);
  const atCaches = location.pathname === "/cache";

  const modelBytes = useMemo(
    () => (models.data ?? []).reduce((sum, m) => sum + m.size_bytes, 0),
    [models.data],
  );
  const imageBytes = useMemo(
    () => (images.data ?? []).reduce((sum, i) => sum + (i.present ? i.size_bytes : 0), 0),
    [images.data],
  );
  const cacheBytes = useMemo(
    () => (cache.data?.entries ?? []).reduce((sum, e) => sum + e.size_bytes, 0),
    [cache.data],
  );

  const select = useCallback(
    (next: string) => navigate(PATH_FOR[next as LibraryTab]),
    [navigate],
  );

  const cleanAll = async () => {
    setCleaningAll(true);
    try {
      await cleanCache(["all"]);
      cache.refetch();
    } catch (e) {
      setAlert({
        title: t("common.error"),
        message: e instanceof Error ? e.message : t("cache.failed"),
      });
    } finally {
      setCleaningAll(false);
    }
  };

  return (
    <div className="space-y-8">
      <PageHeader
        eyebrow={t("nav.library")}
        title={t("models.heading")}
        description={t("library.onDisk", {
          total: formatSize(modelBytes + imageBytes + cacheBytes),
          cache: formatSize(cacheBytes),
        })}
        actions={
          <Button loading={cleaningAll} onClick={() => setConfirmClean(true)}>
            {t("library.cleanCaches")}
          </Button>
        }
      />

      <Tabs
        label={t("nav.library")}
        value={tab}
        onChange={select}
        tabs={[
          { id: "models", label: t("library.tabModels"), count: models.data?.length },
          { id: "engines", label: t("library.tabEngines"), count: images.data?.length },
          { id: "registries", label: t("library.tabRegistries"), count: registries.data?.length },
        ]}
      />

      {/* Only the tab on screen is mounted: each one opens streams and asks
          every node questions, and three of those running at once for two
          nobody is reading is how a page comes to take a second to settle. */}
      {tab === "models" && <ModelsTab models={models} cache={cache} scrollToCaches={atCaches} />}
      {tab === "engines" && <EnginesTab images={images} />}
      {tab === "registries" && <RegistriesTab registries={registries} />}

      {confirmClean && (
        <ConfirmModal
          open
          onClose={() => setConfirmClean(false)}
          onConfirm={() => {
            setConfirmClean(false);
            cleanAll();
          }}
          title={t("cache.confirmAllTitle")}
          message={t("cache.confirmAllBody")}
          confirmLabel={t("cache.clean")}
          confirmVariant="danger"
        />
      )}

      {alert && (
        <AlertModal
          open
          onClose={() => setAlert(null)}
          title={alert.title}
          message={alert.message}
        />
      )}
    </div>
  );
}
