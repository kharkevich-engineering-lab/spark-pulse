/** Mods: the scripts a recipe can run inside its container before the engine.
 *
 * They used to be a tab of the recipes page, with a "Custom mode" switch that
 * swapped the whole page for the operator's own files. Here they are a list
 * of their own: bundled and custom in one table, a source filter and a search
 * over it, and a row that opens the script — bundled mods to read, custom
 * ones to edit.
 */

import { useEffect, useMemo, useState } from "react";
import { AlertCircle, Check, Copy, FileCode, FileCode2, FileText, Loader2, Plus, Trash2, Wrench, X } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import {
  deleteCustomMod,
  fetchMod,
  fetchMods,
  getCustomModFiles,
  listCustomMods,
  saveCustomModFiles,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import type { CustomModInfo, ModDetail, ModFileMap, ModSummary } from "@/lib/types";
import {
  AlertModal,
  Button,
  ConfirmModal,
  EmptyRow,
  ErrorLine,
  IconButton,
  PageHeader,
  SearchInput,
  Section,
  Spinner,
  Table,
  Tabs,
  Td,
  Th,
  THead,
  Toolbar,
  Tr,
  matchesQuery,
} from "@/ui";
import SlideDrawer from "@/components/SlideDrawer";
import CustomModDrawer from "@/components/CustomModDrawer";
import NewModModal from "@/components/NewModModal";

type ModSource = "all" | "bundled" | "custom";

const KIND_STYLE: Record<string, string> = {
  patch: "bg-warning/15 text-warning border-warning/30",
  template: "bg-primary/15 text-blue2 border-primary/30",
  python: "bg-success/15 text-success border-success/30",
  script: "bg-tag-bg text-text-muted border-border",
  yaml: "bg-tag-bg text-text-muted border-border",
  file: "bg-tag-bg text-text-muted border-border",
};

const KIND_ICON: Record<string, React.ReactNode> = {
  patch: <FileCode2 size={12} />,
  template: <FileText size={12} />,
  python: <FileCode size={12} />,
  script: <FileCode size={12} />,
};

function FileBadge({ name, kind }: { name: string; kind: string }) {
  const cls = KIND_STYLE[kind] ?? KIND_STYLE.file;
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[11.5px] ${cls}`}>
      {KIND_ICON[kind]}
      {name}
    </span>
  );
}

/** A bundled mod, read-only: what it carries and the script it would run. */
function ModDrawer({ modId, onClose }: { modId: string; onClose: () => void }) {
  const { t } = useI18n();
  const [detail, setDetail] = useState<ModDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchMod(modId)
      .then(setDetail)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [modId]);

  const copyScript = async () => {
    if (!detail?.script) return;
    await navigator.clipboard.writeText(detail.script);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <SlideDrawer
      open={!!modId}
      onClose={onClose}
      header={
        <div className="flex items-center gap-2">
          <Wrench size={16} className="text-blue2" />
          <span className="truncate font-mono text-[16px] font-semibold">{modId}</span>
        </div>
      }
      actions={<IconButton size="sm" icon={X} label={t("common.close")} onClick={onClose} />}
    >
      {loading && (
        <div className="flex flex-1 items-center justify-center">
          <Loader2 className="animate-spin text-blue2" size={28} />
        </div>
      )}
      {error && (
        <div className="flex flex-1 items-center justify-center px-6">
          <div className="flex items-center gap-3 text-danger">
            <AlertCircle size={18} />
            <span>{error}</span>
          </div>
        </div>
      )}
      {detail && !loading && (
        <div className="space-y-5 p-5">
          {detail.description && <p className="text-[13px] leading-relaxed text-muted">{detail.description}</p>}
          {detail.files.length > 0 && (
            <div>
              <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">{t("recipes.assets")}</p>
              <div className="flex flex-wrap gap-1.5">
                {detail.files.map((f) => (
                  <FileBadge key={f.name} name={f.name} kind={f.kind} />
                ))}
              </div>
            </div>
          )}
          <div>
            <div className="mb-2 flex items-center justify-between">
              <p className="font-mono text-[13px] font-medium">run.sh</p>
              <button
                type="button"
                onClick={copyScript}
                className="flex items-center gap-1.5 text-[12px] text-muted transition-colors hover:text-text"
              >
                {copied ? <Check size={14} className="text-success" /> : <Copy size={14} />}
                {copied ? t("common.copied") : t("common.copy")}
              </button>
            </div>
            <pre className="overflow-x-auto whitespace-pre rounded-md border border-line bg-bg p-4 font-mono text-[12px] leading-relaxed">
              {detail.script || t("recipes.emptyScript")}
            </pre>
          </div>
        </div>
      )}
    </SlideDrawer>
  );
}

interface Row {
  key: string;
  name: string;
  description: string;
  files: React.ReactNode;
  source: "bundled" | "custom";
  bundled?: ModSummary;
  custom?: CustomModInfo;
}

export default function ModsPage() {
  const { t, plural } = useI18n();
  const { data: mods, loading: modsLoading, error: modsError } = useQuery(fetchMods);
  const [customMods, setCustomMods] = useState<CustomModInfo[]>([]);
  const [customError, setCustomError] = useState<string | null>(null);
  const [source, setSource] = useState<ModSource>("all");
  const [query, setQuery] = useState("");
  const [activeModId, setActiveModId] = useState<string | null>(null);
  const [selectedMod, setSelectedMod] = useState<CustomModInfo | null>(null);
  const [modFiles, setModFiles] = useState<ModFileMap>({});
  const [showNewMod, setShowNewMod] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<CustomModInfo | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);

  const loadCustom = async () => {
    try {
      setCustomMods(await listCustomMods());
      setCustomError(null);
    } catch (e) {
      setCustomError(e instanceof Error ? e.message : t("recipes.loadCustomFailed"));
    }
  };

  useEffect(() => {
    void loadCustom();
    // Read once on arrival; every write below re-reads it.
  }, []);

  const openCustom = async (mod: CustomModInfo) => {
    try {
      const { files } = await getCustomModFiles(mod.id);
      setModFiles(files);
      setSelectedMod(mod);
    } catch {
      setAlert({ title: t("common.error"), message: t("recipes.loadModFailed") });
    }
  };

  const runDelete = async () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    setDeleteTarget(null);
    try {
      await deleteCustomMod(target.id);
      await loadCustom();
    } catch (e) {
      setAlert({
        title: t("recipes.deleteFailed", { name: target.name }),
        message: e instanceof Error ? e.message : t("common.unknownError"),
      });
    }
  };

  const rows = useMemo<Row[]>(() => {
    const bundled: Row[] = (mods ?? []).map((m) => ({
      key: `b:${m.id}`,
      name: m.id,
      description: m.description,
      source: "bundled",
      bundled: m,
      files: (
        <span className="flex flex-wrap gap-1">
          {m.has_patches && (
            <span className="rounded border border-warning/30 bg-warning/15 px-1.5 py-0.5 font-mono text-[11.5px] text-warning">
              {t("recipes.patches")}
            </span>
          )}
          {m.files.map((f) => (
            <FileBadge key={f.name} name={f.name} kind={f.kind} />
          ))}
        </span>
      ),
    }));
    const custom: Row[] = customMods.map((m) => ({
      key: `c:${m.id}`,
      name: m.name,
      description: m.description,
      source: "custom",
      custom: m,
      files: m.has_run_sh ? <FileBadge name="run.sh" kind="script" /> : null,
    }));
    return [...bundled, ...custom];
  }, [mods, customMods, t]);

  const visible = rows.filter(
    (r) => (source === "all" || r.source === source) && matchesQuery(query, r.name, r.description),
  );
  const counts = {
    all: rows.length,
    bundled: rows.filter((r) => r.source === "bundled").length,
    custom: rows.filter((r) => r.source === "custom").length,
  };

  const open = (row: Row) => {
    if (row.bundled) setActiveModId(row.bundled.id);
    else if (row.custom) void openCustom(row.custom);
  };

  return (
    <div>
      <PageHeader
        eyebrow={t("nav.mods")}
        title={t("mods.heading")}
        description={plural("mods.meta", rows.length)}
        actions={
          <Button variant="primary" icon={Plus} onClick={() => setShowNewMod(true)}>
            {t("recipes.newMod")}
          </Button>
        }
      />

      <Toolbar>
        <SearchInput value={query} onChange={setQuery} label={t("mods.search")} clearLabel={t("common.clear")} />
        <Tabs
          label={t("mods.sourceLabel")}
          value={source}
          onChange={(id) => setSource(id as ModSource)}
          tabs={[
            { id: "all", label: t("recipes.sourceAll"), count: counts.all },
            { id: "bundled", label: t("recipes.sourceBundled"), count: counts.bundled },
            { id: "custom", label: t("recipes.sourceCustom"), count: counts.custom },
          ]}
        />
      </Toolbar>

      {modsError && <ErrorLine>{modsError}</ErrorLine>}
      {customError && <ErrorLine>{customError}</ErrorLine>}

      {modsLoading ? (
        <div className="flex justify-center py-20">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      ) : (
        <Section flush>
          <Table data-testid="mods-table">
            <THead>
              <Th>{t("mods.colMod")}</Th>
              <Th>{t("mods.colDescription")}</Th>
              <Th>{t("mods.colFiles")}</Th>
              <Th>{t("mods.colSource")}</Th>
              <Th srOnly>{t("runs.actions")}</Th>
            </THead>
            <tbody>
              {visible.length === 0 && (
                <EmptyRow colSpan={5}>
                  {rows.length === 0 ? (
                    <>
                      <p>{t("recipes.noMods")}</p>
                      <p className="mt-1">{t("recipes.modExplainer")}</p>
                    </>
                  ) : source === "custom" && counts.custom === 0 ? (
                    <>
                      <p>{t("recipes.noCustomMods")}</p>
                      <p className="mt-1">{t("recipes.modExplainer")}</p>
                    </>
                  ) : (
                    t("recipes.noMatch")
                  )}
                </EmptyRow>
              )}
              {visible.map((row) => (
                <Tr key={row.key} data-testid={`mod-row-${row.name}`} interactive onClick={() => open(row)}>
                  <Td className="whitespace-nowrap">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        open(row);
                      }}
                      className="text-left font-mono font-semibold hover:text-blue2"
                    >
                      {row.name}
                    </button>
                  </Td>
                  <Td muted truncate title={row.description} className="min-[900px]:w-[45%]">
                    {row.description || "—"}
                  </Td>
                  <Td>{row.files}</Td>
                  <Td muted className="whitespace-nowrap">
                    {row.source === "bundled" ? t("recipes.sourceBundled") : t("recipes.sourceCustom")}
                  </Td>
                  <Td actions>
                    {row.custom && (
                      <IconButton
                        size="sm"
                        icon={Trash2}
                        label={t("recipes.deleteMod", { name: row.name })}
                        onClick={() => setDeleteTarget(row.custom!)}
                      />
                    )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </Section>
      )}

      {activeModId && <ModDrawer modId={activeModId} onClose={() => setActiveModId(null)} />}

      {selectedMod && (
        <CustomModDrawer
          open
          mod={selectedMod}
          files={modFiles}
          onClose={() => setSelectedMod(null)}
          onSave={async (id, fileMap) => {
            await saveCustomModFiles(id, fileMap);
            await loadCustom();
          }}
          onDelete={async (id) => {
            await deleteCustomMod(id);
            await loadCustom();
          }}
          onError={(msg) => setAlert({ title: t("common.error"), message: msg })}
        />
      )}

      {showNewMod && (
        <NewModModal
          open
          onClose={() => setShowNewMod(false)}
          onSave={async () => {
            await loadCustom();
            setShowNewMod(false);
          }}
          onError={(msg) => setAlert({ title: t("common.error"), message: msg })}
        />
      )}

      {deleteTarget && (
        <ConfirmModal
          open
          onClose={() => setDeleteTarget(null)}
          onConfirm={runDelete}
          title={t("recipes.deleteModTitle")}
          message={t("recipes.deleteBody", { name: deleteTarget.name })}
          confirmLabel={t("common.delete")}
          confirmVariant="danger"
        />
      )}

      {alert && <AlertModal open onClose={() => setAlert(null)} title={alert.title} message={alert.message} />}
    </div>
  );
}
