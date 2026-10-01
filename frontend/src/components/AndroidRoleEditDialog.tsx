import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useApp, useChat } from "../store";
import { conversationSwitchBlocked } from "../chatArchive";
import type { EditableCharacter } from "../types";
import { SPEECH_VOICES } from "../replySpeech";
import { localRoleAvatar } from "../roleAvatar";
import AndroidDialog from "./AndroidDialog";
import RoleAvatarPicker from "./RoleAvatarPicker";
import RoleVoiceSelect from "./RoleVoiceSelect";

export default function AndroidRoleEditDialog({ role, onClose, onSaved }: { role: string; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState<EditableCharacter | null>(null);
  const [avatar, setAvatar] = useState<string | null | undefined>();
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [imageBusy, setImageBusy] = useState(false);
  const [error, setError] = useState(""), [saved, setSaved] = useState(false), [reload, setReload] = useState(0);
  const pending = useRef(false), didSave = useRef(false);
  const localBusy = useChat((state) => state.busy || state.awaitingConfirmation || state.historyLoading);
  const serverBusy = useApp((state) => !!state.state?.turn_busy || (state.state?.pending_chat ?? 0) > 0);
  useEffect(() => {
    let current = true; setLoading(true); setError("");
    api.getEditableCharacter(role).then((value) => { if (current) { setForm(value); setAvatar(undefined); } })
      .catch((failure) => { if (current) setError(failure instanceof Error ? failure.message : "角色读取失败，请重试。"); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [role, reload]);
  const patch = (key: "name" | "personality" | "background" | "note" | "voiceId", value: string) => setForm((current) => current ? { ...current, [key]: value } : current);
  const save = async () => {
    if (!form || pending.current || imageBusy) return;
    if (!didSave.current && conversationSwitchBlocked()) { setError("请等待当前消息完成或确认上次请求后，再保存角色。"); return; }
    if (!didSave.current && (!form.name.trim() || [...form.name.trim()].length > 60 || [...form.personality.trim()].length > 3000 || [...form.background.trim()].length > 3000 || [...form.note.trim()].length > 1500)) {
      setError("名称需为 1–60 字，性格与背景各最多 3000 字，补充设定最多 1500 字。"); return;
    }
    pending.current = true; setBusy(true); setError("");
    try {
      if (!didSave.current) {
        await api.editCharacter({ role, name: form.name.trim(), personality: form.personality.trim(), background: form.background.trim(), note: form.note.trim(), voiceId: form.voiceId, ...(avatar !== undefined ? { avatar_data: avatar } : {}) });
        didSave.current = true; setSaved(true);
      }
      useApp.getState().setState(await api.state()); onSaved();
    } catch (failure) {
      const message = failure instanceof Error ? failure.message : "请稍后重试。";
      setError(didSave.current ? `角色已保存，状态刷新失败：${message} 可点击“刷新角色”重试。` : message);
    } finally { pending.current = false; setBusy(false); }
  };
  const locked = loading || busy || saved;
  const voice = SPEECH_VOICES.find((item) => item.id === form?.voiceId)?.id ?? "system-default";
  return <AndroidDialog title="编辑角色" id="android-edit-role-title" backLabel="返回角色" busy={busy || imageBusy} onClose={onClose}>
    {loading ? <p className="android-small" role="status">正在读取角色…</p> : form && <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <RoleAvatarPicker id="role-edit-avatar" name={form.name} value={avatar === undefined ? localRoleAvatar(form.avatar_url) : avatar} disabled={locked} onChange={setAvatar} onBusyChange={setImageBusy} />
      <p className="android-small">保留原有资料，修改的信息从下一轮对话生效。</p>
      <label className="android-field-label" htmlFor="role-edit-name">角色名称</label>
      <input id="role-edit-name" className="android-field" autoComplete="off" required maxLength={60} value={form.name} disabled={locked} onChange={(event) => patch("name", event.target.value)} />
      {form.kind === "search" ? <>
        <label className="android-field-label" htmlFor="role-edit-note">补充设定</label>
        <textarea id="role-edit-note" className="android-field android-role-editor-text" rows={5} maxLength={1500} value={form.note} disabled={locked} placeholder="补充角色性格、动机、说话习惯" onChange={(event) => patch("note", event.target.value)} />
        <p className="android-small">保留原有搜索资料，修改角色的补充设定。</p>
      </> : <>
        <label className="android-field-label" htmlFor="role-edit-personality">角色性格</label>
        <textarea id="role-edit-personality" className="android-field android-role-editor-text" rows={5} maxLength={3000} value={form.personality} disabled={locked} onChange={(event) => patch("personality", event.target.value)} />
        <label className="android-field-label" htmlFor="role-edit-background">角色背景 <span className="text-muted">（选填）</span></label>
        <textarea id="role-edit-background" className="android-field android-role-editor-text" rows={4} maxLength={3000} value={form.background} disabled={locked} onChange={(event) => patch("background", event.target.value)} />
        {form.legacy_prompt_preserved && <p className="android-small">留空的性格与背景沿用原有设定，无需全部重写。</p>}
      </>}
      <RoleVoiceSelect id="role-edit-voice" value={voice} includeSystem disabled={locked} onChange={(value) => patch("voiceId", value)} />
      <button type="submit" className="android-button primary android-role-editor-save" disabled={loading || busy || imageBusy || (!saved && (localBusy || serverBusy))}>{busy ? "保存中…" : saved ? "刷新角色" : "保存修改"}</button>
      {!saved && (localBusy || serverBusy) && <p className="android-small" role="status">当前消息处理完成后可保存。</p>}
    </form>}
    {error && <p className="android-compose-error" role="alert">{error}</p>}
    {!form && !loading && <button type="button" className="android-button" onClick={() => setReload((value) => value + 1)}>重新读取</button>}
  </AndroidDialog>;
}
