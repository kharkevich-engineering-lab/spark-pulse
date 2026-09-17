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
  updateNode,
  updateNodeAgent,
  discoverNodes,
  fetchNodeDiagnostics,
  fetchNodeHostKey,
  fetchNodes,
  installNodeAgent,
  removeNode,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import {
  Button,
  ConfirmModal,
  ErrorLine,
  Field,
  Input,
  Modal,
  NodeState as NodeStateBadge,
  Spinner,
  Textarea,
  type NodeCondition,
} from "@/ui";
import NodeDoctor from "@/components/NodeDoctor";
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
  Pencil,
  Stethoscope,
  Plus,
  Radar,
  Server,
  Trash2,
} from "lucide-react";

/** The registry's three words in the one node vocabulary. `unknown` is the one
 *  that matters: a node we could not reach has not failed. */
const STATE_CONDITION: Record<NodeState, NodeCondition> = {
  healthy: "ok",
  unknown: "unknown",
  dead: "bad",
};

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
            className={`flex items-start gap-3 rounded-md border p-3 text-sm ${
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
    <Modal
      open
      onClose={onClose}
      size="md"
      title={t("nodes.addNode")}
      icon={<Server size={20} className="text-blue2" />}
      actions={
        <>
          <Button size="sm" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            size="sm"
            variant="primary"
            icon={Plus}
            loading={submitting}
            disabled={!address.trim()}
            onClick={submit}
          >
            {t("nodes.addNode")}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label={t("nodes.address")}>
          {(control) => (
            <Input
              {...control}
              type="text"
              autoFocus
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder={t("nodes.addressPlaceholder")}
            />
          )}
        </Field>

        <Field label={t("nodes.name")}>
          {(control) => (
            <Input
              {...control}
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("nodes.namePlaceholder")}
            />
          )}
        </Field>

        <Field label={t("nodes.sshUser")} hint={t("nodes.addNote")}>
          {(control) => (
            <Input
              {...control}
              type="text"
              value={sshUser}
              onChange={(e) => setSshUser(e.target.value)}
              placeholder={t("nodes.sshUserPlaceholder")}
            />
          )}
        </Field>

        {/* Discovery is an aid, never a gate: the address field above always works. */}
        <div className="rounded-md border border-line p-3">
          <div className="flex items-center justify-between gap-3">
            <p className="text-[14px] font-medium">{t("nodes.find")}</p>
            <Button size="sm" icon={Radar} loading={scanning} onClick={scan}>
              {t("nodes.scan")}
            </Button>
          </div>

          {peers !== null && !mdnsAvailable && (
            <p className="mt-2 text-[14px] text-muted">{t("nodes.mdnsUnavailable")}</p>
          )}
          {peers !== null && mdnsAvailable && peers.length === 0 && (
            <p className="mt-2 text-[14px] text-muted">{t("nodes.noResponders")}</p>
          )}
          {peers !== null && peers.length > 0 && (
            <ul className="mt-2 space-y-1">
              {peers.map((peer) => (
                <li key={`${peer.address}-${peer.service}`}>
                  <button
                    type="button"
                    onClick={() => {
                      setAddress(peer.address);
                      if (!name) setName(peer.hostname.replace(/\.local$/, ""));
                    }}
                    disabled={peer.registered}
                    className="flex w-full items-center justify-between gap-3 rounded-sm px-2 py-1.5 text-left text-[14px] hover:bg-surface-hover disabled:opacity-50"
                  >
                    <span className="min-w-0 truncate">
                      <span className="font-medium">{peer.address}</span>
                      <span className="text-muted"> · {peer.hostname || "unnamed"}</span>
                    </span>
                    <span className="shrink-0 text-[13px] text-muted">
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

        <ErrorLine>{error}</ErrorLine>
      </div>
    </Modal>
  );
}

interface EditNodeDialogProps {
  node: ClusterNode;
  onClose: () => void;
  onSaved: () => void;
}

/** Fixing a mistyped name, address or SSH user without "Forget" and re-adding
 * the node, which would throw away its agent enrollment for nothing the
 * enrollment was wrong about. Only what `update_node` actually accepts is
 * offered here (`nodes.py::update_node`'s allowlist: name, address, ssh_user
 * among them) and only the fields that changed are sent.
 *
 * The control plane is a special case, the same way it already is for
 * install and forget: its address is not a place peers dial it at (its own
 * agent is reached over loopback, never SSH), so re-addressing it here would
 * invite an operator to "fix" a value nothing reads that way. Only its name
 * is offered. */
function EditNodeDialog({ node, onClose, onSaved }: EditNodeDialogProps) {
  const { t } = useI18n();
  const [name, setName] = useState(node.name);
  const [address, setAddress] = useState(node.address);
  const [sshUser, setSshUser] = useState(node.ssh_user);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmedName = name.trim();
  const trimmedAddress = address.trim();
  const canSave = trimmedName !== "" && (node.is_control_plane || trimmedAddress !== "");

  const submit = async () => {
    const changes: Partial<ClusterNode> = {};
    if (trimmedName !== node.name) changes.name = trimmedName;
    if (!node.is_control_plane) {
      if (trimmedAddress !== node.address) changes.address = trimmedAddress;
      if (sshUser.trim() !== node.ssh_user) changes.ssh_user = sshUser.trim();
    }
    if (Object.keys(changes).length === 0) {
      onClose();
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await updateNode(node.id, changes);
      onSaved();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update the node");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={t("nodes.edit.title", { name: node.name })}
      icon={<Pencil size={20} className="text-blue2" />}
      actions={
        <>
          <Button size="sm" onClick={onClose} disabled={submitting}>
            {t("common.cancel")}
          </Button>
          <Button
            size="sm"
            variant="primary"
            loading={submitting}
            disabled={!canSave}
            onClick={submit}
          >
            {submitting ? t("common.saving") : t("common.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label={t("nodes.name")}>
          {(control) => (
            <Input
              {...control}
              type="text"
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={submitting}
            />
          )}
        </Field>

        {node.is_control_plane ? (
          <p className="text-[13px] text-muted">{t("nodes.edit.controlPlaneNote")}</p>
        ) : (
          <>
            <Field label={t("nodes.address")}>
              {(control) => (
                <Input
                  {...control}
                  type="text"
                  value={address}
                  onChange={(e) => setAddress(e.target.value)}
                  disabled={submitting}
                />
              )}
            </Field>
            <Field label={t("nodes.sshUser")}>
              {(control) => (
                <Input
                  {...control}
                  type="text"
                  value={sshUser}
                  onChange={(e) => setSshUser(e.target.value)}
                  disabled={submitting}
                />
              )}
            </Field>
          </>
        )}

        <ErrorLine>{error}</ErrorLine>
      </div>
    </Modal>
  );
}

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
  // A node that already has an agent is reached with the key the install
  // left behind; an update is a reinstall over that key, no password asked.
  const updating = Boolean(node.agent?.enrolled);
  const [auth, setAuth] = useState<NodeAuthMethod>(updating ? "control_plane_key" : "password");
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

  const title = updating
    ? `${t("nodes.install.update")}: ${node.name || node.address}`
    : t("nodes.install.title", { name: node.name || node.address });

  return (
    <Modal
      open
      onClose={onClose}
      size="md"
      title={title}
      icon={<KeyRound size={20} className="text-blue2" />}
      actions={
        report ? (
          <Button size="sm" variant="primary" onClick={onClose}>
            {t("nodes.install.close")}
          </Button>
        ) : (
          <>
            <Button size="sm" onClick={onClose} disabled={installing}>
              {t("nodes.install.later")}
            </Button>
            <Button
              size="sm"
              variant="primary"
              icon={Download}
              loading={installing}
              disabled={!hostKey || !credentialsReady}
              title={hostKey ? undefined : t("nodes.install.runHint")}
              onClick={install}
            >
              {t("nodes.install.run")}
            </Button>
          </>
        )
      }
    >
      <>
        {report ? (
          <InstallOutcome report={report} />
        ) : (
          <div className="space-y-4">
            <p className="text-[14px] text-muted">
              {updating
                ? t("nodes.install.updateHint", {
                    version: node.agent?.version || "?",
                    current: node.agent?.control_plane_version || "?",
                  })
                : t("nodes.install.intro", { address: node.address })}
            </p>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-[1fr_7rem]">
              <Field label={t("nodes.install.username")}>
                {(control) => (
                  <Input
                    {...control}
                    type="text"
                    autoFocus
                    autoComplete="username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder={t("nodes.sshUserPlaceholder")}
                    disabled={installing}
                  />
                )}
              </Field>
              <Field label={t("nodes.install.port")}>
                {(control) => (
                  <Input
                    {...control}
                    type="number"
                    min={1}
                    max={65535}
                    value={port}
                    onChange={(e) => {
                      setPort(e.target.value);
                      setHostKey(null);
                    }}
                    disabled={installing}
                  />
                )}
              </Field>
            </div>

            <fieldset>
              <legend className="block text-[13px] font-medium text-text mb-1.5">
                {t("nodes.install.auth")}
              </legend>
              <div className="flex flex-wrap gap-4 text-[14px]">
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
              <Field label={t("nodes.install.password")} hint={t("nodes.install.passwordNote")}>
                {(control) => (
                  <Input
                    {...control}
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    disabled={installing}
                  />
                )}
              </Field>
            )}

            {auth === "key" && (
              <div className="space-y-3">
                <Field label={t("nodes.install.key")} hint={t("nodes.install.keyNote")}>
                  {(control) => (
                    <>
                      <Textarea
                        {...control}
                        mono
                        rows={4}
                        value={privateKey}
                        onChange={(e) => setPrivateKey(e.target.value)}
                        placeholder={t("nodes.install.keyPlaceholder")}
                        spellCheck={false}
                        disabled={installing}
                      />
                      <label className="mt-2 inline-flex cursor-pointer items-center gap-1.5 rounded-sm border border-line px-3 py-[7px] text-[13px] font-semibold hover:border-line-strong">
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
                    </>
                  )}
                </Field>
                <Field
                  label={t("nodes.install.passphrase")}
                  hint={t("nodes.install.passphraseNote")}
                >
                  {(control) => (
                    <Input
                      {...control}
                      type="password"
                      autoComplete="off"
                      value={passphrase}
                      onChange={(e) => setPassphrase(e.target.value)}
                      disabled={installing}
                    />
                  )}
                </Field>
              </div>
            )}

            {auth === "control_plane_key" && (
              <p className="text-[13px] text-muted">{t("nodes.install.controlPlaneKeyNote")}</p>
            )}

            <Field label={t("nodes.install.sudoPassword")} hint={t("nodes.install.sudoNote")}>
              {(control) => (
                <Input
                  {...control}
                  type="password"
                  autoComplete="off"
                  value={sudoPassword}
                  onChange={(e) => setSudoPassword(e.target.value)}
                  disabled={installing}
                />
              )}
            </Field>

            {/* The host key, before any secret. This is ssh's own first-contact
                prompt, with the fingerprint where the operator can read it. */}
            <div className="rounded-md border border-line p-3" data-testid="host-key">
              <div className="flex items-center justify-between gap-3">
                <p className="text-[14px] font-medium">{t("nodes.install.hostKey")}</p>
                <Button
                  size="sm"
                  icon={KeyRound}
                  loading={checking}
                  disabled={installing || !portValid}
                  onClick={checkHostKey}
                >
                  {t("nodes.install.checkHostKey")}
                </Button>
              </div>
              {checking && <p className="mt-2 text-[14px] text-muted">{t("nodes.install.checking")}</p>}
              {hostKey && (
                <div className="mt-2 space-y-1">
                  <p className="break-all font-mono text-[12.5px]" data-testid="host-key-fingerprint">
                    {hostKey.algorithm} {hostKey.fingerprint}
                  </p>
                  <p className="text-[13px] text-muted">
                    {t("nodes.install.hostKeyHint", {
                      command: "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub",
                    })}
                  </p>
                </div>
              )}
            </div>

            {installing && (
              <p className="flex items-center gap-2 text-[14px] text-muted" role="status">
                <Spinner size="sm" />
                {t("nodes.install.running")}
              </p>
            )}

            <ErrorLine>{error}</ErrorLine>
          </div>
        )}
      </>
    </Modal>
  );
}

/** The installer's report, as the operator reads it: the verdict first, then
 * why it chose what it chose, then what it went ahead without. */
function InstallOutcome({ report }: { report: InstallReport }) {
  const { t } = useI18n();
  return (
    <div className="space-y-4" data-testid="install-report">
      <div
        className={`flex items-start gap-3 rounded-sm border p-3 text-sm ${
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
                className="rounded-sm border border-warning/30 bg-warning/10 p-3 text-sm"
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
  const [editing, setEditing] = useState<ClusterNode | null>(null);
  const [diagnosing, setDiagnosing] = useState<ClusterNode | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);
  const [updateError, setUpdateError] = useState<string | null>(null);
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

  const runUpdate = useCallback(
    async (node: ClusterNode) => {
      setUpdateError(null);
      setUpdating(node.id);
      try {
        const result = await updateNodeAgent(node.id);
        if (result.needs_reinstall) {
          // The agent is too old to update itself over its stream. Hand the
          // operator straight to the reinstall dialog, which defaults to the
          // control-plane key already trusted on the node — one confirmation,
          // no password, and the first hop onto a self-updating agent is done.
          setInstalling(node);
          return;
        }
        if (!result.updated) {
          setUpdateError(`${node.name}: ${result.detail || "update failed"}`);
        }
        // The agent restarts onto the new binary; give it a moment, then reload
        // so the row reflects the new version once it reconnects.
        setTimeout(reload, 4000);
      } catch (e) {
        setUpdateError(e instanceof Error ? e.message : "Update failed");
      } finally {
        setUpdating(null);
      }
    },
    [reload],
  );

  return (
    <section
      data-testid="node-registry"
      className="rounded-md border border-border bg-surface p-4"
    >
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-lg font-bold">
            <Network size={18} className="text-blue2" />
            {t("nodes.heading")}
          </h3>
          <p className="mt-0.5 text-sm text-text-muted">
            {t("nodes.subtitle")}
          </p>
        </div>
        <button
          onClick={() => setShowAdd(true)}
          className="flex shrink-0 items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm transition-colors hover:bg-surface-hover"
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
          <Loader2 className="animate-spin text-blue2" size={24} />
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="flex items-center gap-3 rounded-sm border border-danger/30 bg-danger/10 p-3 text-danger"
        >
          <AlertCircle size={18} />
          <span>{error}</span>
        </div>
      )}

      {updateError && (
        <div
          role="alert"
          className="mb-3 flex items-center gap-3 rounded-sm border border-danger/30 bg-danger/10 p-3 text-danger"
        >
          <AlertCircle size={18} />
          <span>{updateError}</span>
        </div>
      )}

      {removeError && (
        <div
          role="alert"
          className="mb-3 flex items-center gap-3 rounded-sm border border-danger/30 bg-danger/10 p-3 text-danger"
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
                    <div className="flex flex-col items-start gap-1">
                      {node.is_control_plane ? (
                        <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide bg-primary/15 text-blue2">
                          Control plane
                        </span>
                      ) : (
                        <span className="text-text-muted">{t("nodes.peer")}</span>
                      )}
                      {node.agent?.version && (
                        <span
                          className={`text-xs ${node.agent.current === false ? "text-warning" : "text-text-muted"}`}
                          data-testid={`agent-version-${node.id}`}
                        >
                          {t("nodes.agentVersion", { version: node.agent.version })}
                          {node.agent.current === false && ` · ${t("nodes.agentStale")}`}
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="py-2.5 pr-4">
                    <div className="flex flex-col items-start gap-1">
                      <NodeStateBadge state={STATE_CONDITION[node.state] ?? "unknown"} />
                      {node.agent && !node.agent.enrolled && (
                        <span className="text-xs text-text-muted">{t("nodes.noAgent")}</span>
                      )}
                    </div>
                  </td>
                  <td className="py-2.5 text-right">
                    {!node.is_control_plane && node.agent?.current === false ? (
                      <button
                        onClick={() => void runUpdate(node)}
                        disabled={updating === node.id}
                        aria-label={t("nodes.install.actionFor", { name: node.name })}
                        title={t("nodes.install.update")}
                        className="mr-1 inline-flex items-center gap-1.5 rounded-sm border border-warning/40 bg-warning/10 px-2.5 py-1 text-xs font-medium text-warning transition-colors hover:bg-warning/20 disabled:opacity-50"
                      >
                        {updating === node.id ? (
                          <Loader2 size={13} className="animate-spin" />
                        ) : (
                          <Download size={13} />
                        )}
                        {updating === node.id ? t("nodes.updating") : t("nodes.updateAction")}
                      </button>
                    ) : (
                      !node.is_control_plane && (
                        <button
                          onClick={() => setInstalling(node)}
                          aria-label={t("nodes.install.actionFor", { name: node.name })}
                          title={
                            node.agent?.enrolled
                              ? t("nodes.install.reinstall")
                              : t("nodes.install.action")
                          }
                          className="rounded-sm p-1.5 text-text-muted transition-colors hover:bg-primary/10 hover:text-blue2"
                        >
                          <Download size={14} />
                        </button>
                      )
                    )}
                    <button
                      onClick={() => setEditing(node)}
                      aria-label={t("nodes.edit.actionFor", { name: node.name })}
                      title={t("nodes.edit.action")}
                      className="rounded-sm p-1.5 text-text-muted transition-colors hover:bg-primary/10 hover:text-blue2"
                    >
                      <Pencil size={14} />
                    </button>
                    <button
                      onClick={() => setDiagnosing(node)}
                      aria-label={t("nodes.doctor.actionFor", { name: node.name })}
                      title={t("nodes.doctor.action")}
                      className="rounded-sm p-1.5 text-text-muted transition-colors hover:bg-primary/10 hover:text-blue2"
                    >
                      <Stethoscope size={14} />
                    </button>
                    {!node.is_control_plane && (
                      <button
                        onClick={() => {
                          setRemoveError(null);
                          setForgetting(node);
                        }}
                        aria-label={`Forget ${node.name}`}
                        title={t("nodes.forget")}
                        className="rounded-sm p-1.5 text-text-muted transition-colors hover:bg-danger/10 hover:text-danger"
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

      {editing && (
        <EditNodeDialog
          node={editing}
          onClose={() => setEditing(null)}
          onSaved={reload}
        />
      )}

      {diagnosing && (
        <NodeDoctor
          node={diagnosing}
          onClose={() => setDiagnosing(null)}
          onChanged={reload}
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
