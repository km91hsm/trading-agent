// 확인 대화상자(<dialog>). window.confirm 대체. danger 면 확인 버튼이 위험색.
import { useEffect, useRef, type ReactNode } from "react";

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  danger,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  body?: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    else if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} className="dialog" onClose={onClose} aria-labelledby="dlg-title">
      <h2 id="dlg-title">{title}</h2>
      {body ? <div className="dialog-body">{body}</div> : null}
      <div className="dialog-actions">
        <button type="button" className="btn" onClick={onClose}>
          취소
        </button>
        <button type="button" className={danger ? "btn btn-danger" : "btn btn-primary"} onClick={onConfirm}>
          {confirmLabel}
        </button>
      </div>
    </dialog>
  );
}
