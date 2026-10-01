import { Pin, PinOff, Trash2 } from "lucide-react";
import AndroidDialog from "./AndroidDialog";
import type { ConversationSummary } from "../types";

export default function AndroidConversationDialog({ chat, deleting, busy, error, active, onClose, onPin, onAskDelete, onDelete }: {
  chat: ConversationSummary; deleting: boolean; busy: boolean; error: string; active: boolean;
  onClose: () => void; onPin: () => void; onAskDelete: () => void; onDelete: () => void;
}) {
  return <AndroidDialog title={deleting ? "删除聊天？" : "管理聊天"} id="android-conversation-dialog-title" busy={busy} onClose={onClose}>
    <p className="android-conversation-dialog-name">{chat.title || "新聊天"}</p>
    {deleting ? <>
      <p className="android-small">聊天记录删除后无法恢复。{active ? "删除当前聊天会停止设备输出与自动运行，并新建一个空白聊天。" : ""}</p>
      <div className="android-dialog-actions"><button type="button" className="android-button" disabled={busy} onClick={onClose}>取消</button><button type="button" className="android-button android-danger" disabled={busy} onClick={onDelete}>{busy ? "删除中…" : "确认删除"}</button></div>
    </> : <>
      <div className="android-conversation-actions">
        <button type="button" disabled={busy} onClick={onPin}>{chat.pinned ? <PinOff size={20} /> : <Pin size={20} />}<span>{busy ? "处理中…" : chat.pinned ? "取消置顶" : "置顶"}</span></button>
        <button type="button" disabled={busy} onClick={onAskDelete}><Trash2 size={20} /><span>删除</span></button>
      </div>
      <button type="button" className="android-button android-dialog-cancel" disabled={busy} onClick={onClose}>取消</button>
    </>}
    {error && <p className="android-compose-error" role="alert">{error}</p>}
  </AndroidDialog>;
}
