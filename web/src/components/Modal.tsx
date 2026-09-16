/** Reusable modal/dialog components to replace browser confirm/alert. */

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useT } from "@/lib/i18n";
import { AlertCircle, AlertTriangle, X } from "lucide-react";

interface BaseModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  actions?: React.ReactNode;
  icon?: React.ReactNode;
}

/** Every element a keyboard user could tab to inside the dialog. Shared
 *  between the initial focus and the trap so the two agree on what counts. */
const FOCUSABLE_SELECTOR =
  "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

/** The dialog frame, exported so a page can put its own controls inside one.
 *
 * `ConfirmModal` covers "are you sure"; a dialog that asks *which nodes* is a
 * form, and a form needs the frame rather than another fixed shape. */
export function Modal({ open, onClose, title, children, actions, icon }: BaseModalProps) {
  const t = useT();
  const modalRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  /** Whoever had focus before the dialog opened, so closing it does not strand
   *  a keyboard or screen-reader user on a page whose element just vanished. */
  const openerRef = useRef<HTMLElement | null>(null);

  // Auto-focus when modal opens, and give the opener its focus back on close.
  useEffect(() => {
    if (!open) return;
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // Focus the modal header or first interactive element
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

  // Close on Escape key
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

      {/* Modal */}
      <div
        ref={modalRef}
        className="relative w-full max-w-md rounded-xl bg-surface border border-border shadow-2xl"
      >
        {/* Header */}
        <div className="flex items-start justify-between p-5 pb-0">
          <div className="flex items-center gap-3">
            {icon}
            <h3 id={titleId} className="text-lg font-bold">{title}</h3>
          </div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-surface-hover transition-colors" title={t("common.close")}>
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="p-5">
          {children}
        </div>

        {/* Actions */}
        {actions && (
          <div className="flex items-center justify-end gap-3 p-5 pt-0">
            {actions}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Confirm Modal ────────────────────────────────────────────────────────────

interface ConfirmModalProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: string;
  confirmLabel?: string;
  confirmVariant?: "danger" | "primary";
}

export function ConfirmModal({ open, onClose, onConfirm, title, message, confirmLabel, confirmVariant = "primary" }: ConfirmModalProps) {
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

  // Enter confirms, ESC cancels (handled by BaseModal)
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
        icon={<AlertTriangle size={20} className={confirmVariant === "danger" ? "text-warning" : "text-primary"} />}
        actions={
          <>
          <button
            onClick={onClose}
            disabled={confirming}
            className="px-4 py-2 rounded-lg border border-border hover:border-border-hover disabled:opacity-50 transition-colors"
          >
            {t("common.cancel")}
          </button>
          <button
            onClick={handleConfirm}
            disabled={confirming}
            className={`px-4 py-2 rounded-lg text-white font-medium transition-colors disabled:opacity-50 ${
              confirmVariant === "danger"
                ? "bg-danger hover:bg-danger/80"
                : "bg-primary hover:bg-primary-hover"
            }`}
          >
            {confirming ? t("common.working") : label}
          </button>
        </>
      }
    >
      <p className="text-text-muted">{message}</p>
    </Modal>
    </div>
  );
}

// ── Alert Modal ──────────────────────────────────────────────────────────────

interface AlertModalProps {
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
      icon={<AlertCircle size={20} className="text-danger" />}
      actions={
        <button
          onClick={onClose}
          className="px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover text-white font-medium transition-colors"
        >
          {t("common.ok")}
        </button>
      }
    >
      <p className="text-text-muted whitespace-pre-wrap">{message}</p>
    </Modal>
  );
}
