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
import { Link } from "react-router-dom";
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
  EmptyState,
  ErrorLine,
  Field,
  Input,
  Modal,
  NodeState as NodeStateBadge,
  Spinner,
  Textarea,
  InfoTip,
  Section,
  Table,
  THead,
  Th,
  Tr,
  Td,
  IconButton,
  type NodeCondition,
} from "@/ui";
import { UsageBar } from "@/components/nodes/NodeLive";
import { formatMb, type MemoryLayout } from "@/lib/memoryLayout";
import type {
  ClusterNode,
  DiscoveredPeer,
  InstallReport,
  NodeAuthMethod,
  NodeFinding,
  NodeHostKey,
  NodeState,
  NodeStats,
} from "@/lib/types";
import {
  AlertCircle,
  CheckCircle2,
  Download,
  KeyRound,
  Pencil,
  Plus,
  Radar,
  Server,
  Trash2,
} from "lucide-react";

/** The registry's three words in the one node vocabulary. `unknown` is the one
 *  that matters: a node we could not reach has not failed. */
export const STATE_CONDITION: Record<NodeState, NodeCondition> = {
  healthy: "ok",
  unknown: "unknown",
  dead: "bad",
};

export function interfaceSummary(node: ClusterNode): string {
  const names = [node.ethernet_interface, ...node.infiniband_interfaces].filter(Boolean);
  return names.length > 0 ? names.join(", ") : "—";
}

/** A finding, as one line.
 *
 * Never an error and never a yellow box: each of these is a condition the
 * cluster *runs with*, and each costs an afternoon when it is not named. A
 * warning is the warn colour and an informational one is muted — the same
 * vocabulary the node's own state uses, so an operator is not learning a
 * second palette to read the same list.
 *
 * A finding that names nodes is rendered on each of those nodes' rows, where
 * the machine it is about is; one that names none is rendered above the list,
 * because it is about the fleet rather than a machine. */
export function FindingLine({ finding }: { finding: NodeFinding }) {
  const { t } = useI18n();
  return (
    <p
      role="note"
      className={`flex items-start gap-1.5 text-[13px] ${finding.severity === "warning" ? "text-warn" : "text-muted"}`}
    >
      <span className="min-w-0">{finding.summary}</span>
      {finding.remedy && (
        <InfoTip label={t("nodesPage.howToFix")}>
          <span className="break-words font-mono text-[12px]">{finding.remedy}</span>
        </InfoTip>
      )}
    </p>
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
      setError(e instanceof Error ? e.message : t("nodes.addFailed"));
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
                      <span className="text-muted"> · {peer.hostname || t("fleet.peerUnnamed")}</span>
                    </span>
                    <span className="shrink-0 text-[13px] text-muted">
                      {peer.registered
                        ? t("fleet.peerRegistered")
                        : peer.is_spark_pulse
                          ? t("fleet.peerSparkPulse", { version: peer.version })
                          : t("fleet.peerSshOnly")}
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
      setError(e instanceof Error ? e.message : t("nodes.updateNodeFailed"));
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
      setError(e instanceof Error ? e.message : t("nodes.hostKeyFailed"));
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
      setError(e instanceof Error ? e.message : t("nodes.installFailed"));
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

export interface NodeRegistryProps {
  /** The add dialog, when the page owns the button. Left undefined the
   *  registry renders its own — which is what a test, or any other caller,
   *  gets. */
  addOpen?: boolean;
  onAddOpenChange?: (open: boolean) => void;
  /** Each node's own live stats, from the page. Absent, the live columns say
   *  nothing rather than zero. */
  stats?: NodeStats[];
  /** Memory cut by run, per node id. */
  layouts?: Record<string, MemoryLayout | null>;
}

/** What a node's agent is, in one cell. */
export function AgentCell({ node }: { node: ClusterNode }) {
  const { t } = useI18n();
  const stale = node.agent?.current === false;
  if (node.agent && !node.agent.enrolled) return <span className="text-muted">{t("nodes.noAgent")}</span>;
  if (!node.agent?.version) return <span className="text-muted">—</span>;
  return (
    <span data-testid={`agent-version-${node.id}`} className={stale ? "text-warn" : "text-muted"}>
      {node.agent.version}
      {stale && ` · ${t("nodes.agentStale")}`}
    </span>
  );
}

/** The memory cell: a thin bar and how much of the total is in use. */
function MemoryCell({ stats, layout }: { stats?: NodeStats; layout?: MemoryLayout | null }) {
  const { t } = useI18n();
  if (!stats) return <span className="text-muted">—</span>;
  if (!stats.reachable) return <span className="text-muted" title={stats.error ?? undefined}>{t("monitoring.unreachable")}</span>;
  if (!layout || layout.totalMb <= 0) return <span className="text-muted">—</span>;
  return (
    <div className="min-w-[140px] space-y-1">
      <UsageBar percent={(layout.usedMb / layout.totalMb) * 100} />
      <p className="font-mono text-[11.5px] text-muted tabular-nums">
        {formatMb(layout.usedMb)} / {formatMb(layout.totalMb)}
      </p>
    </div>
  );
}

export interface RowActions {
  updating: boolean;
  onUpdate: () => void;
  onInstall: () => void;
  onEdit: () => void;
  onForget: () => void;
}

/** Install/update, edit, forget — as buttons on the node's page, icons in the row. */
export function NodeActions({ node, compact, updating, onUpdate, onInstall, onEdit, onForget }: RowActions & { node: ClusterNode; compact?: boolean }) {
  const { t } = useI18n();
  const stale = node.agent?.current === false;
  const agentLabel = stale
    ? updating
      ? t("nodes.updating")
      : t("nodes.updateAction")
    : node.agent?.enrolled
      ? t("nodes.install.reinstall")
      : t("nodes.install.action");
  const items = [
    !node.is_control_plane && {
      key: "agent",
      icon: Download,
      label: agentLabel,
      title: stale ? t("nodes.install.update") : node.agent?.enrolled ? t("nodes.install.reinstall") : t("nodes.install.action"),
      aria: t("nodes.install.actionFor", { name: node.name }),
      onClick: stale ? onUpdate : onInstall,
      loading: stale && updating,
      danger: false,
    },
    { key: "edit", icon: Pencil, label: t("nodes.edit.action"), title: t("nodes.edit.action"), aria: t("nodes.edit.actionFor", { name: node.name }), onClick: onEdit, loading: false, danger: false },
    !node.is_control_plane && {
      key: "forget",
      icon: Trash2,
      label: t("nodes.forgetConfirm"),
      title: t("nodes.forget"),
      aria: t("fleet.forgetFor", { name: node.name }),
      onClick: onForget,
      loading: false,
      danger: true,
    },
  ].filter(Boolean) as { key: string; icon: typeof Download; label: string; title: string; aria: string; onClick: () => void; loading: boolean; danger: boolean }[];

  if (compact) {
    return (
      <>
        {items.map((a) => (
          <IconButton
            key={a.key}
            size="sm"
            icon={a.icon}
            label={a.aria}
            title={a.title}
            loading={a.loading}
            variant={a.danger ? "danger" : "ghost"}
            onClick={a.onClick}
          />
        ))}
      </>
    );
  }
  return (
    <>
      {items.map((a) => (
        <Button key={a.key} size="sm" icon={a.icon} loading={a.loading} variant={a.danger ? "danger" : undefined} onClick={a.onClick} title={a.title}>
          {a.label}
        </Button>
      ))}
    </>
  );
}

/** What can be done to a node, and the dialogs that do it.
 *
 * The table's row icons and the node page's buttons are the same four
 * actions; this holds their state once so both places open the same dialogs.
 * `reload` is whatever re-reads the nodes; `onForgotten` runs after a node is
 * forgotten (the node's page leaves for the list).
 */
export function useNodeActions({ reload, onForgotten }: { reload: () => void; onForgotten?: (node: ClusterNode) => void }) {
  const { t } = useI18n();
  const [installing, setInstalling] = useState<ClusterNode | null>(null);
  const [editing, setEditing] = useState<ClusterNode | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);
  const [forgetting, setForgetting] = useState<ClusterNode | null>(null);
  const [error, setError] = useState<string | null>(null);

  const runUpdate = useCallback(
    async (node: ClusterNode) => {
      setError(null);
      setUpdating(node.id);
      try {
        const result = await updateNodeAgent(node.id);
        if (result.needs_reinstall) {
          // The agent is too old to update itself over its stream. Hand the
          // operator straight to the reinstall dialog, which defaults to the
          // control-plane key already trusted on the node.
          setInstalling(node);
          return;
        }
        if (!result.updated) {
          setError(`${node.name}: ${result.detail || t("nodes.updateFailed")}`);
        }
        // The agent restarts onto the new binary; reload once it reconnects.
        setTimeout(reload, 4000);
      } catch (e) {
        setError(e instanceof Error ? e.message : t("nodes.updateFailed"));
      } finally {
        setUpdating(null);
      }
    },
    [reload, t],
  );

  const actionsFor = (node: ClusterNode): RowActions => ({
    updating: updating === node.id,
    onUpdate: () => void runUpdate(node),
    onInstall: () => setInstalling(node),
    onEdit: () => setEditing(node),
    onForget: () => {
      setError(null);
      setForgetting(node);
    },
  });

  const dialogs = (
    <>
      {installing && <InstallAgentDialog node={installing} onClose={() => setInstalling(null)} onInstalled={reload} />}
      {editing && <EditNodeDialog node={editing} onClose={() => setEditing(null)} onSaved={reload} />}
      {forgetting && (
        <ConfirmModal
          open
          onClose={() => setForgetting(null)}
          title={t("nodes.forgetTitle")}
          message={t("nodes.forgetBody", { name: forgetting.name })}
          confirmLabel={t("nodes.forgetConfirm")}
          confirmVariant="danger"
          onConfirm={async () => {
            const node = forgetting;
            try {
              await removeNode(node.id);
              setForgetting(null);
              reload();
              onForgotten?.(node);
            } catch (e) {
              setForgetting(null);
              setError(e instanceof Error ? e.message : t("nodes.forgetFailed"));
            }
          }}
        />
      )}
    </>
  );

  return { actionsFor, dialogs, error, openInstall: setInstalling };
}

export default function NodeRegistry({ addOpen, onAddOpenChange, stats, layouts }: NodeRegistryProps = {}) {
  const { t } = useI18n();
  const { data: nodes, loading, error, refetch } = useQuery<ClusterNode[]>(fetchNodes);
  const [findings, setFindings] = useState<NodeFinding[]>([]);
  const [ownAdd, setOwnAdd] = useState(false);

  // Controlled when the page owns the button, uncontrolled otherwise, so the
  // same component works with a page header above it and on its own.
  const controlled = addOpen !== undefined;
  const showAdd = addOpen ?? ownAdd;
  const setShowAdd = onAddOpenChange ?? setOwnAdd;

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

  const { actionsFor, dialogs, error: actionError, openInstall } = useNodeActions({ reload });

  /** A finding that names no node, or several, is about the fleet: said once
   *  above the list. One that names a single node is on that node's page, and
   *  marked on its row. */
  const fleetFindings = findings.filter((f) => f.node_ids.length !== 1);
  const findingsFor = (id: string) => findings.filter((f) => f.node_ids.length === 1 && f.node_ids[0] === id);
  const statsFor = (id: string) => stats?.find((s) => s.id === id);

  return (
    <div data-testid="node-registry" className="space-y-3">
      {fleetFindings.length > 0 && (
        <div className="space-y-1 rounded-md border border-warn/40 bg-warn/10 px-3 py-2" data-testid="node-diagnostics">
          {fleetFindings.map((finding) => (
            <FindingLine key={finding.code} finding={finding} />
          ))}
        </div>
      )}

      <ErrorLine>{error}</ErrorLine>
      <ErrorLine>{actionError}</ErrorLine>

      <Section
        flush
        title={t("nodes.heading")}
        meta={nodes ? String(nodes.length) : undefined}
        actions={
          !controlled && (
            <Button size="sm" icon={Plus} onClick={() => setShowAdd(true)}>
              {t("nodes.addNode")}
            </Button>
          )
        }
      >
        {loading && (
          <div className="flex justify-center py-10">
            <Spinner size="lg" label={t("common.loading")} />
          </div>
        )}
        {nodes && nodes.length === 0 && <EmptyState icon={Server}>{t("nodes.none")}</EmptyState>}
        {nodes && nodes.length > 0 && (
          <Table>
            <THead>
              <Th>{t("nodesPage.colNode")}</Th>
              <Th>{t("nodesPage.colState")}</Th>
              <Th>{t("nodesPage.colAddress")}</Th>
              <Th>{t("nodesPage.colMemory")}</Th>
              <Th numeric>{t("nodesPage.colGpu")}</Th>
              <Th numeric>{t("nodesPage.colTemp")}</Th>
              <Th>{t("nodesPage.agent")}</Th>
              <Th srOnly>{t("runs.actions")}</Th>
            </THead>
            <tbody>
              {nodes.map((node) => {
                const live = statsFor(node.id);
                const gpu = live?.reachable ? live.gpu[0] : undefined;
                const nodeFindings = findingsFor(node.id);
                return (
                  <Tr key={node.id} aria-label={node.name} data-testid={`node-row-${node.id}`}>
                    <Td>
                      <div className="flex items-center gap-2 whitespace-nowrap">
                        <Link to={`/cluster/${encodeURIComponent(node.id)}`} className="font-semibold text-text hover:text-blue2">
                          {node.name}
                        </Link>
                        {node.is_control_plane && (
                          <span className="rounded-full border border-line px-1.5 py-px text-[11px] text-muted">
                            {t("nodes.controlPlane")}
                          </span>
                        )}
                        {nodeFindings.some((f) => f.severity === "warning") && (
                          <span className="h-1.5 w-1.5 rounded-full bg-warn" title={nodeFindings.map((f) => f.summary).join("\n")} />
                        )}
                      </div>
                    </Td>
                    <Td className="whitespace-nowrap">
                      <NodeStateBadge state={STATE_CONDITION[node.state] ?? "unknown"} />
                    </Td>
                    <Td mono muted className="whitespace-nowrap">
                      {node.address || "—"}
                    </Td>
                    <Td>
                      <MemoryCell stats={live} layout={layouts?.[node.id]} />
                    </Td>
                    <Td numeric>{gpu?.utilization != null ? `${gpu.utilization}%` : "—"}</Td>
                    <Td numeric muted>
                      {gpu?.temperature != null ? `${gpu.temperature}°` : "—"}
                    </Td>
                    <Td className="whitespace-nowrap text-[12.5px]">
                      <AgentCell node={node} />
                    </Td>
                    <Td actions>
                      <NodeActions node={node} compact {...actionsFor(node)} />
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Section>

      {showAdd && (
        <AddNodeDialog
          onClose={() => setShowAdd(false)}
          onAdded={(added) => {
            reload();
            // Registering is half of it: the install follows without another click.
            if (!added.is_control_plane) openInstall(added);
          }}
        />
      )}

      {dialogs}
    </div>
  );
}
