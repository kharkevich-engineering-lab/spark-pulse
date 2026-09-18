/** The cache directories, under the catalogue they belong to.
 *
 * This was a page of its own, reached from a link in the Models subtitle. It
 * answers the same question the table above it does — what is using the disk —
 * so it is a section of that page rather than a destination: a rule, a
 * heading, and one card per directory.
 */

import { useEffect, useRef, useState } from "react";
import { Database } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { cleanCache } from "@/lib/api";
import type { UseQueryResult } from "@/hooks/useQuery";
import { formatSize } from "@/lib/utils";
import { AlertModal, ConfirmModal, EmptyState, ErrorLine, Spinner } from "@/ui";
import type { CacheEntry } from "@/lib/types";

export interface CachesSectionProps {
  cache: UseQueryResult<{ entries: CacheEntry[] }>;
  /** `/cache` used to be a page; arriving at that address scrolls here. */
  scrollTo?: boolean;
}

export default function CachesSection({ cache, scrollTo }: CachesSectionProps) {
  const { t, plural } = useI18n();
  const { data, loading, error, refetch } = cache;
  const [cleaning, setCleaning] = useState<string | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);
  const anchor = useRef<HTMLElement>(null);

  useEffect(() => {
    if (scrollTo && data) anchor.current?.scrollIntoView({ block: "start" });
  }, [scrollTo, data]);

  const doClean = async (name: string) => {
    setCleaning(name);
    try {
      await cleanCache([name]);
      refetch();
    } catch (e) {
      setAlert({
        title: t("common.error"),
        message: e instanceof Error ? e.message : t("cache.failed"),
      });
    } finally {
      setCleaning(null);
    }
  };

  return (
    <section ref={anchor} id="caches" className="border-t border-line pt-12 mt-10 scroll-mt-20">
      <h2 className="text-[22px] font-bold tracking-[-0.02em]">{t("library.caches")}</h2>
      <p className="text-[13px] text-muted mt-1">{t("library.cachesHint")}</p>

      {loading && (
        <div className="flex justify-center py-12">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      <ErrorLine className="mt-3">{error}</ErrorLine>

      <div className="mt-5 grid grid-cols-1 gap-4 min-[600px]:grid-cols-2 min-[1000px]:grid-cols-4">
        {data?.entries.map((entry) => (
          <div
            key={entry.name}
            data-testid={`cache-${entry.name}`}
            className="rounded-md bg-surface border border-line p-4 flex flex-col gap-2"
          >
            <h3 className="text-[15px] font-semibold">{entry.name}</h3>
            <p className="font-mono text-[12.5px] text-muted break-all">{entry.path}</p>
            <div className="mt-auto flex items-center justify-between gap-2 pt-2">
              <span className="font-mono text-[13px]">
                {formatSize(entry.size_bytes)}
                <span className="text-muted"> · {plural("cache.files", entry.file_count)}</span>
              </span>
              <button
                type="button"
                onClick={() => setTarget(entry.name)}
                disabled={cleaning === entry.name}
                aria-label={t("library.cleanOne", { name: entry.name })}
                className="text-[13px] text-blue2 hover:underline disabled:opacity-[0.55]"
              >
                {t("cache.clean")}
              </button>
            </div>
          </div>
        ))}
      </div>

      {data && data.entries.length === 0 && !loading && (
        <EmptyState icon={Database}>{t("cache.empty")}</EmptyState>
      )}

      {target && (
        <ConfirmModal
          open
          onClose={() => setTarget(null)}
          onConfirm={() => {
            const name = target;
            setTarget(null);
            doClean(name);
          }}
          title={t("cache.confirmOneTitle")}
          message={t("cache.confirmOneBody", { name: target })}
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
    </section>
  );
}
