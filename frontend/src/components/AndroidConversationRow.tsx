import { useEffect, useRef } from "react";
import { Pin } from "lucide-react";
import type { ConversationSummary } from "../types";

/** Scrolling cancels the long press; its release must never select the chat. */
export default function AndroidConversationRow({ chat, active, disabled, onSelect, onManage }: {
  chat: ConversationSummary; active: boolean; disabled: boolean; onSelect: () => void; onManage: () => void;
}) {
  const press = useRef<{ pointerId: number; x: number; y: number; timer: number } | null>(null);
  const suppressClick = useRef(false);
  const cancel = () => { if (press.current) window.clearTimeout(press.current.timer); press.current = null; };
  useEffect(() => { if (disabled) cancel(); return cancel; }, [disabled]);
  const manage = () => { cancel(); if (!disabled) { suppressClick.current = true; onManage(); } };
  return <button type="button" className="android-conversation-row" data-conversation-id={chat.id}
    aria-current={active ? "true" : undefined} aria-haspopup="dialog" aria-describedby="android-history-hint" disabled={disabled}
    onPointerDown={(event) => {
      cancel(); suppressClick.current = false;
      if (disabled || !event.isPrimary || event.button !== 0) return;
      press.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, timer: window.setTimeout(manage, 550) };
    }}
    onPointerMove={(event) => {
      const start = press.current;
      if (start && (event.pointerId !== start.pointerId || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 10)) cancel();
    }}
    onPointerUp={cancel} onPointerCancel={cancel} onLostPointerCapture={cancel} onBlur={cancel}
    onContextMenu={(event) => { event.preventDefault(); event.stopPropagation(); manage(); }}
    onKeyDown={(event) => {
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) { event.preventDefault(); event.stopPropagation(); manage(); }
      else if (event.key === "Enter" || event.key === " ") suppressClick.current = false;
    }}
    onClick={(event) => {
      if (suppressClick.current) { event.preventDefault(); event.stopPropagation(); suppressClick.current = false; return; }
      onSelect();
    }}>
    <span className="android-conversation-title">{chat.pinned && <Pin size={13} aria-label="已置顶" />}<span>{chat.title || "新聊天"}</span></span>
    <small>{chat.message_count} 条消息</small>
  </button>;
}
