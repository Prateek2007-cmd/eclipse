import { useEffect, useRef, type ReactNode } from "react";

/** A confirmation dialog for destructive or external actions
 * (send, delete, revoke, apply). Native <dialog> (guide:
 * dialog): the browser provides the focus trap, top layer,
 * Esc handling and focus return — no role="dialog",
 * aria-modal, or manual trap. The safe action gets autofocus.
 *
 * The dialog reads its answer from returnValue in the close
 * handler: "confirm" runs the action, anything else (Esc,
 * Cancel, backdrop) cancels it. */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  danger = false,
  pending = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  pending?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.returnValue = "";
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open ]);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    const onClose = () => {
      const value = dialog.returnValue;
      dialog.returnValue = "";
      if (value === "confirm") {
        onConfirm();
      } else {
        onCancel();
      }
    };
    dialog.addEventListener("close", onClose);
    return () => dialog.removeEventListener("close", onClose);
  }, [onConfirm, onCancel]);

  return (
    <dialog
      ref={ref}
      aria-labelledby="confirm-title"
      className="confirm-dialog"
      closedby="closerequest"
    >
      <h2 id="confirm-title">{title}</h2>
      <div className="confirm-body">{children}</div>
      <form method="dialog" className="confirm-actions">
        <button type="submit" value="cancel" autoFocus disabled={pending}>
          Cancel
        </button>
        <button
          type="submit"
          value="confirm"
          className={danger ? "danger-button" : "primary"}
          disabled={pending}
        >
          {pending ? "Working…" : confirmLabel}
        </button>
      </form>
    </dialog>
  );
}
