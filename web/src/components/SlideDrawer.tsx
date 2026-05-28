/** Generic slide-in drawer component.

Usage:
```tsx
<SlideDrawer open={open} onClose={onClose}
  header={
    <div>
      <h3 className="text-xl font-bold">{title}</h3>
      <p className="text-sm text-text-muted">{subtitle}</p>
    </div>
  }
  actions={
    <>
      <Button>Save</Button>
      <Button onClick={onClose}>✕</Button>
    </>
  }>
  <div className="px-6 py-5">{children}</div>
</SlideDrawer>
```
*/

import { useCallback, useEffect, useRef } from "react";

interface SlideDrawerProps {
  open: boolean;
  onClose: () => void;
  header: React.ReactNode;
  actions: React.ReactNode;
  children: React.ReactNode;
}

export default function SlideDrawer({ open, onClose, header, actions, children }: SlideDrawerProps) {
  const drawerRef = useRef<HTMLDivElement>(null);

  const handleKeyDown = useCallback(() => {
    // Don't close the drawer if a nested confirm modal is handling Escape
    if (document.querySelector("[data-confirm-modal]")) return;
    onClose();
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    drawerRef.current?.focus();
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") handleKeyDown();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, handleKeyDown]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
      <div
        ref={drawerRef}
        tabIndex={-1}
        className="h-full w-full max-w-2xl bg-surface border-l border-border shadow-xl flex flex-col overflow-hidden outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header row: content + actions */}
        <div className="sticky top-0 bg-surface border-b border-border px-6 py-4 flex items-start justify-between gap-3 shrink-0">
          <div className="flex-1 min-w-0">
            {header}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {actions}
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto">
          {children}
        </div>
      </div>
    </div>
  );
}
