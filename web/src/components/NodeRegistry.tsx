/** The node registry: the persisted set of machines, replacing two IP boxes.
 *
 * The Cluster page used to ask for a head IP and a comma-separated list of
 * worker IPs in free text, and threw both away on refresh. This is the table
 * `docs/cluster-agent-plan.md` section 8 asks for: each node's name, address,
 * the interfaces we derived rather than guessed, whether it is the control
 * plane, and its state — with adding and removing.
 *
 * Three details are deliberate:
 *
 * * **Three states are shown as three states.** Healthy, unknown and dead are
 *   visually distinct, and unknown says "status unverified" in words rather
 *   than showing a spinner where the honest answer is that we do not know.
 * * **Discovery never blocks manual entry.** The add dialog opens on the
 *   address field. Browsing the LAN is a button next to it, and when mDNS is
 *   unavailable the dialog says so and keeps working.
 * * **Removal is named for what it does.** This is *forget* — it drops what we
 *   know about a machine that is already gone. Wiping a node's identity and
 *   uninstalling its agent while keeping that identity are separate actions,
 *   and they arrive with the agent.
 * * **Installing the agent is done here, from the browser.** A registered
 *   address is not a node anyone can reach; the agent has to be put on the
 *   machine, and that needs an SSH login. The install dialog takes a
 *   password, a private key (pasted or uploaded, with its passphrase) or the
 *   control plane's own key, shows the node's host key fingerprint before any
 *   of it is sent, and reports what the installer did. None of the secrets is
 *   kept: what the registry keeps is the SSH user, and what the node keeps is
 *   the control plane's public key.
 */

import { useCallback, useEffect, useState } from "react";
import { useI18n } from "@/lib/i18n";
import {
  addNode,
  discoverNodes,
  fetchNodeDiagnostics,
  fetchNodeHostKey,
  fetchNodes,
  installNodeAgent,
  removeNode,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { ConfirmModal } from "@/components/Modal";
import type {
  ClusterNode,
  DiscoveredPeer,
  InstallReport,
  NodeAuthMethod,
  NodeFinding,
  NodeHostKey,
  NodeState,
} from "@/lib/types";
import {
  AlertCircle,
  CheckCircle2,
  Download,
  Info,
  KeyRound,
  Loader2,
  Network,
  Plus,
  Radar,
  Server,
  Trash2,
  X,
} from "lucide-react";

/** How each state reads, and why. `unknown` is the one that matters. */
const STATE_STYLE: Record<NodeState, { label: string; className: string; title: string }> = {
  healthy: {
    label: "Healthy",
    className: "bg-success/20 text-success border-success/30",
    title: "Reached and responding.",
  },
  unknown: {
    label: "Unknown",
    className: "bg-warning/20 text-warning border-warning/30",
    title: "Status unverified — we could not reach it, which is not the same as failed.",
  },
  dead: {
    label: "Dead",
    className: "bg-danger/20 text-danger border-danger/30",
    title: "Confirmed unreachable.",
  },
};

function NodeStateBadge({ state }: { state: NodeState }) {
  const style = STATE_STYLE[state] ?? STATE_STYLE.unknown;
  return (
    <span
      title={style.title}
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${style.className}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {style.label}
    </span>
  );
}

function interfaceSummary(node: ClusterNode): string {
  const names = [node.ethernet_interface, ...node.infiniband_interfaces].filter(Boolean);
  return names.length > 0 ? names.join(", ") : "—";
}

/** Findings with their remedy. Never rendered as errors: each one is a
 * condition the cluster runs with, and each costs an afternoon when unnamed. */
function Diagnostics({ findings }: { findings: NodeFinding[] }) {
  if (findings.length === 0) return null;
  return (
    <div className="space-y-2" data-testid="node-diagnostics">
      {findings.map((finding) => {
        const warning = finding.severity === "warning";
        return (
          <div
            key={finding.code}
            role="note"
            className={`flex items-start gap-3 rounded-lg border p-3 text-sm ${
              warning
                ? "border-warning/30 bg-warning/10"
                : "border-border bg-surface-hover"
            }`}
          >
            {warning ? (
              <AlertCircle size={16} className="mt-0.5 shrink-0 text-warning" />
            ) : (
              <Info size={16} className="mt-0.5 shrink-0 text-text-muted" />
            )}
            <div className="min-w-0">
              <p className="font-medium">{finding.summary}</p>
              <p className="mt-1 text-text-muted">{finding.remedy}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}

interface AddNodeDialogProps {
  onClose: () => void;
  onAdded: (node: ClusterNode) => void;
}

function AddNodeDialog({ onClose, onAdded }: AddNodeDialogProps) {
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [sshUser, setSshUser] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [scanning, setScanning] = useState(false);
  const [peers, setPeers] = useState<DiscoveredPeer[] | null>(null);
  const [mdnsAvailable, setMdnsAvailable] = useState(true);

  const scan = async () => {
    setScanning(true);
    setError(null);
    try {
      const result = await discoverNodes();
      setPeers(result.peers);
      setMdnsAvailable(result.mdns_available);
    } catch {
      // Discovery failing is never fatal: typing an address still works.
      setPeers([]);
      setMdnsAvailable(false);
    } finally {
      setScanning(false);
    }
  };

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const added = await addNode({
        name: name.trim() || undefined,
        address: address.trim(),
        ssh_user: sshUser.trim() || undefined,
      });
      onAdded(added);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not add the node");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div
        role="dialog"
        aria-label={t("nodes.addNode")}
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-border bg-surface p-6 shadow-2xl"
      >
        <div className="mb-6 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-lg font-bold">
            <Server size={20} className="text-primary" />
            {t("nodes.addNode")}
          </h3>
          <button
            onClick={onClose}
            aria-label={t("common.close")}
            className="rounded-lg p-1 hover:bg-surface-hover"
          >
            <X size={18} />
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <label
              htmlFor="node-address"
              className="mb-1 block text-sm font-medium text-text-muted"
            >
              {t("nodes.address")}
            </label>
            <input
              id="node-address"
              type="text"
              autoFocus
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder={t("nodes.addressPlaceholder")}
              className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
            />
          </div>

          <div>
            <label
              htmlFor="node-name"
              className="mb-1 block text-sm font-medium text-text-muted"
            >
              {t("nodes.name")}
            </label>
            <input
              id="node-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("nodes.namePlaceholder")}
              className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
            />
          </div>

          <div>
            <label
              htmlFor="node-ssh-user"
              className="mb-1 block text-sm font-medium text-text-muted"
            >
              {t("nodes.sshUser")}
            </label>
            <input
              id="node-ssh-user"
              type="text"
              value={sshUser}
              onChange={(e) => setSshUser(e.target.value)}
              placeholder={t("nodes.sshUserPlaceholder")}
              className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
            />
          </div>
          <p className="text-xs text-text-muted">{t("nodes.addNote")}</p>

          {/* Discovery is an aid, never a gate: the address field above always works. */}
          <div className="rounded-lg border border-border p-3">
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-medium">{t("nodes.find")}</p>
              <button
                onClick={scan}
                disabled={scanning}
                className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-surface-hover disabled:opacity-50"
              >
                {scanning ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <Radar size={14} />
                )}
                {t("nodes.scan")}
              </button>
            </div>

            {peers !== null && !mdnsAvailable && (
              <p className="mt-2 text-sm text-text-muted">
                {t("nodes.mdnsUnavailable")}
              </p>
            )}
            {peers !== null && mdnsAvailable && peers.length === 0 && (
              <p className="mt-2 text-sm text-text-muted">
                {t("nodes.noResponders")}
              </p>
            )}
            {peers !== null && peers.length > 0 && (
              <ul className="mt-2 space-y-1">
                {peers.map((peer) => (
                  <li key={`${peer.address}-${peer.service}`}>
                    <button
                      onClick={() => {
                        setAddress(peer.address);
                        if (!name) setName(peer.hostname.replace(/\.local$/, ""));
                      }}
                      disabled={peer.registered}
                      className="flex w-full items-center justify-between gap-3 rounded-md px-2 py-1.5 text-left text-sm hover:bg-surface-hover disabled:opacity-50"
                    >
                      <span className="min-w-0 truncate">
                        <span className="font-medium">{peer.address}</span>
                        <span className="text-text-muted"> · {peer.hostname || "unnamed"}</span>
                      </span>
                      <span className="shrink-0 text-xs text-text-muted">
                        {peer.registered
                          ? "already registered"
                          : peer.is_spark_pulse
                            ? `Spark Pulse ${peer.version}`
                            : "SSH only"}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {error && (
            <div
              role="alert"
              className="flex items-center gap-2 rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger"
            >
              <AlertCircle size={16} className="shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>

        <div className="mt-6 flex items-center justify-end gap-3 border-t border-border pt-4">
          <button
            onClick={onClose}
            className="rounded-lg border border-border px-4 py-2 transition-colors hover:bg-surface-hover"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={submitting || !address.trim()}
            className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
          >
            {submitting ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />}
            Add node
          </button>
        </div>
      </div>
    </div>
  );
}

const INPUT =
  "w-full rounded-lg border border-border bg-bg px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-primary/50";
const LABEL = "mb-1 block text-sm font-medium text-text-muted";

interface InstallAgentDialogProps {
  node: ClusterNode;
  onClose: () => void;
  onInstalled: () => void;
}

/** SSH once, from here. Three phases in one dialog: the credentials, the host
 * key the node offers (shown before any of them is sent), and the installer's
 * report. The request runs as long as the install does, so the dialog stays
 * put and says what is happening rather than closing on a promise. */
function InstallAgentDialog({ node, onClose, onInstalled }: InstallAgentDialogProps) {
  const { t } = useI18n();
  const [username, setUsername] = useState(node.ssh_user || "");
  const [port, setPort] = useState("22");
  const [auth, setAuth] = useState<NodeAuthMethod>("password");
  const [password, setPassword] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [sudoPassword, setSudoPassword] = useState("");
  const [hostKey, setHostKey] = useState<NodeHostKey | null>(null);
  const [checking, setChecking] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [report, setReport] = useState<InstallReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const portNumber = Number.parseInt(port, 10);
  const portValid = Number.isInteger(portNumber) && portNumber > 0 && portNumber < 65536;
  const credentialsReady =
    username.trim() !== "" &&
    portValid &&
    (auth === "control_plane_key" ||
      (auth === "password" && password !== "") ||
      (auth === "key" && privateKey.trim() !== ""));

  const checkHostKey = async () => {
    setChecking(true);
    setError(null);
    setHostKey(null);
    try {
      setHostKey(await fetchNodeHostKey(node.id, portValid ? portNumber : 22));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read the host key");
    } finally {
      setChecking(false);
    }
  };

  const readKeyFile = async (file: File | undefined) => {
    if (!file) return;
    setPrivateKey(await file.text());
  };

  const install = async () => {
    if (!hostKey) return;
    setInstalling(true);
    setError(null);
    try {
      const result = await installNodeAgent(node.id, {
        username: username.trim(),
        auth,
        host_key_fingerprint: hostKey.fingerprint,
        port: portNumber,
        ...(auth === "password" ? { password } : {}),
        ...(auth === "key"
          ? { private_key: privateKey, ...(passphrase ? { passphrase } : {}) }
          : {}),
        ...(sudoPassword ? { sudo_password: sudoPassword } : {}),
      });
      setReport(result);
      onInstalled();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The install failed");
      // A refused host key means the fingerprint shown is no longer what the
      // node offers; it has to be fetched and looked at again.
      if (e instanceof Error && /host key/i.test(e.message)) setHostKey(null);
    } finally {
      setInstalling(false);
    }
  };

  const title = t("nodes.install.title", { name: node.name || node.address });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div
        role="dialog"
        aria-label={title}
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-border bg-surface p-6 shadow-2xl"
      >
        <div className="mb-4 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-lg font-bold">
            <KeyRound size={20} className="text-primary" />
            {title}
          </h3>
          <button
            onClick={onClose}
            aria-label={t("common.close")}
            className="rounded-lg p-1 hover:bg-surface-hover"
          >
            <X size={18} />
          </button>
        </div>

        {report ? (
          <InstallOutcome report={report} />
        ) : (
          <div className="space-y-4">
            <p className="text-sm text-text-muted">
              {t("nodes.install.intro", { address: node.address })}
            </p>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-[1fr_7rem]">
              <div>
                <label htmlFor="install-username" className={LABEL}>
                  {t("nodes.install.username")}
                </label>
                <input
                  id="install-username"
                  type="text"
                  autoFocus
                  autoComplete="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder={t("nodes.sshUserPlaceholder")}
                  disabled={installing}
                  className={INPUT}
                />
              </div>
              <div>
                <label htmlFor="install-port" className={LABEL}>
                  {t("nodes.install.port")}
                </label>
                <input
                  id="install-port"
                  type="number"
                  min={1}
                  max={65535}
                  value={port}
                  onChange={(e) => {
                    setPort(e.target.value);
                    setHostKey(null);
                  }}
                  disabled={installing}
                  className={INPUT}
                />
              </div>
            </div>

            <fieldset>
              <legend className={LABEL}>{t("nodes.install.auth")}</legend>
              <div className="flex flex-wrap gap-4 text-sm">
                {(
                  [
                    ["password", t("nodes.install.authPassword")],
                    ["key", t("nodes.install.authKey")],
                    ["control_plane_key", t("nodes.install.authControlPlaneKey")],
                  ] as [NodeAuthMethod, string][]
                ).map(([method, label]) => (
                  <label key={method} className="flex items-center gap-1.5">
                    <input
                      type="radio"
                      name="install-auth"
                      value={method}
                      checked={auth === method}
                      onChange={() => setAuth(method)}
                      disabled={installing}
                    />
                    {label}
                  </label>
                ))}
              </div>
            </fieldset>

            {auth === "password" && (
              <div>
                <label htmlFor="install-password" className={LABEL}>
                  {t("nodes.install.password")}
                </label>
                <input
                  id="install-password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={installing}
                  className={INPUT}
                />
                <p className="mt-1 text-xs text-text-muted">{t("nodes.install.passwordNote")}</p>
              </div>
            )}

            {auth === "key" && (
              <div className="space-y-3">
                <div>
                  <label htmlFor="install-key" className={LABEL}>
                    {t("nodes.install.key")}
                  </label>
                  <textarea
                    id="install-key"
                    rows={4}
                    value={privateKey}
                    onChange={(e) => setPrivateKey(e.target.value)}
                    placeholder={t("nodes.install.keyPlaceholder")}
                    spellCheck={false}
                    disabled={installing}
                    className={`${INPUT} font-mono text-xs`}
                  />
                  <label className="mt-2 inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-surface-hover">
                    <Download size={14} />
                    {t("nodes.install.keyFile")}
                    <input
                      type="file"
                      aria-label={t("nodes.install.keyFile")}
                      className="sr-only"
                      onChange={(e) => void readKeyFile(e.target.files?.[0])}
                      disabled={installing}
                    />
                  </label>
                  <p className="mt-1 text-xs text-text-muted">{t("nodes.install.keyNote")}</p>
                </div>
                <div>
                  <label htmlFor="install-passphrase" className={LABEL}>
                    {t("nodes.install.passphrase")}
                  </label>
                  <input
                    id="install-passphrase"
                    type="password"
                    autoComplete="off"
                    value={passphrase}
                    onChange={(e) => setPassphrase(e.target.value)}
                    disabled={installing}
                    className={INPUT}
                  />
                  <p className="mt-1 text-xs text-text-muted">{t("nodes.install.passphraseNote")}</p>
                </div>
              </div>
            )}

            {auth === "control_plane_key" && (
              <p className="text-xs text-text-muted">{t("nodes.install.controlPlaneKeyNote")}</p>
            )}

            <div>
              <label htmlFor="install-sudo" className={LABEL}>
                {t("nodes.install.sudoPassword")}
              </label>
              <input
                id="install-sudo"
                type="password"
                autoComplete="off"
                value={sudoPassword}
                onChange={(e) => setSudoPassword(e.target.value)}
                disabled={installing}
                className={INPUT}
              />
              <p className="mt-1 text-xs text-text-muted">{t("nodes.install.sudoNote")}</p>
            </div>

            {/* The host key, before any secret. This is ssh's own first-contact
                prompt, with the fingerprint where the operator can read it. */}
            <div className="rounded-lg border border-border p-3" data-testid="host-key">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-medium">{t("nodes.install.hostKey")}</p>
                <button
                  onClick={checkHostKey}
                  disabled={checking || installing || !portValid}
                  className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-surface-hover disabled:opacity-50"
                >
                  {checking ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />}
                  {t("nodes.install.checkHostKey")}
                </button>
              </div>
              {checking && (
                <p className="mt-2 text-sm text-text-muted">{t("nodes.install.checking")}</p>
              )}
              {hostKey && (
                <div className="mt-2 space-y-1">
                  <p className="break-all font-mono text-xs" data-testid="host-key-fingerprint">
                    {hostKey.algorithm} {hostKey.fingerprint}
                  </p>
                  <p className="text-xs text-text-muted">
                    {t("nodes.install.hostKeyHint", {
                      command: "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub",
                    })}
                  </p>
                </div>
              )}
            </div>

            {installing && (
              <p className="flex items-center gap-2 text-sm text-text-muted" role="status">
                <Loader2 size={14} className="animate-spin" />
                {t("nodes.install.running")}
              </p>
            )}

            {error && (
              <div
                role="alert"
                className="flex items-center gap-2 rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger"
              >
                <AlertCircle size={16} className="shrink-0" />
                <span>{error}</span>
              </div>
            )}
          </div>
        )}

        <div className="mt-6 flex items-center justify-end gap-3 border-t border-border pt-4">
          {report ? (
            <button
              onClick={onClose}
              className="rounded-lg bg-primary px-4 py-2 text-primary-foreground transition-colors hover:bg-primary/90"
            >
              {t("nodes.install.close")}
            </button>
          ) : (
            <>
              <button
                onClick={onClose}
                disabled={installing}
                className="rounded-lg border border-border px-4 py-2 transition-colors hover:bg-surface-hover disabled:opacity-50"
              >
                {t("nodes.install.later")}
              </button>
              <button
                onClick={install}
                disabled={installing || !hostKey || !credentialsReady}
                title={hostKey ? undefined : t("nodes.install.runHint")}
                className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
              >
                {installing ? <Loader2 size={16} className="animate-spin" /> : <Download size={16} />}
                {t("nodes.install.run")}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** The installer's report, as the operator reads it: the verdict first, then
 * why it chose what it chose, then what it went ahead without. */
function InstallOutcome({ report }: { report: InstallReport }) {
  const { t } = useI18n();
  return (
    <div className="space-y-4" data-testid="install-report">
      <div
        className={`flex items-start gap-3 rounded-lg border p-3 text-sm ${
          report.connected
            ? "border-success/30 bg-success/10"
            : "border-warning/30 bg-warning/10"
        }`}
        role="status"
      >
        {report.connected ? (
          <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-success" />
        ) : (
          <AlertCircle size={16} className="mt-0.5 shrink-0 text-warning" />
        )}
        <div>
          <p className="font-medium">
            {report.connected ? t("nodes.install.done") : t("nodes.install.notConnected")}
          </p>
          {report.scope && (
            <p className="mt-1 text-text-muted">
              {t("nodes.install.scope", { scope: report.scope, reason: report.scope_reason })}
            </p>
          )}
        </div>
      </div>

      {report.concessions.length > 0 && (
        <div>
          <p className="mb-2 text-sm font-medium">{t("nodes.install.concessions")}</p>
          <div className="space-y-2">
            {report.concessions.map((c) => (
              <div
                key={c.capability}
                role="note"
                className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm"
              >
                <p className="font-medium">{c.detail}</p>
                <p className="mt-1 text-text-muted">{c.cost}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      <div>
        <p className="mb-2 text-sm font-medium">{t("nodes.install.steps")}</p>
        <ol className="list-decimal space-y-0.5 pl-5 text-xs text-text-muted">
          {report.steps.map((step, i) => (
            <li key={i}>{step}</li>
          ))}
        </ol>
        <p className="mt-2 text-xs text-text-muted">
          {t("nodes.install.privileged", { count: report.privileged_calls.length })}
        </p>
      </div>
    </div>
  );
}

export default function NodeRegistry() {
  const { t } = useI18n();
  const { data: nodes, loading, error, refetch } = useQuery<ClusterNode[]>(fetchNodes);
  const [findings, setFindings] = useState<NodeFinding[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [installing, setInstalling] = useState<ClusterNode | null>(null);
  const [forgetting, setForgetting] = useState<ClusterNode | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);

  const loadDiagnostics = useCallback(() => {
    fetchNodeDiagnostics()
      .then((result) => setFindings(result.findings))
      .catch(() => setFindings([]));
  }, []);

  useEffect(() => {
    loadDiagnostics();
  }, [loadDiagnostics]);

  const reload = useCallback(() => {
    refetch();
    loadDiagnostics();
  }, [refetch, loadDiagnostics]);

  return (
    <section
      data-testid="node-registry"
      className="rounded-xl border border-border bg-surface p-4"
    >
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-lg font-bold">
            <Network size={18} className="text-primary" />
            {t("nodes.heading")}
          </h3>
          <p className="mt-0.5 text-sm text-text-muted">
            {t("nodes.subtitle")}
          </p>
        </div>
        <button
          onClick={() => setShowAdd(true)}
          className="flex shrink-0 items-center gap-2 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-surface-hover"
        >
          <Plus size={14} />
          Add node
        </button>
      </div>

      {findings.length > 0 && (
        <div className="mb-4">
          <Diagnostics findings={findings} />
        </div>
      )}

      {loading && (
        <div className="flex justify-center py-10">
          <Loader2 className="animate-spin text-primary" size={24} />
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="flex items-center gap-3 rounded-lg border border-danger/30 bg-danger/10 p-3 text-danger"
        >
          <AlertCircle size={18} />
          <span>{error}</span>
        </div>
      )}

      {removeError && (
        <div
          role="alert"
          className="mb-3 flex items-center gap-3 rounded-lg border border-danger/30 bg-danger/10 p-3 text-danger"
        >
          <AlertCircle size={18} />
          <span>{removeError}</span>
        </div>
      )}

      {nodes && nodes.length === 0 && (
        <p className="py-8 text-center text-text-muted">
          No nodes registered yet.
        </p>
      )}

      {nodes && nodes.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-text-muted">
                <th scope="col" className="py-2 pr-4 font-semibold">{t("nodes.colName")}</th>
                <th scope="col" className="py-2 pr-4 font-semibold">{t("nodes.colAddress")}</th>
                <th scope="col" className="py-2 pr-4 font-semibold">{t("nodes.colInterfaces")}</th>
                <th scope="col" className="py-2 pr-4 font-semibold">{t("nodes.colRole")}</th>
                <th scope="col" className="py-2 pr-4 font-semibold">{t("nodes.colState")}</th>
                <th scope="col" className="py-2 font-semibold">
                  <span className="sr-only">{t("nodes.colActions")}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {nodes.map((node) => (
                <tr key={node.id} className="border-b border-border/50 last:border-0">
                  <td className="py-2.5 pr-4 font-medium">{node.name}</td>
                  <td className="py-2.5 pr-4 font-mono text-xs">{node.address || "—"}</td>
                  <td className="py-2.5 pr-4 font-mono text-xs text-text-muted">
                    {interfaceSummary(node)}
                  </td>
                  <td className="py-2.5 pr-4">
                    {node.is_control_plane ? (
                      <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide bg-primary/15 text-primary">
                        Control plane
                      </span>
                    ) : (
                      <span className="text-text-muted">{t("nodes.peer")}</span>
                    )}
                  </td>
                  <td className="py-2.5 pr-4">
                    <div className="flex flex-col items-start gap-1">
                      <NodeStateBadge state={node.state} />
                      {node.agent && !node.agent.enrolled && (
                        <span className="text-xs text-text-muted">{t("nodes.noAgent")}</span>
                      )}
                    </div>
                  </td>
                  <td className="py-2.5 text-right">
                    {!node.is_control_plane && (
                      <button
                        onClick={() => setInstalling(node)}
                        aria-label={t("nodes.install.actionFor", { name: node.name })}
                        title={
                          node.agent?.enrolled
                            ? t("nodes.install.reinstall")
                            : t("nodes.install.action")
                        }
                        className="rounded-lg p-1.5 text-text-muted transition-colors hover:bg-primary/10 hover:text-primary"
                      >
                        <Download size={14} />
                      </button>
                    )}
                    {!node.is_control_plane && (
                      <button
                        onClick={() => {
                          setRemoveError(null);
                          setForgetting(node);
                        }}
                        aria-label={`Forget ${node.name}`}
                        title={t("nodes.forget")}
                        className="rounded-lg p-1.5 text-text-muted transition-colors hover:bg-danger/10 hover:text-danger"
                      >
                        <Trash2 size={14} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showAdd && (
        <AddNodeDialog
          onClose={() => setShowAdd(false)}
          onAdded={(added) => {
            reload();
            // Registering is half of it. The agent is what makes the machine
            // reachable, so the install follows without another click to find.
            if (!added.is_control_plane) setInstalling(added);
          }}
        />
      )}

      {installing && (
        <InstallAgentDialog
          node={installing}
          onClose={() => setInstalling(null)}
          onInstalled={reload}
        />
      )}

      {forgetting && (
        <ConfirmModal
          open
          onClose={() => setForgetting(null)}
          title={t("nodes.forgetTitle")}
          message={t("nodes.forgetBody", { name: forgetting.name })}
          confirmLabel={t("nodes.forgetConfirm")}
          confirmVariant="danger"
          onConfirm={async () => {
            try {
              await removeNode(forgetting.id);
              setForgetting(null);
              reload();
            } catch (e) {
              setForgetting(null);
              setRemoveError(e instanceof Error ? e.message : "Could not forget the node");
            }
          }}
        />
      )}
    </section>
  );
}
