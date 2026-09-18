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

import { useCallback, useEffect, useRef, useState } from "react";
import { useI18n } from "@/lib/i18n";
import {
  addNode,
  updateNode,
  updateNodeAgent,
  discoverNodes,
  fetchFabric,
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
  type NodeCondition,
} from "@/ui";
import NodeDoctor from "@/components/NodeDoctor";
import { NodeFabricPorts } from "@/components/FabricCard";
import type {
  ClusterNode,
  DiscoveredPeer,
  FabricResponse,
  InstallReport,
  NodeAuthMethod,
  NodeFinding,
  NodeHostKey,
  NodeState,
} from "@/lib/types";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
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
const STATE_CONDITION: Record<NodeState, NodeCondition> = {
  healthy: "ok",
  unknown: "unknown",
  dead: "bad",
};

function interfaceSummary(node: ClusterNode): string {
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
function FindingLine({ finding }: { finding: NodeFinding }) {
  return (
    <p
      role="note"
      className={`text-[13px] ${finding.severity === "warning" ? "text-warn" : "text-muted"}`}
    >
      {finding.summary} <span className="text-muted">{finding.remedy}</span>
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

/** The rule that separates one section of the page from the next. */
const SECTION = "border-t border-line pt-12 mt-10 first:border-t-0 first:pt-0 first:mt-0";

export interface NodeRegistryProps {
  /** The add dialog, when the page owns the button. Left undefined the
   *  registry renders its own — which is what a test, or any other caller,
   *  gets. */
  addOpen?: boolean;
  onAddOpenChange?: (open: boolean) => void;
}

/** One machine: two lines and its actions, with the detail folded away.
 *
 * Line one is the name and whether it is answering — the two things an
 * operator scans a list of machines for. Line two is everything that
 * identifies it: address, role, interfaces, agent version. What a node's
 * cables are doing and what the doctor makes of it are a fold below, because
 * they are a question about one machine and this list is about all of them.
 */
function NodeRow({
  node,
  findings,
  expanded,
  onToggle,
  fabric,
  updating,
  onUpdate,
  onInstall,
  onEdit,
  onForget,
  onChanged,
}: {
  node: ClusterNode;
  findings: NodeFinding[];
  expanded: boolean;
  onToggle: () => void;
  fabric: FabricResponse | null;
  updating: boolean;
  onUpdate: () => void;
  onInstall: () => void;
  onEdit: () => void;
  onForget: () => void;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const stale = node.agent?.current === false;
  const fabricNode = fabric?.nodes.find((n) => n.node_id === node.id);
  const fabricPlan = fabric?.plan?.nodes.find((n) => n.node_id === node.id);

  return (
    <div
      role="listitem"
      aria-label={node.name}
      data-testid={`node-row-${node.id}`}
      className="py-4 space-y-3"
    >
      {/* Line 1 — who, and whether it answers. */}
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          aria-label={t("fleet.detailsFor", { name: node.name })}
          className="inline-flex items-center gap-1.5 text-[17px] font-semibold tracking-[-0.02em] hover:text-blue2 transition-colors duration-200"
        >
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          {node.name}
        </button>
        <NodeStateBadge state={STATE_CONDITION[node.state] ?? "unknown"} />
      </div>

      {/* Whatever the fleet diagnostic says about *this* machine — a warn
          line under its name, not a box at the top of a list of machines it
          may not even be about. */}
      {findings.length > 0 && (
        <div className="space-y-1" data-testid={`node-finding-${node.id}`}>
          {findings.map((finding) => (
            <FindingLine key={finding.code} finding={finding} />
          ))}
        </div>
      )}

      {/* Line 2 — what identifies it. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[13px] text-muted">
        <span className="font-mono">{node.address || "—"}</span>
        <span className="rounded-full border border-line px-2 py-0.5">
          {node.is_control_plane ? t("nodes.controlPlane") : t("nodes.peer")}
        </span>
        <span className="font-mono">{interfaceSummary(node)}</span>
        {node.agent?.version && (
          <span data-testid={`agent-version-${node.id}`} className={stale ? "text-warn" : undefined}>
            {t("nodes.agentVersion", { version: node.agent.version })}
            {" · "}
            {stale ? t("nodes.agentStale") : t("fleet.agentCurrent")}
          </span>
        )}
        {node.agent && !node.agent.enrolled && <span>{t("nodes.noAgent")}</span>}
      </div>

      {/* The actions: full-width thirds under 900 so a thumb has a target, and
          one column under 520, where a third of the screen is narrower than
          the word "Install agent" and the row would scroll sideways. */}
      <div className="grid grid-cols-1 gap-2 min-[520px]:grid-cols-3 min-[900px]:flex min-[900px]:flex-wrap">
        {!node.is_control_plane &&
          (stale ? (
            <Button
              size="sm"
              icon={Download}
              loading={updating}
              onClick={onUpdate}
              aria-label={t("nodes.install.actionFor", { name: node.name })}
              title={t("nodes.install.update")}
            >
              {updating ? t("nodes.updating") : t("nodes.updateAction")}
            </Button>
          ) : (
            <Button
              size="sm"
              icon={Download}
              onClick={onInstall}
              aria-label={t("nodes.install.actionFor", { name: node.name })}
              title={node.agent?.enrolled ? t("nodes.install.reinstall") : t("nodes.install.action")}
            >
              {node.agent?.enrolled ? t("nodes.install.reinstall") : t("nodes.install.action")}
            </Button>
          ))}
        <Button
          size="sm"
          icon={Pencil}
          onClick={onEdit}
          aria-label={t("nodes.edit.actionFor", { name: node.name })}
          title={t("nodes.edit.action")}
        >
          {t("nodes.edit.action")}
        </Button>
        {!node.is_control_plane && (
          <Button
            size="sm"
            variant="danger"
            icon={Trash2}
            onClick={onForget}
            aria-label={t("fleet.forgetFor", { name: node.name })}
            title={t("nodes.forget")}
          >
            {t("nodes.forgetConfirm")}
          </Button>
        )}
      </div>

      {expanded && (
        <div className="space-y-6 pt-2">
          <div className="space-y-2">
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
              {t("fleet.fabricPorts")}
            </p>
            <NodeFabricPorts node={fabricNode} plan={fabricPlan} />
          </div>
          <div className="space-y-2">
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
              {t("fleet.doctorSection")}
            </p>
            {/* The doctor runs because the row was opened. Diagnosis changes
                nothing, so there is no reason to make an operator press a
                second button to be told what is wrong. */}
            <NodeDoctor node={node} onChanged={onChanged} />
          </div>
        </div>
      )}
    </div>
  );
}

export default function NodeRegistry({ addOpen, onAddOpenChange }: NodeRegistryProps = {}) {
  const { t } = useI18n();
  const { data: nodes, loading, error, refetch } = useQuery<ClusterNode[]>(fetchNodes);
  const [findings, setFindings] = useState<NodeFinding[]>([]);
  const [ownAdd, setOwnAdd] = useState(false);
  const [installing, setInstalling] = useState<ClusterNode | null>(null);
  const [editing, setEditing] = useState<ClusterNode | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [fabric, setFabric] = useState<FabricResponse | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [forgetting, setForgetting] = useState<ClusterNode | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);

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

  /** The fabric is read the first time a row is opened, and not before: a
   *  list of machines does not need to know what every cable is doing. */
  const fabricAsked = useRef(false);
  const toggle = useCallback((id: string) => {
    setExpanded((current) => (current === id ? null : id));
    if (fabricAsked.current) return;
    fabricAsked.current = true;
    fetchFabric()
      .then(setFabric)
      .catch(() => {
        // A fabric nobody can read is said so by the ports section itself.
      });
  }, []);

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
          setUpdateError(`${node.name}: ${result.detail || t("nodes.updateFailed")}`);
        }
        // The agent restarts onto the new binary; give it a moment, then reload
        // so the row reflects the new version once it reconnects.
        setTimeout(reload, 4000);
      } catch (e) {
        setUpdateError(e instanceof Error ? e.message : t("nodes.updateFailed"));
      } finally {
        setUpdating(null);
      }
    },
    [reload],
  );

  /** A finding that names no node is about the fleet, not a machine. */
  const fleetFindings = findings.filter((f) => f.node_ids.length === 0);

  return (
    <section data-testid="node-registry" className={SECTION}>
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-[22px] font-bold tracking-[-0.02em]">{t("nodes.heading")}</h2>
          <p className="mt-1 text-[13px] text-muted">{t("nodes.subtitle")}</p>
        </div>
        {!controlled && (
          <Button size="sm" icon={Plus} onClick={() => setShowAdd(true)}>
            {t("nodes.addNode")}
          </Button>
        )}
      </div>

      {fleetFindings.length > 0 && (
        <div className="mb-4 space-y-1" data-testid="node-diagnostics">
          {fleetFindings.map((finding) => (
            <FindingLine key={finding.code} finding={finding} />
          ))}
        </div>
      )}

      <ErrorLine className="mb-3">{error}</ErrorLine>
      <ErrorLine className="mb-3">{updateError}</ErrorLine>
      <ErrorLine className="mb-3">{removeError}</ErrorLine>

      {loading && (
        <div className="flex justify-center py-10">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}

      {nodes && nodes.length === 0 && (
        <EmptyState icon={Server}>{t("nodes.none")}</EmptyState>
      )}

      {nodes && nodes.length > 0 && (
        <div role="list" className="border-y border-line divide-y divide-line">
          {nodes.map((node) => (
            <NodeRow
              key={node.id}
              node={node}
              findings={findings.filter((f) => f.node_ids.includes(node.id))}
              expanded={expanded === node.id}
              onToggle={() => toggle(node.id)}
              fabric={fabric}
              updating={updating === node.id}
              onUpdate={() => void runUpdate(node)}
              onInstall={() => setInstalling(node)}
              onEdit={() => setEditing(node)}
              onForget={() => {
                setRemoveError(null);
                setForgetting(node);
              }}
              onChanged={reload}
            />
          ))}
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
              setRemoveError(e instanceof Error ? e.message : t("nodes.forgetFailed"));
            }
          }}
        />
      )}
    </section>
  );
}
