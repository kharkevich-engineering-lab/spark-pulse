/** The save bar of a Sources section: there only while something is unsaved.
 *
 * Each Sources section edits its own small piece of configuration and writes
 * it with its own call, so each carries its own bar rather than leaning on a
 * page-wide form that does not exist here.
 */

import { Undo2 } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { Button, ErrorLine } from "@/ui";

export interface SourcesSaveBarProps {
  dirty: boolean;
  saving: boolean;
  error: string | null;
  onSave: () => void;
  onDiscard: () => void;
}

export default function SourcesSaveBar({ dirty, saving, error, onSave, onDiscard }: SourcesSaveBarProps) {
  const { t } = useI18n();
  if (!dirty && !error) return null;
  return (
    <div
      role="region"
      aria-label={t("library.saveBar")}
      className="sticky bottom-3 mt-4 flex flex-wrap items-center gap-3 rounded-md border border-line bg-surface px-4 py-2.5 shadow-lg"
    >
      <div className="min-w-0 flex-1 text-[13px]">
        {error ? <ErrorLine>{error}</ErrorLine> : <span className="text-muted">{t("settings.unsaved")}</span>}
      </div>
      {dirty && (
        <Button size="sm" variant="ghost" icon={Undo2} onClick={onDiscard} disabled={saving}>
          {t("settings.discard")}
        </Button>
      )}
      <Button size="sm" variant="primary" loading={saving} disabled={!dirty} onClick={onSave}>
        {t("common.save")}
      </Button>
    </div>
  );
}
