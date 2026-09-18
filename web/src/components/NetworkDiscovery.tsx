/** Network discovery and fabric validation, at the foot of the Fleet page.
 *
 * This used to be a card in Settings, under a "Cluster" tab whose only other
 * control was a toggle nothing read. It describes this host — its interfaces,
 * its RoCE devices, the NCCL variables a multi-node launch will be given —
 * and the machines are managed here, under the node registry.
 *
 * It is collapsed, and last. An operator reads the list of machines and what
 * their cables are doing; a scan of the LAN is what you reach for when a
 * machine you expected is not in that list, which is once, not every time the
 * page is opened.
 */

import { useState } from "react";
import { AlertCircle, Check, ChevronDown, ChevronRight, Radio, Wifi, WifiOff } from "lucide-react";
import { runDiscovery, type DiscoveryResult, type ValidationResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Button, Code, ErrorLine } from "@/ui";

/** The rule that separates one section of the page from the next. */
const SECTION = "border-t border-line pt-12 mt-10 first:border-t-0 first:pt-0 first:mt-0";

export default function NetworkDiscovery() {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
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
      setDiscoveryError(e instanceof Error ? e.message : t("nodes.discoveryFailed"));
    } finally {
      setDiscoveryLoading(false);
    }
  };

  return (
    <section data-testid="network-discovery" className={SECTION}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex items-center gap-2 text-[22px] font-bold tracking-[-0.02em] hover:text-blue2 transition-colors duration-200"
      >
        {open ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
        {t("fleet.discover")}
      </button>

      {open && (
        <div className="mt-4 space-y-4">
          <Button size="sm" icon={Radio} loading={discoveryLoading} onClick={handleDiscover}>
            {t("settings.discover")}
          </Button>

          <ErrorLine>{discoveryError}</ErrorLine>

          {discoveryResult && (
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-3 text-[14px]">
                <span className="text-muted">{t("settings.localIp")}</span>
                <Code>{discoveryResult.local_ip || t("common.notDetected")}</Code>
              </div>

              <div className="flex items-center justify-between gap-3 text-[14px]">
                <span className="text-muted">{t("settings.ethernet")}</span>
                <Code>{discoveryResult.ethernet_if || t("common.notDetected")}</Code>
              </div>

              <div className="flex items-center justify-between gap-3 text-[14px]">
                <span className="text-muted">{t("settings.infiniband")}</span>
                <span className="flex items-center gap-1.5 text-[13px]">
                  {discoveryResult.infiniband_present ? (
                    <>
                      <Wifi size={14} className="text-good" />
                      <span className="text-good">
                        {discoveryResult.infiniband_devices.length} HCA
                        {discoveryResult.infiniband_devices.length > 1 ? "s" : ""}
                      </span>
                    </>
                  ) : (
                    <>
                      <WifiOff size={14} className="text-muted" />
                      <span className="text-muted">{t("common.notPresent")}</span>
                    </>
                  )}
                </span>
              </div>

              {discoveryResult.infiniband_present && discoveryResult.infiniband_devices.length > 0 && (
                <div className="space-y-1 text-[13px] text-muted">
                  {discoveryResult.infiniband_devices.map((dev) => (
                    <div key={dev.hca} className="flex flex-wrap items-center gap-2">
                      <span className="font-mono">{dev.hca}</span>
                      <span className={dev.state === "ACTIVE" ? "text-good" : "text-warn"}>
                        {dev.state}
                      </span>
                      {dev.ports.length > 0 && <span>ports: {dev.ports.join(",")}</span>}
                    </div>
                  ))}
                </div>
              )}

              {discoveryResult.nccl_defaults && (
                <div className="space-y-1.5 border-t border-line pt-3">
                  <div className="flex items-center justify-between gap-3 text-[14px]">
                    <span className="text-muted">{t("settings.ncclSocket")}</span>
                    <Code>{discoveryResult.nccl_defaults.socket_ifname}</Code>
                  </div>
                  <div className="flex items-center justify-between gap-3 text-[14px]">
                    <span className="text-muted">{t("settings.ncclHca")}</span>
                    <Code>{discoveryResult.nccl_defaults.ib_hca || t("common.none")}</Code>
                  </div>
                  <div className="flex items-center justify-between gap-3 text-[14px]">
                    <span className="text-muted">{t("settings.ncclDisabled")}</span>
                    <span
                      className={`text-[13px] font-medium ${
                        discoveryResult.nccl_defaults.ib_disable ? "text-warn" : "text-good"
                      }`}
                    >
                      {discoveryResult.nccl_defaults.ib_disable ? t("common.yes") : t("common.no")}
                    </span>
                  </div>

                  <p className="pt-1 text-[13px] text-muted">{t("settings.ncclNote")}</p>
                </div>
              )}

              {validationResult && (
                <div
                  className={`space-y-1 border-t border-line pt-3 text-[13px] ${
                    !validationResult.healthy
                      ? "text-bad"
                      : validationResult.warnings.length > 0
                        ? "text-warn"
                        : "text-good"
                  }`}
                >
                  <div className="flex items-center gap-1.5 font-medium">
                    {validationResult.healthy ? <Check size={14} /> : <AlertCircle size={14} />}
                    {validationResult.healthy
                      ? t("settings.networkHealthy")
                      : t("settings.networkIssues")}
                  </div>
                  {validationResult.warnings.map((w, i) => (
                    <div key={`w${i}`} className="pl-5">
                      {w}
                    </div>
                  ))}
                  {validationResult.errors.map((e, i) => (
                    <div key={`e${i}`} className="pl-5">
                      {e}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {!discoveryResult && !discoveryLoading && (
            <p className="text-[13px] text-muted">{t("settings.discoveryIdle")}</p>
          )}
        </div>
      )}
    </section>
  );
}
