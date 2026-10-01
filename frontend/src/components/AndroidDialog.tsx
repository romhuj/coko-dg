import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft } from "lucide-react";

/** A dialog never makes the global emergency control inert. */
export default function AndroidDialog({ title, id, busy = false, onClose, children, backLabel }: {
  title: string; id: string; busy?: boolean; onClose: () => void; children: ReactNode; backLabel?: string;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const latest = useRef({ busy, onClose }); latest.current = { busy, onClose };
  useEffect(() => {
    window.dispatchEvent(new Event("coyote:voice-stop"));
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.current?.focus({ preventScroll: true });
    const keys = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault(); event.stopPropagation();
        if (!latest.current.busy) latest.current.onClose();
      } else if (event.key === "Tab") {
        const items = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]') ?? []),
          ...document.querySelectorAll<HTMLElement>('[data-coyote-estop]:not(:disabled)')].filter((item) => item.getClientRects().length);
        event.preventDefault();
        if (!items.length) return;
        const at = items.indexOf(document.activeElement as HTMLElement);
        items[(at + (event.shiftKey ? -1 : 1) + items.length) % items.length].focus();
      }
    };
    document.addEventListener("keydown", keys, true);
    return () => { document.removeEventListener("keydown", keys, true); if (previous?.isConnected) previous.focus(); };
  }, []);
  return createPortal(<div className={`android-dialog-layer${backLabel ? " android-dialog-page-layer" : ""}`} onPointerDown={(event) => event.stopPropagation()} onPointerUp={(event) => event.stopPropagation()}
    onClick={(event) => { event.stopPropagation(); if (event.target === event.currentTarget && !busy) onClose(); }}>
    <div ref={dialog} className={`android-dialog${backLabel ? " android-dialog-page" : ""}`} role="dialog" aria-modal={false} aria-labelledby={id} aria-busy={busy} tabIndex={-1}>
      {backLabel ? <><div className="android-dialog-page-heading"><button type="button" className="android-icon" disabled={busy} aria-label={backLabel} onClick={onClose}><ArrowLeft size={22} /></button><h2 id={id}>{title}</h2></div><div className="android-dialog-page-body">{children}</div></> : <><h2 id={id}>{title}</h2>{children}</>}
    </div>
  </div>, document.body);
}
