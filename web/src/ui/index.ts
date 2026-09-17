/** The primitives layer.
 *
 * One file per control, each replacing every copy that was in the tree. A page
 * imports from `@/ui`; nothing under `@/ui` imports from a page.
 */

export { Button, IconButton } from "./Button";
export type { ButtonProps, ButtonSize, ButtonVariant, IconButtonProps } from "./Button";

export { Card } from "./Card";
export type { CardProps } from "./Card";

export { Code } from "./Code";
export type { CodeProps } from "./Code";

export { EmptyState } from "./EmptyState";
export type { EmptyStateProps } from "./EmptyState";

export { ErrorLine } from "./ErrorLine";
export type { ErrorLineProps } from "./ErrorLine";

export { Field, Input, Select, Textarea } from "./Field";
export type { FieldProps, InputProps, SelectProps, TextareaProps } from "./Field";

export { Modal, ConfirmModal, AlertModal } from "./Modal";
export type { ModalProps, ModalSize, ConfirmModalProps, AlertModalProps } from "./Modal";

export { NodeScopedDialog } from "./NodeScopedDialog";
export type { NodeScopedDialogProps, NodePresenceLike, NodeScopeVerb } from "./NodeScopedDialog";

export { NodeState } from "./NodeState";
export type { NodeCondition, NodeStateProps } from "./NodeState";

export { PageHeader } from "./PageHeader";
export type { PageHeaderProps } from "./PageHeader";

export {
  ProgressRow,
  ACTIVE_STATES,
  isActive,
  progressPercent,
  transferred,
} from "./ProgressRow";
export type { ProgressJob, ProgressRowProps } from "./ProgressRow";

export { Spinner } from "./Spinner";
export type { SpinnerProps } from "./Spinner";

export { StatusBadge, isSettling, statusTone, TONE_TEXT } from "./StatusBadge";
export type { StatusBadgeProps, StatusTone } from "./StatusBadge";

export { Tabs } from "./Tabs";
export type { TabItem, TabsProps } from "./Tabs";

export { Toggle } from "./Toggle";
export type { ToggleProps } from "./Toggle";
