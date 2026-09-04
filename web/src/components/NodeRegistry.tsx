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
 */

import { useCallback, useEffect, useState } from "react";
import {
  addNode,
  discoverNodes,
  fetchNodeDiagnostics,
  fetchNodes,
  removeNode,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { ConfirmModal } from "@/components/Modal";
import type { ClusterNode, DiscoveredPeer, NodeFinding, NodeState } from "@/lib/types";
import {
  AlertCircle,
  Info,
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
  onAdded: () => void;
}

function AddNodeDialog({ onClose, onAdded }: AddNodeDialogProps) {
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [sshUser, setSshUser] = useState("");
  const [sshKeyPath, setSshKeyPath] = useState("");
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
      await addNode({
        name: name.trim() || undefined,
        address: address.trim(),
        ssh_user: sshUser.trim() || undefined,
        ssh_key_path: sshKeyPath.trim() || undefined,
      });
      onAdded();
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
        aria-label="Add node"
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-border bg-surface p-6 shadow-2xl"
      >
        <div className="mb-6 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-lg font-bold">
            <Server size={20} className="text-primary" />
            Add node
          </h3>
          <button
            onClick={onClose}
            aria-label="Close"
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
              Address *
            </label>
            <input
              id="node-address"
              type="text"
              autoFocus
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder="10.0.0.11"
              className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
            />
          </div>

          <div>
            <label
              htmlFor="node-name"
              className="mb-1 block text-sm font-medium text-text-muted"
            >
              Name
            </label>
            <input
              id="node-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Defaults to the address"
              className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
            />
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label
                htmlFor="node-ssh-user"
                className="mb-1 block text-sm font-medium text-text-muted"
              >
                SSH user
              </label>
              <input
                id="node-ssh-user"
                type="text"
                value={sshUser}
                onChange={(e) => setSshUser(e.target.value)}
                placeholder="Leave blank for ssh_config"
                className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
              />
            </div>
            <div>
              <label
                htmlFor="node-ssh-key"
                className="mb-1 block text-sm font-medium text-text-muted"
              >
                SSH key path
              </label>
              <input
                id="node-ssh-key"
                type="text"
                value={sshKeyPath}
                onChange={(e) => setSshKeyPath(e.target.value)}
                placeholder="~/.ssh/id_ed25519"
                className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
              />
            </div>
          </div>
          <p className="text-xs text-text-muted">
            Only the path is stored. The private key never leaves this machine, and only
            its public half is ever pushed to a node.
          </p>

          {/* Discovery is an aid, never a gate: the address field above always works. */}
          <div className="rounded-lg border border-border p-3">
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-medium">Find nodes on the network</p>
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
                Scan
              </button>
            </div>

            {peers !== null && !mdnsAvailable && (
              <p className="mt-2 text-sm text-text-muted">
                mDNS is unavailable here, so nothing can be discovered. Type the address
                above instead — that always works.
              </p>
            )}
            {peers !== null && mdnsAvailable && peers.length === 0 && (
              <p className="mt-2 text-sm text-text-muted">
                No responders answered. Type the address above instead.
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

export default function NodeRegistry() {
  const { data: nodes, loading, error, refetch } = useQuery<ClusterNode[]>(fetchNodes);
  const [findings, setFindings] = useState<NodeFinding[]>([]);
  const [showAdd, setShowAdd] = useState(false);
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
            Nodes
          </h3>
          <p className="mt-0.5 text-sm text-text-muted">
            The machines this control plane knows about. Each keeps a minted identity,
            so renaming or re-addressing one does not re-enroll it.
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
                <th scope="col" className="py-2 pr-4 font-semibold">Name</th>
                <th scope="col" className="py-2 pr-4 font-semibold">Address</th>
                <th scope="col" className="py-2 pr-4 font-semibold">Interfaces</th>
                <th scope="col" className="py-2 pr-4 font-semibold">Role</th>
                <th scope="col" className="py-2 pr-4 font-semibold">State</th>
                <th scope="col" className="py-2 font-semibold">
                  <span className="sr-only">Actions</span>
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
                      <span className="text-text-muted">Peer</span>
                    )}
                  </td>
                  <td className="py-2.5 pr-4">
                    <NodeStateBadge state={node.state} />
                  </td>
                  <td className="py-2.5 text-right">
                    {!node.is_control_plane && (
                      <button
                        onClick={() => {
                          setRemoveError(null);
                          setForgetting(node);
                        }}
                        aria-label={`Forget ${node.name}`}
                        title="Forget this node"
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
        <AddNodeDialog onClose={() => setShowAdd(false)} onAdded={reload} />
      )}

      {forgetting && (
        <ConfirmModal
          open
          onClose={() => setForgetting(null)}
          title="Forget node"
          message={`Forget "${forgetting.name}"? This drops what we know about the machine. It does not touch the machine itself — uninstalling an agent and wiping a node's identity are separate actions.`}
          confirmLabel="Forget"
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
