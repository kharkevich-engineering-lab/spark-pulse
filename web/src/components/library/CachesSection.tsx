/** The cache directories, per node, under the catalogue they belong to.
 *
 * This was a page of its own, then a section listing four directories. Both
 * were about *one* machine while saying nothing about which: the control plane
 * walked its own `~/.cache` and the page presented the answer as the cluster's.
 * Every node is asked through its own agent now, so the section is a list of
 * machines — a heading, a total, and the four caches under it — the same rhythm
 * Fleet's monitoring uses, because it is the same list of machines.
 *
 * A node that could not be asked keeps its heading and says why. Dropping it
 * would make a cluster look smaller than it is, and showing it empty would make
 * a full disk look free.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Database } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { cleanAllCaches, cleanCache } from "@/lib/api";
import type { UseQueryResult } from "@/hooks/useQuery";
import { formatSize } from "@/lib/utils";
import { AlertModal, Button, ConfirmModal, EmptyState, ErrorLine, NodeState, Spinner } from "@/ui";
import type { CacheEntry, CacheNode, CacheResponse } from "@/lib/types";

export interface CachesSectionProps {
  cache: UseQueryResult<CacheResponse>;
  /** `/cache` used to be a page; arriving at that address scrolls here. */
  scrollTo?: boolean;
}

/** What the confirm dialog is about: one cache on a node, or all of them. */
type Target = { node: CacheNode; entry: CacheEntry | null };

export default function CachesSection({ cache, scrollTo }: CachesSectionProps) {
  const { t } = useI18n();
  const { data, loading, error, refetch } = cache;
  const [cleaning, setCleaning] = useState<string | null>(null);
  const [target, setTarget] = useState<Target | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);
  const anchor = useRef<HTMLElement>(null);

  useEffect(() => {
    if (scrollTo && data) anchor.current?.scrollIntoView({ block: "start" });
  }, [scrollTo, data]);

  const nodes = useMemo(() => data?.nodes ?? [], [data]);

  const doClean = async ({ node, entry }: Target) => {
    const key = cleanKey(node, entry);
    setCleaning(key);
    try {
      const result = entry
        ? await cleanCache(node.node_id, entry.name)
        : await cleanAllCaches(node.node_id);
      if (!result.reachable) {
        setAlert({
          title: t("cache.failed"),
          message: result.reason ?? t("cache.failed"),
        });
      } else {
        const refused = result.results.find((one) => one.error);
        if (refused) setAlert({ title: t("cache.failed"), message: refused.error! });
      }
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

      {loading && !data && (
        <div className="flex justify-center py-12">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      <ErrorLine className="mt-3">{error}</ErrorLine>

      {nodes.map((node) => (
        <NodeCaches
          key={node.node_id || node.address}
          node={node}
          cleaning={cleaning}
          onClean={(entry) => setTarget({ node, entry })}
        />
      ))}

      {data && nodes.length === 0 && !loading && (
        <EmptyState icon={Database}>{t("cache.empty")}</EmptyState>
      )}

      {target && (
        <ConfirmModal
          open
          onClose={() => setTarget(null)}
          onConfirm={() => {
            const pending = target;
            setTarget(null);
            doClean(pending);
          }}
          title={target.entry ? t("cache.confirmOneTitle") : t("library.cleanNodeTitle")}
          message={
            target.entry
              ? t("cache.confirmOneBody", {
                  name: target.entry.name,
                  node: nodeLabel(target.node, t),
                })
              : t("library.cleanNodeBody", { node: nodeLabel(target.node, t) })
          }
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

/** One machine's caches. Separated by a rule, like Fleet's node sections. */
function NodeCaches({
  node,
  cleaning,
  onClean,
}: {
  node: CacheNode;
  cleaning: string | null;
  onClean: (entry: CacheEntry | null) => void;
}) {
  const { t, plural } = useI18n();
  const label = nodeLabel(node, t);

  return (
    <section
      data-testid={`cache-node-${node.node_id || node.address}`}
      className="mt-8 first:mt-5"
    >
      <div className="flex items-center gap-3 flex-wrap">
        <h3 className="text-[17px] font-semibold tracking-[-0.02em]">{label}</h3>
        {node.reachable ? (
          <NodeState state="ok" dotOnly />
        ) : (
          <NodeState
            state="unknown"
            label={t("library.cacheUnreachable")}
            title={node.reason ?? undefined}
          />
        )}
        <span className="ml-auto flex items-center gap-3">
          {node.reachable && (
            <span className="font-mono text-[13px]">{formatSize(node.total_bytes)}</span>
          )}
          {node.reachable && (
            <Button
              size="sm"
              loading={cleaning === cleanKey(node, null)}
              onClick={() => onClean(null)}
            >
              {t("library.cleanNode")}
            </Button>
          )}
        </span>
      </div>

      {!node.reachable ? (
        // Unknown, not empty: the node did not answer, and a section showing
        // four zeroes would say this machine is holding nothing.
        <p className="text-[13px] text-muted mt-2">{node.reason}</p>
      ) : (
        <div className="mt-4 grid grid-cols-1 gap-4 min-[600px]:grid-cols-2 min-[1000px]:grid-cols-4">
          {node.dirs.map((entry) => (
            <div
              key={entry.name}
              data-testid={`cache-${node.node_id || node.address}-${entry.name}`}
              className="rounded-md bg-surface border border-line p-4 flex flex-col gap-2"
            >
              <h4 className="text-[15px] font-semibold">{entry.name}</h4>
              <p className="font-mono text-[12.5px] text-muted break-all">{entry.path}</p>
              {entry.error && <p className="text-[12.5px] text-warn">{entry.error}</p>}
              <div className="mt-auto flex items-center justify-between gap-2 pt-2">
                <span className="font-mono text-[13px]">
                  {entry.truncated ? t("cache.atLeast", { size: formatSize(entry.size_bytes) }) : formatSize(entry.size_bytes)}
                  <span className="text-muted"> · {plural("cache.files", entry.file_count)}</span>
                </span>
                <button
                  type="button"
                  onClick={() => onClean(entry)}
                  disabled={cleaning === cleanKey(node, entry)}
                  aria-label={t("library.cleanOne", { name: entry.name, node: label })}
                  className="text-[13px] text-blue2 hover:underline disabled:opacity-[0.55]"
                >
                  {t("cache.clean")}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/** How a node is named on the page: its name, then its address, then a word. */
function nodeLabel(node: CacheNode, t: (key: string) => string): string {
  return node.name || node.address || t("library.thisNode");
}

/** What is in flight: one cache on one node, or a whole node. */
function cleanKey(node: CacheNode, entry: CacheEntry | null): string {
  return `${node.node_id}:${entry?.name ?? "*"}`;
}
