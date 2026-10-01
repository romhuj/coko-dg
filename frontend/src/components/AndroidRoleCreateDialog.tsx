import { useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, PencilLine, Search } from "lucide-react";
import { api } from "../api";
import { useApp, useChat } from "../store";
import type { SpeechVoiceId } from "../replySpeech";
import RoleVoiceSelect from "./RoleVoiceSelect";
import RoleAvatarPicker from "./RoleAvatarPicker";
import { conversationSwitchBlocked } from "../chatArchive";

interface Props { onSearch: () => void; onCreated: () => void; onClose: () => void }

export default function AndroidRoleCreateDialog({ onSearch, onCreated, onClose }: Props) {
  const [custom, setCustom] = useState(false);
  const [name, setName] = useState("");
  const [personality, setPersonality] = useState("");
  const [background, setBackground] = useState("");
  const [voiceId, setVoiceId] = useState<SpeechVoiceId>("melo-zh");
  const [avatar, setAvatar] = useState<string | null>(null), [imageBusy, setImageBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const dialog = useRef<HTMLDivElement>(null);
  const pending = useRef(false);
  const created = useRef(false);
  const goBack = useRef(() => {});
  goBack.current = () => {
    if (pending.current || imageBusy) return;
    if (custom && !created.current) { setCustom(false); setError(""); }
    else onClose();
  };

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const keys = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); goBack.current(); return; }
      if (event.key !== "Tab") return;
      const items = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]') ?? []), ...document.querySelectorAll<HTMLElement>('[data-coyote-estop]:not(:disabled)')].filter((item) => item.getClientRects().length > 0);
      event.preventDefault();
      if (!items.length) { dialog.current?.focus(); return; }
      const current = items.indexOf(document.activeElement as HTMLElement);
      const next = current < 0 ? (event.shiftKey ? items.length - 1 : 0) : (current + (event.shiftKey ? -1 : 1) + items.length) % items.length;
      items[next].focus();
    };
    document.addEventListener("keydown", keys, true);
    return () => { document.removeEventListener("keydown", keys, true); if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => { dialog.current?.focus({ preventScroll: true }); }, [custom]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (pending.current || imageBusy) return;
    if (!created.current && conversationSwitchBlocked()) { setError("请等待当前消息完成或确认上次请求后，再创建角色。"); return; }
    if (!created.current && (!name.trim() || name.trim().length > 60 || !personality.trim() || personality.trim().length > 3000 || background.trim().length > 3000)) {
      setError("请填写 1–60 字的名称和 1–3000 字的性格，背景最多 3000 字。");
      return;
    }
    pending.current = true; setBusy(true); setError("");
    try {
      if (!created.current) {
        await api.createCustomCharacter(name.trim(), personality.trim(), background.trim(), voiceId, avatar);
        created.current = true; setSaved(true);
      }
      useApp.setState({ state: await api.state() });
      if (useApp.getState().state?.platform !== "android") useChat.getState().clear();
      onCreated();
    } catch (failure) {
      const message = failure instanceof Error ? failure.message : String(failure);
      setError(created.current ? `角色已创建，列表刷新失败：${message}，请点击“刷新角色”重试。` : `创建失败：${message}`);
    } finally { pending.current = false; setBusy(false); }
  };

  return createPortal(<div
    className={custom ? "android-role-create-page fixed inset-x-0 bottom-0 top-16 z-[100] flex bg-ink" : "android-role-create-overlay android-role-delete-overlay"}
    onPointerDown={(event) => event.stopPropagation()} onPointerUp={(event) => event.stopPropagation()}
    onClick={(event) => { event.stopPropagation(); if (!custom && event.target === event.currentTarget && !busy) onClose(); }}>
    <div ref={dialog} role="dialog" aria-modal={false} aria-labelledby="android-create-role-title" aria-describedby="android-create-role-description" aria-busy={busy} tabIndex={-1}
      className={custom ? "flex h-full min-h-0 w-full flex-col overflow-hidden bg-ink text-text outline-none" : "android-role-delete-dialog android-role-create-dialog"}>
      {custom ? <>
        <div className="flex shrink-0 items-center gap-2 px-[22px] pb-2 pt-1"><button type="button" data-role-custom-back disabled={busy || imageBusy} aria-label="返回创建方式" onClick={() => goBack.current()} className="-ml-3 grid h-12 w-12 shrink-0 place-items-center rounded-full disabled:opacity-40"><ArrowLeft size={22} aria-hidden="true" /></button><h2 id="android-create-role-title" className="text-[25px] font-semibold">自定义创建角色</h2></div>
        <form className="min-h-0 flex-1 overflow-y-auto px-[22px] pb-6" onSubmit={(event) => void save(event)}>
          <p id="android-create-role-description" className="mb-5 text-sm leading-relaxed text-muted">填写角色性格，角色将据此判断和回应。</p>
          <RoleAvatarPicker id="role-custom-avatar" name={name} value={avatar} disabled={busy || saved} onChange={setAvatar} onBusyChange={setImageBusy} />
          <label className="android-field-label" htmlFor="role-custom-name">角色名称</label>
          <input id="role-custom-name" className="android-field" autoComplete="off" required maxLength={60} value={name} disabled={busy || saved} placeholder="为角色起一个名字" onChange={(event) => setName(event.target.value)} />
          <label className="android-field-label" htmlFor="role-custom-personality">角色性格</label>
          <textarea id="role-custom-personality" className="android-field min-h-[150px] resize-y" required maxLength={3000} value={personality} disabled={busy || saved} placeholder="例如：冷静、善于观察，有自己的原则，说话简洁。" onChange={(event) => setPersonality(event.target.value)} />
          <label className="android-field-label" htmlFor="role-custom-background">角色背景 <span className="text-muted">（选填）</span></label>
          <textarea id="role-custom-background" className="android-field min-h-[120px] resize-y" maxLength={3000} value={background} disabled={busy || saved} placeholder="经历、关系或当前情景" onChange={(event) => setBackground(event.target.value)} />
          <RoleVoiceSelect id="role-custom-voice" value={voiceId} disabled={busy || saved} onChange={setVoiceId} />
          {error && <p className="android-compose-error" role="alert">{error}</p>}
          {busy && <p className="android-small" role="status">{saved ? "正在刷新角色…" : "当前回合结束后创建并切换角色…"}</p>}
          <button type="submit" data-role-custom-submit className="android-button primary mt-5 w-full" disabled={busy || imageBusy}>{saved ? "刷新角色" : "创建并使用"}</button>
        </form>
      </> : <>
        <h2 id="android-create-role-title">创建角色</h2><p id="android-create-role-description">搜索角色资料，或自己填写角色性格与背景。</p>
        <div className="mt-5 flex flex-col gap-3"><button type="button" data-role-create-option="search" className="android-button" onClick={onSearch}><Search size={19} aria-hidden="true" />搜索并创建角色</button><button type="button" data-role-create-option="custom" className="android-button" onClick={() => setCustom(true)}><PencilLine size={19} aria-hidden="true" />自定义创建角色</button><button type="button" className="android-button" onClick={onClose}>取消</button></div>
      </>}
    </div>
  </div>, document.body);
}
