/** Network discovery and fabric validation, on the page about nodes.
 *
 * This used to be a card in Settings, under a "Cluster" tab whose only other
 * control was a toggle nothing read. It describes the machines — their
 * interfaces, their RoCE devices, the NCCL variables a multi-node launch will
 * be given — and the machines are managed here, next to the node registry. A
 * reading about this host belongs beside the list of hosts, not two pages away
 * under a heading that also held ports and shm sizes.
 */

import { useState } from "react";
import { AlertCircle, Check, Radio, Wifi, WifiOff } from "lucide-react";
import { runDiscovery, type DiscoveryResult, type ValidationResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Button, Card, Code, ErrorLine } from "@/ui";

export default function NetworkDiscovery() {
  const { t } = useI18n();
  const [discoveryResult, setDiscoveryResult] = useState<DiscoveryResult | null>(null);
  const [validationResult, setValidationResult] = useState<ValidationResult | null>(null);
  const [discoveryLoading, setDiscoveryLoading] = useState(false);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);

  const handleDiscover = async () => {
    setDiscoveryError(null);
    setDiscoveryLoading(true);
    try {
      const response = await runDiscovery();
      setDiscoveryResult(response.detected);
      setValidationResult(response.validation);
    } catch (e) {
      setDiscoveryError(e instanceof Error ? e.message : "Discovery failed");
    } finally {
      setDiscoveryLoading(false);
    }
  };

  return (
    <Card padding="none" className="p-5 space-y-4">
      <div className="flex items-center justify-between pb-3 border-b border-line">
        <div className="flex items-center gap-2">
          <Radio size={16} className="text-blue2" />
          <h3 className="font-semibold">{t("settings.discovery")}</h3>
        </div>
        <Button size="sm" icon={Radio} loading={discoveryLoading} onClick={handleDiscover}>
          {t("settings.discover")}
        </Button>
      </div>

      <ErrorLine>{discoveryError}</ErrorLine>

      {discoveryResult && (
        <div className="space-y-3">
          <div className="flex items-center justify-between text-sm">
            <span className="text-text-muted">{t("settings.localIp")}</span>
            <Code>{discoveryResult.local_ip || t("common.notDetected")}</Code>
          </div>

          <div className="flex items-center justify-between text-sm">
            <span className="text-text-muted">{t("settings.ethernet")}</span>
            <Code>{discoveryResult.ethernet_if || t("common.notDetected")}</Code>
          </div>

          <div className="flex items-center justify-between text-sm">
            <span className="text-text-muted">{t("settings.infiniband")}</span>
            <span className="flex items-center gap-1 text-xs">
              {discoveryResult.infiniband_present ? (
                <>
                  <Wifi size={12} className="text-success" />
                  <span className="text-success">{discoveryResult.infiniband_devices.length} HCA{discoveryResult.infiniband_devices.length > 1 ? "s" : ""}</span>
                </>
              ) : (
                <>
                  <WifiOff size={12} className="text-text-muted" />
                  <span className="text-text-muted">{t("common.notPresent")}</span>
                </>
              )}
            </span>
          </div>

          {discoveryResult.infiniband_present && discoveryResult.infiniband_devices.length > 0 && (
            <div className="text-xs text-text-muted space-y-0.5 pl-1">
              {discoveryResult.infiniband_devices.map((dev) => (
                <div key={dev.hca} className="flex items-center gap-1.5">
                  <span className="font-mono">{dev.hca}</span>
                  <span className={`px-1.5 py-0.5 rounded ${dev.state === "ACTIVE" ? "bg-success/15 text-success" : "bg-warning/15 text-warning"}`}>
                    {dev.state}
                  </span>
                  {dev.ports.length > 0 && <span>ports: {dev.ports.join(",")}</span>}
                </div>
              ))}
            </div>
          )}

          {discoveryResult.nccl_defaults && (
            <div className="pt-2 border-t border-border space-y-1.5">
              <div className="flex items-center justify-between text-sm">
                <span className="text-text-muted">{t("settings.ncclSocket")}</span>
                <Code>{discoveryResult.nccl_defaults.socket_ifname}</Code>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span className="text-text-muted">{t("settings.ncclHca")}</span>
                <Code>{discoveryResult.nccl_defaults.ib_hca || t("common.none")}</Code>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span className="text-text-muted">{t("settings.ncclDisabled")}</span>
                <span className={`text-xs font-medium ${discoveryResult.nccl_defaults.ib_disable ? "text-warning" : "text-success"}`}>
                  {discoveryResult.nccl_defaults.ib_disable ? t("common.yes") : t("common.no")}
                </span>
              </div>

              <p className="text-xs text-text-muted pt-1">
                {t("settings.ncclNote")}
              </p>
            </div>
          )}

          {validationResult && (
            <div className={`pt-2 border-t border-border text-xs space-y-1 ${!validationResult.healthy ? "text-danger" : validationResult.warnings.length > 0 ? "text-warning" : "text-success"}`}>
              <div className="flex items-center gap-1.5 font-medium">
                {validationResult.healthy ? <Check size={12} /> : <AlertCircle size={12} />}
                {validationResult.healthy ? t("settings.networkHealthy") : t("settings.networkIssues")}
              </div>
              {validationResult.warnings.map((w, i) => <div key={`w${i}`} className="pl-3.5">⚠ {w}</div>)}
              {validationResult.errors.map((e, i) => <div key={`e${i}`} className="pl-3.5">✕ {e}</div>)}
            </div>
          )}
        </div>
      )}

      {!discoveryResult && !discoveryLoading && (
        <p className="text-xs text-text-muted">{t("settings.discoveryIdle")}</p>
      )}
    </Card>
  );
}
