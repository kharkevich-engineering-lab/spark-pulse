/** Moved to `@/ui/Modal`, where the other primitives live.
 *
 * Re-exported here so the dozen `@/components/Modal` imports across the pages
 * did not all have to change in the same commit that changed what a dialog
 * looks like.
 */

export { Modal, ConfirmModal, AlertModal, default } from "@/ui/Modal";
export type { ModalProps, ModalSize, ConfirmModalProps, AlertModalProps } from "@/ui/Modal";
