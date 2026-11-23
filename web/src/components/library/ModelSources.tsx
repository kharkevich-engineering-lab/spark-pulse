/** Where models come from: a name, a hub or a path, and the key of a token.
 *
 * What Settings → Library used to hold for models. A name here is what the
 * download dialog and a recipe's `source` field select. Edits are held here
 * and written by this section's own save bar through `PUT /api/models/sources`.
 */

import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { fetchModelSources, saveModelSources } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import type { ModelSource } from "@/lib/types";
import { IconButton, Input, Select, Spinner } from "@/ui";
import SettingsSection from "@/components/SettingsSection";
import SourcesSaveBar from "./SourcesSaveBar";

export const NEW_MODEL_SOURCE: ModelSource = {
  name: "",
  type: "hf_hub",
  endpoint: "https://huggingface.co",
  token_secret: "",
};

export interface ModelSourcesProps {
  /** The header's *Add source* bumps this; each change appends an empty source. */
  addTick?: number;
}

export default function ModelSources({ addTick = 0 }: ModelSourcesProps) {
  const { t } = useI18n();
  const { data: saved, refetch } = useQuery(fetchModelSources);
  const [draft, setDraft] = useState<ModelSource[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sources = draft ?? saved;

  const seenTick = useRef(addTick);
  useEffect(() => {
    if (addTick === seenTick.current) return;
    seenTick.current = addTick;
    if (sources) setDraft([...sources, { ...NEW_MODEL_SOURCE }]);
    // The list is read at the moment of the press, not a dependency.
  }, [addTick]);

  if (!sources) {
    return (
      <div className="flex justify-center py-16">
        <Spinner size="lg" label={t("common.loading")} />
      </div>
    );
  }

  const dirty = draft !== null && JSON.stringify(draft) !== JSON.stringify(saved);
  const patch = (i: number, change: Partial<ModelSource>) =>
    setDraft(sources.map((s, idx) => (idx === i ? { ...s, ...change } : s)));

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    setError(null);
    try {
      await saveModelSources(draft);
      setDraft(null);
      await refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("common.unknownError"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div data-testid="model-sources">
      <SettingsSection first title={t("models.sources")} hint={t("settingsPage.sourcesHint")}>
        <div className="space-y-3">
          {sources.length === 0 && <p className="text-[14px] text-muted">{t("models.noSources")}</p>}
          {sources.map((s, i) => (
            <div
              key={i}
              className="grid grid-cols-1 items-center gap-2 md:grid-cols-[1fr_140px_1fr_1fr_auto]"
            >
              <Input
                aria-label={t("models.sourceName", { n: i + 1 })}
                value={s.name}
                onChange={(e) => patch(i, { name: e.target.value })}
                placeholder={t("models.namePlaceholder")}
              />
              <Select
                aria-label={t("models.sourceType", { n: i + 1 })}
                value={s.type}
                onChange={(e) => patch(i, { type: e.target.value as ModelSource["type"] })}
              >
                <option value="hf_hub">hf_hub</option>
                <option value="local_path">local_path</option>
              </Select>
              {s.type === "hf_hub" ? (
                <>
                  <Input
                    mono
                    aria-label={t("models.sourceEndpoint", { n: i + 1 })}
                    value={s.endpoint ?? ""}
                    onChange={(e) => patch(i, { endpoint: e.target.value })}
                    placeholder="https://huggingface.co"
                  />
                  <Input
                    mono
                    aria-label={t("models.sourceToken", { n: i + 1 })}
                    value={s.token_secret ?? ""}
                    onChange={(e) => patch(i, { token_secret: e.target.value })}
                    placeholder={t("models.tokenPlaceholder")}
                  />
                </>
              ) : (
                <Input
                  mono
                  aria-label={t("models.sourcePath", { n: i + 1 })}
                  value={s.path ?? ""}
                  onChange={(e) => patch(i, { path: e.target.value })}
                  placeholder="/models"
                  className="md:col-span-2"
                />
              )}
              <IconButton
                size="sm"
                icon={X}
                label={t("settingsPage.removeSource", { n: String(i + 1) })}
                onClick={() => setDraft(sources.filter((_, idx) => idx !== i))}
                className="border-transparent text-muted hover:border-line hover:text-bad"
              />
            </div>
          ))}
        </div>
      </SettingsSection>

      <SourcesSaveBar
        dirty={dirty}
        saving={saving}
        error={error}
        onSave={save}
        onDiscard={() => {
          setDraft(null);
          setError(null);
        }}
      />
    </div>
  );
}
