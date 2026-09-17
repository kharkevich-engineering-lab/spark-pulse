/** "Which machines?" — asked once, for every operation that has to ask.
 *
 * Three dialogs said this: delete an image, delete a model, replicate a model.
 * They shared a shape (a sentence, a fieldset of nodes, a note beside each
 * name, cancel and confirm) and disagreed on everything else — one preselected
 * every node when presence was unknown, one preselected none; one showed a
 * spinner while asking, one showed nothing; only one offered `force`.
 *
 * The verb decides the preselection, which is the only part that genuinely
 * differs: **delete** starts on the nodes that hold a copy, because a node
 * without one has no disk to reclaim, and **replicate** starts on the nodes
 * that do not, because a node that already has it has nothing to gain from a
 * transfer. Everything else is the same dialog.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useT } from "@/lib/i18n";
import { Modal } from "./Modal";
import { Button } from "./Button";
import { Spinner } from "./Spinner";
import { ErrorLine } from "./ErrorLine";

/** All this dialog needs of a presence answer: who holds a copy. */
export interface NodePresenceLike {
  nodes: { node: string; present: boolean }[];
}

export type NodeScopeVerb = "delete" | "replicate";

export interface NodeScopedDialogProps {
  verb: NodeScopeVerb;
  title: string;
  /** One sentence naming the thing and what is about to happen to it. */
  body: React.ReactNode;
  /** The fieldset's legend — "Also remove from", "Replicate to". */
  legend: string;
  /** The nodes on offer. The control node is never among them. */
  nodes: string[];
  /** Asked as the dialog opens, and again after a confirm resolves. */
  fetchPresence?: (nodes: string[]) => Promise<NodePresenceLike>;
  /** For a caller that already holds the answer and does not want it asked
   *  again. `"loading"` says the caller is still asking. */
  presence?: NodePresenceLike | "loading" | null;
  /** What to preselect while presence is unknown. Default: nothing. */
  selectionWhenUnknown?: string[];
  /** A note after a node's name — "not there", "already there", an outcome. */
  noteFor?: (node: string, holds: boolean | null) => React.ReactNode;
  /** Offering `force` at all is the caller's choice; only replicate does. */
  forceLabel?: string;
  /** A function when the wording depends on what is selected: removing a
   *  model from this machine alone is "Delete", and from four machines is
   *  something the operator should be made to read. */
  confirmLabel: string | ((selected: string[]) => string);
  /** Resolving re-asks presence. The dialog never closes itself: a delete
   *  closes from the page, a replicate stays open to show what happened. */
  onConfirm: (nodes: string[], options: { force: boolean }) => void | Promise<void>;
  onClose: () => void;
  /** One red line above the buttons. */
  error?: React.ReactNode;
  /** Refuse an empty selection. Delete allows it — "just from here". */
  requireSelection?: boolean;
  children?: React.ReactNode;
}

export function NodeScopedDialog({
  verb,
  title,
  body,
  legend,
  nodes,
  fetchPresence,
  presence: given,
  selectionWhenUnknown,
  noteFor,
  forceLabel,
  confirmLabel,
  onConfirm,
  onClose,
  error,
  requireSelection = false,
  children,
}: NodeScopedDialogProps) {
  const t = useT();
  const [fetched, setFetched] = useState<NodePresenceLike | "loading" | null>(null);
  const [selected, setSelected] = useState<string[]>(selectionWhenUnknown ?? []);
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);
  /** A ref, not state: whether the operator has touched the boxes must not
   *  re-run the presence query, and reading it inside the effect is exactly
   *  what a ref is for. */
  const touched = useRef(false);

  const presence = fetchPresence ? fetched : (given ?? null);

  const load = useCallback(() => {
    if (!fetchPresence || nodes.length === 0) return Promise.resolve();
    setFetched("loading");
    return fetchPresence(nodes)
      .then((answer) => setFetched(answer))
      .catch(() => setFetched(null));
  }, [fetchPresence, nodes]);

  useEffect(() => {
    load();
  }, [load]);

  /** The nodes that hold a copy, or `null` while nobody knows. */
  const holders = useMemo(() => {
    if (!presence || presence === "loading") return null;
    return presence.nodes.filter((n) => n.present).map((n) => n.node);
  }, [presence]);

  useEffect(() => {
    if (holders === null) return;
    // Only preselect what the operator has not already changed.
    if (touched.current) return;
    setSelected(
      verb === "delete" ? holders : nodes.filter((n) => !holders.includes(n)),
    );
  }, [holders, nodes, verb]);

  const toggle = (node: string, on: boolean) => {
    touched.current = true;
    setSelected((current) => (on ? [...current, node] : current.filter((n) => n !== node)));
  };

  const confirm = async () => {
    setBusy(true);
    try {
      await onConfirm(selected, { force });
      await load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={onClose} title={title} size="md">
      <div className="space-y-4">
        <p className="text-[14px] text-muted">{body}</p>

        {nodes.length > 0 && (
          <fieldset className="space-y-2">
            <legend className="text-[13px] font-medium mb-1">{legend}</legend>
            {presence === "loading" && (
              <p className="text-[13px] text-muted flex items-center gap-2">
                <Spinner size="sm" />
                {t("common.loading")}
              </p>
            )}
            {nodes.map((node) => (
              <label key={node} className="flex items-center gap-2 text-[14px]">
                <input
                  type="checkbox"
                  className="accent-[var(--blue)]"
                  checked={selected.includes(node)}
                  onChange={(e) => toggle(node, e.target.checked)}
                />
                <span className="font-mono">{node}</span>
                {noteFor?.(node, holders === null ? null : holders.includes(node))}
              </label>
            ))}
          </fieldset>
        )}

        {forceLabel && (
          <label className="flex items-center gap-2 text-[14px]">
            <input
              type="checkbox"
              className="accent-[var(--blue)]"
              checked={force}
              onChange={(e) => setForce(e.target.checked)}
            />
            {forceLabel}
          </label>
        )}

        {children}

        <ErrorLine>{error}</ErrorLine>

        <div className="flex justify-end gap-2">
          <Button size="sm" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            size="sm"
            variant={verb === "delete" ? "danger" : "primary"}
            onClick={confirm}
            loading={busy}
            disabled={requireSelection && selected.length === 0}
          >
            {typeof confirmLabel === "function" ? confirmLabel(selected) : confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export default NodeScopedDialog;
