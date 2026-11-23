/** Engines: what this cluster can run, and where engines come from.
 *
 * The catalogue is every engine image with its standing on the nodes; the
 * sources are the indexes that publish them. One primary action follows the
 * section: *Pull image* on the catalogue, *Add index* on the sources.
 */

import { useMemo, useState } from "react";
import { ArrowDownToLine, Plus } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { fetchEngines, fetchImages } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { formatSize } from "@/lib/utils";
import LibraryKindPage from "@/components/library/LibraryKindPage";
import EnginesTab from "@/components/library/EnginesTab";
import EngineSources from "@/components/library/EngineSources";

export default function EnginesPage() {
  const { t } = useI18n();
  const images = useQuery(fetchImages);
  const engines = useQuery(fetchEngines);
  const [pullOpen, setPullOpen] = useState(false);
  const [addTick, setAddTick] = useState(0);

  const summary = useMemo(() => {
    const all = images.data ?? [];
    const pulled = all.filter((i) => i.present);
    return t("library.enginesSummary", {
      pulled: pulled.length,
      total: all.length,
      size: formatSize(pulled.reduce((sum, i) => sum + i.size_bytes, 0)),
    });
  }, [images.data, t]);

  return (
    <LibraryKindPage
      title={t("library.headingEngines")}
      description={images.data ? summary : undefined}
      action={{ label: t("engines.pullImage"), icon: ArrowDownToLine, onClick: () => setPullOpen(true) }}
      sourcesAction={{ label: t("engines.addIndex"), icon: Plus, onClick: () => setAddTick((n) => n + 1) }}
      sourcesCount={engines.data?.indexes?.length}
      catalogue={<EnginesTab images={images} pullOpen={pullOpen} onPullOpenChange={setPullOpen} />}
      sources={<EngineSources addTick={addTick} />}
    />
  );
}
