import { useState } from "react";

import { planKubernetes } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { DeployPlanRequest, KubernetesHfCache, KubernetesPlan } from "@/lib/types";
import { Button, ErrorLine, Select } from "@/ui";

/** The same run as Kubernetes manifests, under the deploy preview (#164).
 *
 *  Asked for on demand rather than with the preview: it is a second backend's
 *  reading of the same intent, and most operators deploy to their own nodes.
 *  Nothing is applied — the findings say what the manifests needed and the
 *  run's intent did not carry.
 */
export function KubernetesPreview({ body }: { body: DeployPlanRequest }) {
  const { t } = useI18n();
  const [cache, setCache] = useState<KubernetesHfCache>("pvc");
  const [plan, setPlan] = useState<KubernetesPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const render = async () => {
    setLoading(true);
    setError(null);
    setPlan(null);
    try {
      setPlan(await planKubernetes({ ...body, hf_cache: cache }));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("kubernetesPreview.failed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div data-testid="kubernetes-preview">
      <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted mb-1">
        {t("kubernetesPreview.heading")}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Select
          aria-label={t("kubernetesPreview.cache")}
          data-testid="kubernetes-cache"
          value={cache}
          onChange={(e) => setCache(e.target.value as KubernetesHfCache)}
          className="text-sm w-auto"
        >
          <option value="pvc">{t("kubernetesPreview.cachePvc")}</option>
          <option value="hostPath">{t("kubernetesPreview.cacheHostPath")}</option>
        </Select>
        <Button size="sm" loading={loading} onClick={render} data-testid="kubernetes-render">
          {t("kubernetesPreview.render")}
        </Button>
      </div>
      <ErrorLine className="mt-2">{error}</ErrorLine>
      {plan && (
        <div className="mt-3 space-y-3">
          <p className="text-[13px] text-muted">{t("kubernetesPreview.notApplied")}</p>
          {plan.findings.length > 0 && (
            <div>
              <p className="text-[13px] font-medium mb-1">{t("kubernetesPreview.findings")}</p>
              <ul className="list-disc pl-5 space-y-1" data-testid="kubernetes-findings">
                {plan.findings.map((finding) => (
                  <li key={finding.code} className="text-[13px] text-warn">
                    <span className="font-mono">{finding.code}</span>: {finding.detail}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <pre
            data-testid="kubernetes-yaml"
            className="p-3 rounded-sm bg-surface border border-line text-[12.5px] font-mono overflow-x-auto max-h-[28rem]"
          >
            {plan.yaml}
          </pre>
        </div>
      )}
    </div>
  );
}
