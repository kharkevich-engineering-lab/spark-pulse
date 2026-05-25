/** Reusable modal/dialog components to replace browser confirm/alert. */

import { useEffect, useRef, useState } from "react";
import { AlertCircle, AlertTriangle, X } from "lucide-react";

interface BaseModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  actions?: React.ReactNode;
  icon?: React.ReactNode;
}

function BaseModal({ open, onClose, title, children, actions, icon }: BaseModalProps) {
  const modalRef = useRef<HTMLDivElement>(null);

  // Auto-focus when modal opens
  useEffect(() => {
    if (!open) return;
    // Focus the modal header or first interactive element
    const el = modalRef.current;
    if (el) {
      const focusable = el.querySelector("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
      if (focusable instanceof HTMLElement) focusable.focus();
      else el.querySelector("h3")?.focus();
    }
  }, [open]);

  // Close on Escape key
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4" role="dialog" aria-modal="true">
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
            <h3 className="text-lg font-bold">{title}</h3>
          </div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-surface-hover transition-colors" title="Close">
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

export function ConfirmModal({ open, onClose, onConfirm, title, message, confirmLabel = "Confirm", confirmVariant = "primary" }: ConfirmModalProps) {
  const [confirming, setConfirming] = useState(false);

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
      if (e.key === "Enter" && !confirming) {
        e.preventDefault();
        handleConfirm();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, confirming, onConfirm]);

  return (
    <div data-confirm-modal="true">
      <BaseModal
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
            Cancel
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
            {confirming ? "..." : confirmLabel}
          </button>
        </>
      }
    >
      <p className="text-text-muted">{message}</p>
    </BaseModal>
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
  return (
    <BaseModal
      open={open}
      onClose={onClose}
      title={title}
      icon={<AlertCircle size={20} className="text-danger" />}
      actions={
        <button
          onClick={onClose}
          className="px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover text-white font-medium transition-colors"
        >
          OK
        </button>
      }
    >
      <p className="text-text-muted whitespace-pre-wrap">{message}</p>
    </BaseModal>
  );
}
