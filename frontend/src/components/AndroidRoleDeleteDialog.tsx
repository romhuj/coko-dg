import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

interface Props {
  label: string;
  active: boolean;
  busy: boolean;
  deleted: boolean;
  error: string;
  onCancel: () => void;
  onConfirm: () => void;
}

export default function AndroidRoleDeleteDialog({ label, active, busy, deleted, error, onCancel, onConfirm }: Props) {
  const dialog = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const close = useRef(onCancel);
  const locked = useRef(busy);
  close.current = onCancel;
  locked.current = busy;
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancel.current?.focus();
    const keys = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault(); event.stopPropagation();
        if (!locked.current) close.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), [tabindex="0"]') ?? []), ...document.querySelectorAll<HTMLElement>('[data-coyote-estop]:not(:disabled)')].filter((item) => item.getClientRects().length > 0);
      event.preventDefault();
      if (!items.length) { dialog.current?.focus(); return; }
      const current = items.indexOf(document.activeElement as HTMLElement);
      const next = current < 0 ? (event.shiftKey ? items.length - 1 : 0) : (current + (event.shiftKey ? -1 : 1) + items.length) % items.length;
      items[next].focus();
    };
    document.addEventListener("keydown", keys, true);
    return () => { document.removeEventListener("keydown", keys, true); if (previous?.isConnected) previous.focus(); };
  }, []);

  return createPortal(<div className="android-role-delete-overlay" onPointerDown={(event) => event.stopPropagation()} onPointerUp={(event) => event.stopPropagation()} onClick={(event) => { event.stopPropagation(); if (event.target === event.currentTarget && !busy) onCancel(); }}>
    <div ref={dialog} role="dialog" aria-modal={false} aria-labelledby="android-delete-role-title" aria-describedby="android-delete-role-description" aria-busy={busy} tabIndex={-1} className="android-role-delete-dialog">
      <h2 id="android-delete-role-title">{deleted ? "角色已删除" : `删除「${label}」？`}</h2>
      <p id="android-delete-role-description">{deleted ? "角色已删除，请刷新列表完成界面更新。" : active ? "将停止设备输出和自动运行，切换回默认角色并删除此角色。此操作无法撤销。" : "将删除此角色及其已保存的角色资料。此操作无法撤销。"}</p>
      {error && <p className="android-compose-error" role="alert">{error}</p>}
      {busy && <p className="android-small" role="status">{deleted ? "正在刷新…" : "当前回合结束后删除…"}</p>}
      <div className="android-role-delete-buttons"><button ref={cancel} type="button" className="android-button" disabled={busy} onClick={onCancel}>{deleted ? "关闭" : "取消"}</button><button type="button" className="android-button danger" disabled={busy} onClick={onConfirm}>{deleted ? "刷新列表" : "删除角色"}</button></div>
    </div>
  </div>, document.body);
}
