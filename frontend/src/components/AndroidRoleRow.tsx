import { useRef } from "react";
import { Check, ChevronRight, Pin, PinOff, Trash2 } from "lucide-react";

interface Props {
  label: string;
  description: string;
  active: boolean;
  disabled: boolean;
  selectDisabled?: boolean;
  manageable: boolean;
  pinned: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSelect: () => void;
  onPin: () => void;
  onDelete: () => void;
}

export default function AndroidRoleRow(props: Props) {
  const start = useRef<{ x: number; y: number; vertical: boolean } | null>(null);
  const suppressClickUntil = useRef(0);
  return <div
    className={`android-role-swipe${props.open ? " revealed" : ""}`}
    data-role-manageable={props.manageable || undefined}
    onPointerDown={(event) => {
      if (!props.manageable) return;
      event.stopPropagation();
      if (props.disabled || !event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) return;
      suppressClickUntil.current = 0;
      start.current = { x: event.clientX, y: event.clientY, vertical: false };
    }}
    onPointerMove={(event) => {
      if (!props.manageable) return;
      event.stopPropagation();
      const from = start.current;
      if (!from || props.disabled) return;
      const dx = event.clientX - from.x, dy = event.clientY - from.y;
      if (Math.abs(dy) > 14 && Math.abs(dy) > Math.abs(dx)) from.vertical = true;
      if (Math.max(Math.abs(dx), Math.abs(dy)) > 14) suppressClickUntil.current = Date.now() + 400;
      if (!from.vertical && Math.abs(dx) > 35 && Math.abs(dx) > Math.abs(dy) * 1.4) props.onOpenChange(dx < 0);
    }}
    onPointerUp={(event) => { if (props.manageable) event.stopPropagation(); start.current = null; }}
    onPointerCancel={() => { start.current = null; suppressClickUntil.current = Date.now() + 400; }}
    onClickCapture={(event) => {
      if (Date.now() < suppressClickUntil.current) { event.preventDefault(); event.stopPropagation(); }
    }}
  >
    {props.manageable && <div className="android-role-actions" aria-hidden={!props.open} inert={!props.open}>
      <button type="button" disabled={props.disabled || !props.open} className="android-role-pin" aria-label={`${props.pinned ? "取消置顶" : "置顶"}${props.label}`} onClick={(event) => { event.stopPropagation(); props.onPin(); }}>
        {props.pinned ? <PinOff size={19} aria-hidden="true" /> : <Pin size={19} aria-hidden="true" />}<span>{props.pinned ? "取消置顶" : "置顶"}</span>
      </button>
      <button type="button" disabled={props.disabled || !props.open} className="android-role-delete" aria-label={`删除${props.label}`} onClick={(event) => { event.stopPropagation(); props.onDelete(); }}><Trash2 size={19} aria-hidden="true" /><span>删除</span></button>
    </div>}
    <button type="button" className="android-role-row" aria-pressed={props.active} disabled={props.disabled || props.selectDisabled}
      aria-expanded={props.manageable ? props.open : undefined}
      aria-description={props.manageable ? "向左滑动或按左方向键，显示置顶和删除操作" : undefined}
      onClick={() => { if (props.open) props.onOpenChange(false); else props.onSelect(); }}
      onKeyDown={(event) => {
        if (!props.manageable || props.disabled) return;
        if (event.key === "ArrowLeft" || event.key === "ArrowRight" || (event.key === "Escape" && props.open)) {
          event.preventDefault(); event.stopPropagation(); props.onOpenChange(event.key === "ArrowLeft");
        }
      }}>
      <span className="android-role-avatar" aria-hidden="true">{props.label.slice(0, 1)}</span>
      <span className="android-role-copy"><strong>{props.label}{props.pinned && <Pin size={12} className="android-role-pinned-indicator" aria-label="已置顶" />}</strong><small>{props.description}</small></span>
      {props.active ? <Check size={18} className="text-accent" aria-hidden="true" /> : <ChevronRight size={18} className="text-muted" aria-hidden="true" />}
    </button>
  </div>;
}
