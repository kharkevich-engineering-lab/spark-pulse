/** The cache directories, per node, under the catalogue they belong to.
 *
 * This was a page of its own, then a section listing four directories. Both
 * were about *one* machine while saying nothing about which: the control plane
 * walked its own `~/.cache` and the page presented the answer as the cluster's.
 * Every node is asked through its own agent now, so the section is one table
 * grouped by machine — a header row with its total and "clean all", then a
 * row per directory — rather than a grid of cards whose paths wrapped.
 *
 * A node that could not be asked keeps its heading and says why. Dropping it
 * would make a cluster look smaller than it is, and showing it empty would make
 * a full disk look free.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Database, Eraser } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { cleanAllCaches, cleanCache } from "@/lib/api";
import type { UseQueryResult } from "@/hooks/useQuery";
import { formatSize } from "@/lib/utils";
import {
  AlertModal,
  Button,
  ConfirmModal,
  EmptyState,
  ErrorLine,
  IconButton,
  InfoTip,
  NodeState,
  Section,
  Spinner,
  Table,
  Td,
  Th,
  THead,
  Tr,
} from "@/ui";
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
    <section ref={anchor} id="caches" className="scroll-mt-20">
      <Section
        title={t("library.caches")}
        meta={
          <span className="inline-flex items-center gap-1">
            {data ? formatSize(nodes.filter((n) => n.reachable).reduce((sum, n) => sum + n.total_bytes, 0)) : null}
            <InfoTip label={t("library.aboutCaches")}>{t("library.cachesHint")}</InfoTip>
          </span>
        }
        flush
      >
        {loading && !data && (
          <div className="flex justify-center py-12">
            <Spinner size="lg" label={t("common.loading")} />
          </div>
        )}
        <ErrorLine className="m-3">{error}</ErrorLine>

        {nodes.length > 0 && (
          <Table data-testid="caches-table">
            <THead>
              <Th>{t("cache.colCache")}</Th>
              <Th>{t("cache.colPath")}</Th>
              <Th numeric>{t("models.colSize")}</Th>
              <Th numeric>{t("cache.colFiles")}</Th>
              <Th srOnly>{t("models.colActions")}</Th>
            </THead>
            {nodes.map((node) => (
              <NodeCaches
                key={node.node_id || node.address}
                node={node}
                cleaning={cleaning}
                onClean={(entry) => setTarget({ node, entry })}
              />
            ))}
          </Table>
        )}

        {data && nodes.length === 0 && !loading && (
          <EmptyState icon={Database}>{t("cache.empty")}</EmptyState>
        )}
      </Section>

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

/** One machine's caches: a group header row, then a row per directory. */
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
  const id = node.node_id || node.address;

  return (
    <tbody data-testid={`cache-node-${id}`}>
      <tr className="border-b border-line bg-bg2">
        <td colSpan={5} className="px-3 py-1.5">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h3 className="text-[13px]">{label}</h3>
            {node.reachable ? (
              <NodeState state="ok" dotOnly />
            ) : (
              <NodeState
                state="unknown"
                label={t("library.cacheUnreachable")}
                title={node.reason ?? undefined}
              />
            )}
            {node.reachable && (
              <span className="ml-auto flex items-center gap-2">
                <span className="font-mono text-[12.5px] tabular-nums">{formatSize(node.total_bytes)}</span>
                <Button
                  size="sm"
                  variant="ghost"
                  loading={cleaning === cleanKey(node, null)}
                  onClick={() => onClean(null)}
                >
                  {t("library.cleanNode")}
                </Button>
              </span>
            )}
          </div>
        </td>
      </tr>
      {!node.reachable ? (
        // Unknown, not empty: the node did not answer, and four zeroes would
        // say this machine is holding nothing.
        <tr className="border-b border-line">
          <td colSpan={5} className="px-3 py-2 text-[12.5px] text-muted">{node.reason}</td>
        </tr>
      ) : (
        node.dirs.map((entry) => (
          <Tr key={entry.name} data-testid={`cache-${id}-${entry.name}`}>
            <Td className="whitespace-nowrap font-medium">{entry.name}</Td>
            <Td mono muted truncate title={entry.path} className="max-w-[320px]">
              {entry.path}
              {entry.error && <span className="ml-2 font-sans text-warn">{entry.error}</span>}
            </Td>
            <Td numeric>
              {entry.truncated ? t("cache.atLeast", { size: formatSize(entry.size_bytes) }) : formatSize(entry.size_bytes)}
            </Td>
            <Td numeric className="text-muted">{plural("cache.files", entry.file_count)}</Td>
            <Td actions>
              <IconButton
                size="sm"
                icon={Eraser}
                label={t("library.cleanOne", { name: entry.name, node: label })}
                loading={cleaning === cleanKey(node, entry)}
                onClick={() => onClean(entry)}
              />
            </Td>
          </Tr>
        ))
      )}
    </tbody>
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
