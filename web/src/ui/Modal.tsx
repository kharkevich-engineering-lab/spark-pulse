/** The one dialog frame.
 *
 * The focus trap, Escape, and focus restore are what they always were: this
 * was the only overlay in the tree that had them, and eight others — the
 * benchmark launcher, two recipe dialogs, the fabric apply, the doctor, two
 * node-registry dialogs, and the two "new file" modals — were hand-rolled
 * `fixed inset-0` divs with none. They all come through here now, so a
 * keyboard user gets the same dialog every time.
 *
 * What is new is the shape. A `size`, because a confirm and a per-node form
 * are not the same width, and `max-h-[90vh] overflow-y-auto` on the panel,
 * because the tallest of those dialogs used to run off the bottom of a laptop
 * screen with its own buttons below the fold.
 */

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useT } from "@/lib/i18n";
import { AlertCircle, AlertTriangle, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button, IconButton } from "./Button";

export type ModalSize = "sm" | "md" | "lg";

const WIDTH: Record<ModalSize, string> = {
  sm: "max-w-md",
  md: "max-w-lg",
  lg: "max-w-2xl",
};

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  actions?: React.ReactNode;
  icon?: React.ReactNode;
  size?: ModalSize;
}

/** Every element a keyboard user could tab to inside the dialog. Shared
 *  between the initial focus and the trap so the two agree on what counts. */
const FOCUSABLE_SELECTOR =
  "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function Modal({
  open,
  onClose,
  title,
  children,
  actions,
  icon,
  size = "sm",
}: ModalProps) {
  const t = useT();
  const modalRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  /** Whoever had focus before the dialog opened, so closing it does not strand
   *  a keyboard or screen-reader user on a page whose element just vanished. */
  const openerRef = useRef<HTMLElement | null>(null);

  // Auto-focus when the dialog opens, and give the opener its focus back.
  useEffect(() => {
    if (!open) return;
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const el = modalRef.current;
    if (el) {
      const focusable = el.querySelector(FOCUSABLE_SELECTOR);
      if (focusable instanceof HTMLElement) focusable.focus();
      else el.querySelector("h3")?.focus();
    }
    return () => {
      openerRef.current?.focus();
    };
  }, [open]);

  const handleClose = useCallback(() => {
    if (!open) return;
    onClose();
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") handleClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, handleClose]);

  // Trap Tab inside the dialog: nothing behind it should be reachable by
  // keyboard while it is up, and cycling off either end wraps rather than
  // escaping to the page underneath.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const el = modalRef.current;
      if (!el) return;
      const focusables = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const inside = document.activeElement instanceof Node && el.contains(document.activeElement);
      if (e.shiftKey) {
        if (!inside || document.activeElement === first) {
          e.preventDefault();
          last.focus();
        }
      } else if (!inside || document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
    >
      {/* Backdrop — close on click outside */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

      <div
        ref={modalRef}
        className={cn(
          "relative w-full max-h-[90vh] overflow-y-auto rounded-md bg-surface border border-line",
          WIDTH[size],
        )}
      >
        <div className="flex items-start justify-between p-5 pb-0">
          <div className="flex items-center gap-3">
            {icon}
            <h3 id={titleId} className="text-[17px] font-semibold">
              {title}
            </h3>
          </div>
          <IconButton
            size="sm"
            icon={X}
            label={t("common.close")}
            onClick={onClose}
            className="border-transparent text-muted hover:text-text hover:border-line"
          />
        </div>

        <div className="p-5">{children}</div>

        {actions && <div className="flex items-center justify-end gap-3 p-5 pt-0">{actions}</div>}
      </div>
    </div>
  );
}

// ── Confirm ──────────────────────────────────────────────────────────────────

export interface ConfirmModalProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: React.ReactNode;
  confirmLabel?: string;
  confirmVariant?: "danger" | "primary";
}

export function ConfirmModal({
  open,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel,
  confirmVariant = "primary",
}: ConfirmModalProps) {
  const t = useT();
  const [confirming, setConfirming] = useState(false);
  const label = confirmLabel ?? t("common.confirm");

  const handleConfirm = async () => {
    setConfirming(true);
    try {
      await onConfirm();
    } finally {
      setConfirming(false);
    }
  };

  // Enter confirms, Escape cancels (the frame handles Escape).
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Enter" && !confirming && !(e.target instanceof HTMLInputElement)) {
        e.preventDefault();
        handleConfirm();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, confirming, onConfirm]);

  return (
    <div data-confirm-modal="true">
      <Modal
        open={open}
        onClose={() => !confirming && onClose()}
        title={title}
        icon={
          <AlertTriangle
            size={20}
            className={confirmVariant === "danger" ? "text-bad" : "text-blue2"}
          />
        }
        actions={
          <>
            <Button size="sm" onClick={onClose} disabled={confirming}>
              {t("common.cancel")}
            </Button>
            <Button
              size="sm"
              variant={confirmVariant === "danger" ? "danger" : "primary"}
              onClick={handleConfirm}
              loading={confirming}
            >
              {confirming ? t("common.working") : label}
            </Button>
          </>
        }
      >
        <p className="text-muted">{message}</p>
      </Modal>
    </div>
  );
}

// ── Alert ────────────────────────────────────────────────────────────────────

export interface AlertModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  message: string;
}

export function AlertModal({ open, onClose, title, message }: AlertModalProps) {
  const t = useT();
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      icon={<AlertCircle size={20} className="text-bad" />}
      actions={
        <Button size="sm" variant="primary" onClick={onClose}>
          {t("common.ok")}
        </Button>
      }
    >
      <p className="text-muted whitespace-pre-wrap">{message}</p>
    </Modal>
  );
}

export default Modal;
