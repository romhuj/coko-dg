import { useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, ExternalLink, Loader2, Search, X } from "lucide-react";
import { api } from "../api";
import { useApp, useChat } from "../store";
import type { CharacterSearchResult } from "../types";
import type { SpeechVoiceId } from "../replySpeech";
import RoleVoiceSelect from "./RoleVoiceSelect";
import RoleAvatarPicker from "./RoleAvatarPicker";
import { conversationSwitchBlocked } from "../chatArchive";

interface RoleSearchProps {
  onCreated: () => void;
  onClose: () => void;
}

export default function RoleSearch({ onCreated, onClose }: RoleSearchProps) {
  const isAndroid = useApp((state) => state.state?.platform === "android");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<CharacterSearchResult | null>(null);
  const [sourceIndex, setSourceIndex] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [voiceId, setVoiceId] = useState<SpeechVoiceId>("melo-zh");
  const [avatar, setAvatar] = useState<string | null>(null), [imageBusy, setImageBusy] = useState(false);
  const [searching, setSearching] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createdPending, setCreatedPending] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const queryRef = useRef<HTMLInputElement>(null);
  const busy = searching || creating || imageBusy;
  const locked = busy || createdPending;

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (isAndroid) dialogRef.current?.focus({ preventScroll: true });
    else queryRef.current?.focus();
    return () => previous?.focus();
  }, [isAndroid]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (!busy) onClose();
      }
      if (event.key !== "Tab") return;
      const nodes = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
      );
      const focusable = [
        ...Array.from(nodes ?? []),
        ...(isAndroid ? Array.from(document.querySelectorAll<HTMLElement>('[data-coyote-estop]:not(:disabled)')) : []),
      ].filter((node) => node.getClientRects().length > 0);
      // Android keeps the global stop control in the same keyboard focus cycle.
      if (isAndroid && focusable.length) {
        event.preventDefault();
        const index = focusable.indexOf(document.activeElement as HTMLElement);
        const next = index < 0 ? (event.shiftKey ? focusable.length - 1 : 0)
          : (index + (event.shiftKey ? -1 : 1) + focusable.length) % focusable.length;
        focusable[next].focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first) {
        event.preventDefault();
        dialogRef.current?.focus();
      } else if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [busy, isAndroid, onClose]);

  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (locked || !query.trim()) return;
    setSearching(true);
    setError("");
    setResult(null);
    setSourceIndex(null);
    setName("");
    try {
      const data = await api.searchCharacter(query.trim());
      setResult(data);
      if (!data.sources.length) setError("未找到可用资料，请补充作品名称或尝试角色英文名。");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "搜索失败，请检查网络后重试。");
    } finally {
      setSearching(false);
    }
  };

  const create = async () => {
    if (busy || !result || sourceIndex === null || !name.trim()) return;
    if (isAndroid && !createdPending && conversationSwitchBlocked()) { setError("请等待当前消息完成或确认上次请求后，再创建角色。"); return; }
    setCreating(true);
    setError("");
    let saved = createdPending;
    try {
      if (!saved) {
        await api.createCharacter(result.search_id, sourceIndex, name.trim(), note.trim(), isAndroid ? voiceId : undefined, isAndroid ? avatar : undefined);
        saved = true;
        setCreatedPending(true);
      }
      const state = await api.state();
      useApp.setState({ state });
      if (useApp.getState().state?.platform !== "android") useChat.getState().clear();
      onCreated();
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : "请稍后重试。";
      setError(saved ? `角色已保存，状态刷新失败：${message} 可点击下方按钮重新刷新。` : message);
    } finally {
      setCreating(false);
    }
  };

  return createPortal(
    <div
      className={isAndroid
        ? "android-role-search-page fixed inset-x-0 bottom-0 top-16 z-[100] flex bg-ink"
        : "fixed inset-0 z-[100] flex items-center justify-center bg-black/65 p-3 backdrop-blur-sm sm:p-6"}
      onPointerDown={(event) => { if (isAndroid) event.stopPropagation(); }}
      onPointerUp={(event) => { if (isAndroid) event.stopPropagation(); }}
      onMouseDown={(event) => {
        event.stopPropagation();
        if (!isAndroid && event.target === event.currentTarget && !busy) onClose();
      }}
      onClick={(event) => event.stopPropagation()}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal={!isAndroid}
        aria-labelledby="role-search-title"
        aria-describedby="role-search-description"
        aria-busy={busy}
        tabIndex={-1}
        className={isAndroid
          ? "flex h-full min-h-0 w-full flex-col overflow-hidden bg-ink text-text outline-none"
          : "flex max-h-[calc(100dvh-24px)] w-full max-w-[640px] flex-col overflow-hidden rounded-2xl border border-line bg-panel text-text shadow-2xl"}
      >
        {isAndroid ? (
          <div className="flex shrink-0 items-center gap-2 px-[22px] pb-2 pt-1">
            <button
              type="button"
              aria-label="返回角色"
              disabled={busy}
              onClick={onClose}
              className="-ml-3 grid h-12 w-12 shrink-0 place-items-center rounded-full disabled:opacity-40"
            ><ArrowLeft size={22} aria-hidden="true" /></button>
            <h2 id="role-search-title" className="text-[25px] font-semibold">创建角色</h2>
          </div>
        ) : (
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div>
            <h2 id="role-search-title" className="text-base font-semibold">搜索并创建角色</h2>
            <p id="role-search-description" className="mt-1 text-xs leading-relaxed text-muted">
              输入角色名，查看网络资料，选择匹配的来源后创建。
            </p>
          </div>
          <button
            type="button"
            aria-label="关闭角色搜索"
            disabled={busy}
            onClick={onClose}
            className="shrink-0 rounded-lg p-1.5 text-muted transition hover:bg-panel2 hover:text-text disabled:cursor-wait disabled:opacity-40"
          ><X size={18} /></button>
        </div>
        )}

        <div className={isAndroid ? "min-h-0 flex-1 space-y-5 overflow-y-auto px-[22px] pb-6 pt-2" : "min-h-0 space-y-5 overflow-y-auto px-5 py-4"}>
          {isAndroid && <p id="role-search-description" className="text-sm leading-relaxed text-muted">搜索角色资料，保留他的性格与判断。</p>}
          <form onSubmit={(event) => void search(event)}>
            <label htmlFor="role-search-query" className="mb-2 block text-xs font-medium">角色名 / 作品名</label>
            <div className="flex gap-2">
              <input
                ref={queryRef}
                id="role-search-query"
                value={query}
                maxLength={100}
                disabled={locked}
                placeholder="例如：福尔摩斯"
                autoComplete="off"
                onChange={(event) => {
                  setQuery(event.target.value);
                  setResult(null);
                  setSourceIndex(null);
                  setName("");
                  setError("");
                }}
                className={`min-w-0 flex-1 rounded-lg border border-line bg-panel2 px-3 py-2 text-sm outline-none focus:border-accent disabled:opacity-60 ${isAndroid ? "min-h-12" : ""}`}
              />
              <button
                type="submit"
                disabled={locked || !query.trim()}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-accent/40 bg-accent/15 px-3 py-2 text-sm text-accent transition hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {searching ? <Loader2 className="animate-spin" size={15} /> : <Search size={15} />}
                {searching ? "搜索中" : "搜索"}
              </button>
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-faint">资料来自中英文维基百科与 Wikidata。同名角色可加上作品名称缩小范围。</p>
          </form>

          {searching && <p role="status" className="text-center text-sm text-muted">正在检索角色资料…</p>}

          {result?.warnings?.length ? (
            <div role="status" className="rounded-lg border border-warn/30 bg-warn/5 p-3 text-xs leading-relaxed text-warn">
              {result.warnings.map((warning, index) => <p key={index}>{warning}</p>)}
            </div>
          ) : null}

          {result && result.sources.length > 0 && (
            <fieldset disabled={locked} className="min-w-0 space-y-2">
              <legend className="mb-2 text-xs font-medium">选择一条匹配的资料 · {result.sources.length} 个结果</legend>
              {result.sources.map((source, index) => (
                <div
                  key={`${source.url}:${index}`}
                  className={`rounded-xl border p-3 transition ${sourceIndex === index ? "border-accent/60 bg-accent/5" : "border-line bg-panel2/50"}`}
                >
                  <label className={`flex items-start gap-2.5 ${locked ? "cursor-default" : "cursor-pointer"}`}>
                    <input
                      type="radio"
                      name="character-source"
                      aria-label={`选择资料：${source.title}`}
                      checked={sourceIndex === index}
                      onChange={() => {
                        setSourceIndex(index);
                        setName(source.title.slice(0, 60));
                        setError("");
                      }}
                      className="mt-1 accent-[var(--color-accent)]"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block break-words text-sm font-medium">{source.title}</span>
                      <span className="mt-1 block text-[10px] text-faint">{source.provider} · {source.language === "zh" ? "中文" : source.language === "en" ? "英文" : source.language}</span>
                      <span className={`mt-2 block break-words text-xs leading-relaxed text-muted ${sourceIndex === index ? "" : "line-clamp-3"}`}>{source.summary}</span>
                    </span>
                  </label>
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    referrerPolicy="no-referrer"
                    className="ml-6 mt-2 inline-flex items-center gap-1 text-[11px] text-accent hover:underline"
                  >查看来源 <ExternalLink size={11} /></a>
                </div>
              ))}
            </fieldset>
          )}

          {sourceIndex !== null && (
            <div className="space-y-3 border-t border-line pt-4">
              {isAndroid && <RoleAvatarPicker id="role-search-avatar" name={name} value={avatar} disabled={locked} onChange={setAvatar} onBusyChange={setImageBusy} />}
              <div>
                <label htmlFor="role-search-name" className="mb-2 block text-xs font-medium">角色显示名称</label>
                <input
                  id="role-search-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  maxLength={60}
                  disabled={locked}
                  className={`w-full rounded-lg border border-line bg-panel2 px-3 py-2 text-sm outline-none focus:border-accent disabled:opacity-60 ${isAndroid ? "min-h-12" : ""}`}
                />
              </div>
              <div>
                <label htmlFor="role-search-note" className="mb-2 block text-xs font-medium">性格、动机、说话习惯 <span className="font-normal text-faint">（可选）</span></label>
                <textarea
                  id="role-search-note"
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  placeholder="例如：冷静独立，重视推理；说话简洁，会坚持自己的判断。"
                  maxLength={1500}
                  rows={3}
                  disabled={locked}
                  className="w-full resize-y rounded-lg border border-line bg-panel2 px-3 py-2 text-sm outline-none focus:border-accent disabled:opacity-60"
                />
              </div>
              {isAndroid && <RoleVoiceSelect id="role-search-voice" value={voiceId} disabled={locked} onChange={setVoiceId} />}
              <p className="text-[11px] leading-relaxed text-faint">以所选资料为基础，补充角色的性格、动机与表达习惯，并开启新的角色对话。</p>
            </div>
          )}
          {error && <p role="alert" className="break-words rounded-lg border border-bad/30 bg-bad/5 px-3 py-2 text-xs leading-relaxed text-bad">{error}</p>}
        </div>

        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-line px-5 py-4">
          {!isAndroid && <button
            type="button"
            disabled={busy}
            onClick={onClose}
            className="rounded-lg border border-line px-4 py-2 text-sm text-muted hover:bg-panel2 disabled:cursor-wait disabled:opacity-40"
          >取消</button>}
          <button
            type="button"
            disabled={busy || sourceIndex === null || !name.trim()}
            onClick={() => void create()}
            className={isAndroid
              ? "inline-flex min-h-12 w-full items-center justify-center gap-2 rounded-full bg-accent px-4 py-2 text-sm font-medium text-ink disabled:cursor-not-allowed disabled:opacity-40"
              : "inline-flex items-center gap-2 rounded-lg border border-accent/40 bg-accent/20 px-4 py-2 text-sm font-medium text-accent hover:bg-accent/30 disabled:cursor-not-allowed disabled:opacity-40"}
          >
            {creating && <Loader2 className="animate-spin" size={15} />}
            {creating ? "正在保存…" : createdPending ? "刷新状态并完成" : "创建并使用"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
