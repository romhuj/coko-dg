import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { ArrowUp, Loader2, Mic, Volume2, Repeat2 } from "lucide-react";
import { api, ChatTransportError, ChatUnsafeRetryError } from "../api";
import { labels } from "../commands";
import { useApp, useChat } from "../store";
import { useT } from "../i18n";
import { blocksUnsafeRetry, forgetPendingChat, readPendingChat, rememberPendingChat, type FrozenChatRequest } from "../chatRecovery";
import { useContinuousVoice } from "../useContinuousVoice";
import { useReplySpeech } from "../useReplySpeech";
import type { VoiceSendResult } from "../continuousVoice";
import { acceptArchivedMessages } from "../chatArchive";

export type ChatMode = "auto" | "text";


export default function ChatComposer({ mode, onModeChange, paired, active = true }: {
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  paired: boolean;
  active?: boolean;
}) {
  const t = useT();
  const s = useApp((st) => st.state);
  const isAndroid = s?.platform === "android";
  const busy = useChat((st) => st.busy);
  const historyLoading = useChat((st) => st.historyLoading);
  const setBusy = useChat((st) => st.setBusy);
  const setAwaitingConfirmation = useChat((st) => st.setAwaitingConfirmation);
  const pushMsg = useChat((st) => st.push);
  const [pendingRequest, setPendingRequest] = useState<FrozenChatRequest | null>(() => isAndroid ? readPendingChat() : null);
  const [draft, setDraft] = useState(() => pendingRequest?.keyboardDraft ?? (pendingRequest?.origin === "voice" ? "" : pendingRequest?.draft) ?? "");
  const draftRef = useRef(draft);
  const draftRevision = useRef(0);
  const drafts = useRef(new Map<string, string>());
  const draftConversation = useRef(s?.conversation_id);
  const trackedRequest = useRef<FrozenChatRequest | null>(pendingRequest);
  const [error, setError] = useState(() => pendingRequest ? "上次消息的结果尚未确认，请确认上次请求。" : "");
  const [voiceDraft, setVoiceDraft] = useState("");
  const [voiceError, setVoiceError] = useState("");
  const [blockedVoiceDraft, setBlockedVoiceDraft] = useState<string | null>(null);
  const voiceDraftRef = useRef(voiceDraft);
  voiceDraftRef.current = voiceDraft;
  const [blockedMessage, setBlockedMessage] = useState<string | null>(null);
  const blockedRetry = blocksUnsafeRetry(draft, blockedMessage);
  const [autoError, setAutoError] = useState("");
  const [autoBusy, setAutoBusy] = useState(false);
  const autoSending = useRef(false);
  const resumeSending = useRef(false);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [pattern, setPattern] = useState("");
  const [search, setSearch] = useState("");
  const sending = useRef(false);
  const composing = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const voicePreviewRef = useRef<HTMLDivElement>(null);
  const presets = s?.presets ?? [];
  const selected = presets.find((p) => p.name === pattern)?.name ?? "";
  const query = search.trim().toLocaleLowerCase();
  const matches = presets.filter((p) =>
    p.name === selected || [p.name, p.label, p.category, t(p.name), t(p.category)]
      .some((v) => v.toLocaleLowerCase().includes(query)),
  );
  const categories = [...new Set(matches.map((p) => p.category))];
  const deviceReady = (paired || s?.test_mode) && !s?.estop;
  const queued = busy && (s?.pending_chat ?? 0) > 0;

  useEffect(() => { if (pendingRequest) { setBusy(true); setAwaitingConfirmation(true); } }, [pendingRequest, setBusy, setAwaitingConfirmation]);

  const updateDraft = (value: string) => {
    draftRef.current = value;
    draftRevision.current += 1;
    setDraft(value);
    if (trackedRequest.current) {
      trackedRequest.current = { ...trackedRequest.current, keyboardDraft: value };
      rememberPendingChat(trackedRequest.current);
    }
  };

  useEffect(() => {
    if (draftConversation.current === s?.conversation_id || trackedRequest.current) return;
    if (draftConversation.current) drafts.current.set(draftConversation.current, draftRef.current);
    draftConversation.current = s?.conversation_id;
    updateDraft(drafts.current.get(s?.conversation_id ?? "") ?? "");
    setVoiceDraft(""); setVoiceError(""); setBlockedVoiceDraft(null); setBlockedMessage(null); setError("");
  }, [s?.conversation_id, pendingRequest]);

  const submit = async (input: { message: string; draft: string; origin?: "voice"; request?: FrozenChatRequest; reviewed?: boolean }): Promise<VoiceSendResult> => {
    const message = input.request?.message ?? input.message;
    if (!message || sending.current || ((useChat.getState().busy || useChat.getState().historyLoading) && !input.request) || (pendingRequest && !input.request)) return { status: "blocked" };
    if (blocksUnsafeRetry(message, blockedMessage) || (input.reviewed && blocksUnsafeRetry(message, blockedVoiceDraft))) return { status: "failed", unsafe: true, message: "上次结果无法安全重试。请先检查设备状态，修改消息后再发送。" };
    sending.current = true;
    const revision = draftRevision.current;
    setBusy(true);
    setAwaitingConfirmation(false);
    if (!input.origin || input.request) setError("");
    if (input.reviewed) setVoiceError("");
    let unresolved = false;
    let request = input.request;
    try {
      const sendMode = isAndroid ? "auto" : mode;
      if (isAndroid && !request) {
        request = { requestId: api.newChatRequestId(), message, draft: input.draft, mode: sendMode, origin: input.origin, keyboardDraft: draftRef.current, conversationId: s?.conversation_id };
        // Save before sending: a reload may lose the HTTP response after device actions ran.
      }
      if (isAndroid && request) {
        request = { ...request, keyboardDraft: draftRef.current };
        rememberPendingChat(request);
      }
      trackedRequest.current = request ?? null;
      const result = await api.chat(
        request?.message ?? message,
        request?.mode ?? sendMode,
        request?.preferredPattern ?? (!isAndroid && sendMode === "auto" ? selected || undefined : undefined),
        request?.requestId,
        request?.conversationId,
      );
      // A complete response is definitive, including model failures. A later retry may use a new ID.
      forgetPendingChat(request?.requestId);
      trackedRequest.current = null;
      setPendingRequest(null);
      if (result.conversation_id && Array.isArray(result.messages)) {
        acceptArchivedMessages(result.conversation_id, result.messages);
        window.dispatchEvent(new Event("coyote:conversations-changed"));
      }
      if (result.error) {
        if (result.retryable === false) throw new ChatUnsafeRetryError(result.error, request?.requestId);
        throw new Error(result.error);
      }
      const actions = labels(result);
      if (!result.line?.trim() && !actions) throw new Error(t("未收到回复，请重试。"));
      if (!result.messages?.length && (!result.conversation_id || result.conversation_id === useApp.getState().state?.conversation_id)) {
        pushMsg({ role: "user", text: request?.message ?? message });
        pushMsg({ role: "ai", text: result.line || t("设备操作已处理。"), speechText: result.line || "", actions, executed: result.executed, dropped: result.dropped });
      }
      if (result.persistence_error) useChat.setState({ historyError: "本轮已处理，但记录保存失败。请勿重复发送设备动作。" });
      setBlockedMessage(null);
      // Voice and late HTTP replies must never erase a newer keyboard draft.
      if (!input.origin && request?.origin !== "voice" && revision === draftRevision.current && draftRef.current === input.draft) {
        updateDraft("");
        if (isAndroid && inputRef.current) inputRef.current.style.height = "48px";
      }
      if (input.reviewed && voiceDraftRef.current === input.draft) { setVoiceDraft(""); setVoiceError(""); setBlockedVoiceDraft(null); }
      return { status: "sent" };
    } catch (exc) {
      if (exc instanceof ChatTransportError && request) {
        unresolved = true;
        setPendingRequest(trackedRequest.current ?? request);
        setError("连接中断，上次消息可能仍在处理。请确认上次请求，再发送新消息。");
        if (input.reviewed && voiceDraftRef.current === input.draft) setVoiceDraft("");
        if (!input.origin || input.request) voice.pauseForError("上次请求结果待确认，语音已暂停。");
        return { status: "uncertain", message: "上次请求结果待确认，语音已暂停。" };
      } else {
        forgetPendingChat(request?.requestId);
        trackedRequest.current = null;
        setPendingRequest(null);
        let message = exc instanceof Error ? exc.message : String(exc);
        if (exc instanceof ChatUnsafeRetryError) {
          setBlockedMessage(request?.message ?? input.message);
          message = `${exc.message}。请先检查设备状态，修改消息后再发送。`;
        }
        if (!input.origin && request?.origin !== "voice") {
          setError(message);
          voice.pauseForError("聊天发送未完成，语音已暂停。");
        } else if (input.reviewed || input.request) {
          setVoiceError(message);
          const text = input.request ? [input.request.draft, voiceDraftRef.current].filter(Boolean).join("\n") : voiceDraftRef.current;
          if (input.request) setVoiceDraft(text);
          if (exc instanceof ChatUnsafeRetryError) setBlockedVoiceDraft(text.trim());
        }
        return { status: "failed", message, unsafe: exc instanceof ChatUnsafeRetryError };
      }
    } finally {
      sending.current = false;
      // Keep role/context changes locked until an ambiguous request has a definitive outcome.
      setBusy(unresolved);
      setAwaitingConfirmation(unresolved);
      if (!unresolved && !input.origin && request?.origin !== "voice" && active) inputRef.current?.focus();
    }
  };

  const speech = useReplySpeech({
    enabled: isAndroid,
    active,
    contextKey: `${s?.chat_session_id ?? ""}:${s?.conversation_id ?? ""}:${s?.role ?? ""}:${s?.profile ?? ""}`,
    estop: s?.estop ?? false,
  });
  const speechOn = !!speech.sessionId;
  const voice = useContinuousVoice({
    enabled: isAndroid,
    active,
    contextKey: `${s?.chat_session_id ?? ""}:${s?.conversation_id ?? ""}:${s?.role ?? ""}:${s?.profile ?? ""}`,
    estop: s?.estop ?? false,
    busy: busy || historyLoading,
    blocked: () => sending.current || useChat.getState().busy || useChat.getState().historyLoading || !!trackedRequest.current,
    captureBlocked: speech.isPlaying,
    submit: (sentence) => submit({ message: sentence.text, draft: sentence.text, origin: "voice" }),
    review: (texts, message, unsafe) => {
      const text = [voiceDraftRef.current, ...texts].filter(Boolean).join("\n");
      voiceDraftRef.current = text;
      setVoiceDraft(text);
      setVoiceError(message);
      if (unsafe) setBlockedVoiceDraft(text.trim());
    },
    emergencyStop: async () => {
      window.dispatchEvent(new Event("coyote:voice-stop"));
      const result = await api.estop();
      pushMsg({ role: "sys", text: useApp.getState().state?.connected && !result.sent ? "语音急停已锁定，但设备未确认清零，请检查官方 App。" : "已语音急停，输出清零、自动运行停止。" });
    },
  });
  const voiceOn = ["preparing", "downloading", "listening"].includes(voice.status);
  const voicePreparing = voice.status === "preparing" || voice.status === "downloading";
  const voicePaused = voice.reason === "playback" || (!voice.duplex && !!speech.utteranceId);
  const voicePreview = voicePaused ? "" : voice.preview;
  useLayoutEffect(() => {
    if (voicePaused) voice.clearFeedback();
  }, [voicePaused]);
  useLayoutEffect(() => {
    if (voicePreviewRef.current) voicePreviewRef.current.scrollLeft = voicePreviewRef.current.scrollWidth;
  }, [voicePreview, voiceOn]);
  const send = async () => {
    if (composing.current) return;
    if (pendingRequest) await submit({ message: pendingRequest.message, draft: pendingRequest.draft, origin: pendingRequest.origin, request: pendingRequest });
    else await submit({ message: draft.trim(), draft });
  };

  useEffect(() => {
    setVoiceDraft("");
    setVoiceError("");
    setBlockedVoiceDraft(null);
  }, [s?.chat_session_id, s?.role, s?.profile]);

  useEffect(() => { if (!s?.estop) setAutoError(""); }, [s?.estop]);

  const resume = async () => {
    if (resumeSending.current || !s?.estop) return;
    resumeSending.current = true;
    setResumeBusy(true);
    setAutoError("");
    try {
      // Releasing protection only resumes eligibility; it never restarts autopilot.
      await api.resume();
      setAutoError("");
    } catch (exc) {
      setAutoError(exc instanceof Error ? exc.message : t("解除失败，请重试。"));
    } finally {
      resumeSending.current = false;
      setResumeBusy(false);
    }
  };

  const toggleAutopilot = async () => {
    if (autoSending.current) return;
    if (s?.estop && !s.autopilot) { setAutoError(""); return; }
    autoSending.current = true;
    setAutoBusy(true);
    setAutoError("");
    try {
      // The backend retains the emergency-stop gate and all channel safety caps.
      await api.setAutopilot(!s?.autopilot);
    } catch (exc) {
      setAutoError(exc instanceof Error ? exc.message : t("自动运行切换失败，请重试。"));
    } finally {
      autoSending.current = false;
      setAutoBusy(false);
    }
  };

  if (isAndroid) {
    return (
      <form className="android-composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
        {voiceOn && <div ref={voicePreviewRef} className="android-voice-preview" aria-label="实时语音转写" aria-live="off" dir="ltr" title={voicePreview || undefined}>
          {voicePreview || (voicePaused ? "正在朗读 · 暂停识别" : voicePreparing ? voice.status === "downloading" ? "正在下载语音资源…" : "正在准备语音…" : "正在聆听…")}
        </div>}
        <div className="android-composer-tools" aria-label={t("聊天功能")}>
          <button
            type="button"
            role="switch"
            aria-checked={s?.autopilot ?? false}
            aria-label={t("自动运行")}
            aria-busy={autoBusy}
            disabled={autoBusy}
            onClick={() => void toggleAutopilot()}
            className="android-auto-chip"
          >
            <Repeat2 size={16} aria-hidden="true" />
            <span>{t("自动运行")}</span>
          </button>
          <button
            type="button"
            className="android-voice-chip"
            aria-label={voiceOn ? voicePreparing ? "取消语音准备" : "停止语音输入" : "开启持续语音输入"}
            aria-pressed={voiceOn}
            data-listening={voice.status === "listening"}
            style={{ "--voice-level": voicePaused ? 0 : voice.level } as CSSProperties}
            disabled={!voiceOn && (!!pendingRequest || !!voiceDraft || historyLoading)}
            onClick={() => voiceOn ? voice.stop() : voice.start()}
          >
            {voicePreparing ? <Loader2 size={17} className="animate-spin" aria-hidden="true" /> : <span className="android-voice-glyph" aria-hidden="true"><Mic size={17} />{voiceOn && <span className="android-voice-meter"><i /><i /><i /></span>}</span>}
          </button>
          <button
            type="button"
            className="android-speech-chip"
            aria-label={speechOn ? "关闭回复朗读" : "开启回复朗读"}
            aria-pressed={speechOn}
            aria-busy={speech.status === "preparing"}
            onClick={() => speechOn ? speech.disable() : speech.enable()}
          >
            <Volume2 size={17} aria-hidden="true" />
          </button>
        </div>
        {voiceOn && <div className="android-voice-status" role="status" aria-live="polite">
          <span>{voicePreparing ? voice.message || "正在准备离线语音…" : voicePaused ? "正在朗读 · 暂停识别，耳机可边听边说" : `持续聆听 · ${speech.utteranceId && voice.duplex ? "说话可打断朗读" : "说完自动发送"}${voice.queued ? ` · 待发 ${voice.queued} 句` : ""}`}</span>
          {voice.status === "downloading" && <progress max={100} value={voice.progress} aria-label="语音资源下载进度" />}
          {voice.status === "downloading" && voice.progress !== undefined && <span>{Math.round(voice.progress)}%</span>}
          {voicePreparing && <button type="button" onClick={() => voice.stop()}>取消</button>}
        </div>}
        {speech.status === "preparing" && <p className="android-voice-status" role="status">{speech.message || "正在准备回复朗读…"}</p>}
        {speech.status === "error" && <p className="android-compose-error" role="alert">{speech.message}</p>}
        {voice.status === "error" && !voiceDraft && <p className="android-compose-error" role="alert">{voice.message}</p>}
        {s?.estop && <div className="android-resume-hint">
          <span>请先解除急停，再启用自动运行</span>
          <button type="button" disabled={resumeBusy} onClick={() => void resume()} aria-label="解除急停保护">{resumeBusy ? "解除中…" : "解除"}</button>
        </div>}
        {autoError && <p className="android-compose-error" role="alert">{autoError}</p>}
        {error && <p className={pendingRequest ? "android-chat-recovery" : "android-compose-error"} role="alert">
          {pendingRequest ? error : t("发送失败：{error}", { error })}
        </p>}
        {pendingRequest && <div className="android-pending-confirm">
          <p>{pendingRequest.origin === "voice" ? "待确认语音：" : "待确认消息："}{pendingRequest.message}</p>
          <button type="button" disabled={sending.current} onClick={() => void send()}>确认上次请求</button>
        </div>}
        {voiceDraft && <div className="android-voice-review">
          <label htmlFor="voice-review">待发转写</label>
          {voiceError && <p role="alert">{voiceError}</p>}
          <textarea id="voice-review" value={voiceDraft} rows={2} onChange={(event) => setVoiceDraft(event.target.value)} />
          {voiceDraft.trim().length > 8000 && <p>请将本次发送的内容缩短至 8000 字以内。</p>}
          <div>
            <button type="button" disabled={busy || historyLoading || !!pendingRequest || !voiceDraft.trim() || voiceDraft.trim().length > 8000 || blocksUnsafeRetry(voiceDraft, blockedMessage) || blocksUnsafeRetry(voiceDraft, blockedVoiceDraft)}
              onClick={() => { voice.stop(); void submit({ message: voiceDraft.trim(), draft: voiceDraft, origin: "voice", reviewed: true }); }}>发送转写</button>
            <button type="button" disabled={sending.current} onClick={() => { setVoiceDraft(""); setVoiceError(""); setBlockedVoiceDraft(null); }}>丢弃</button>
          </div>
        </div>}
        <label className="sr-only" htmlFor="chat-message">{t("聊天消息")}</label>
        <div className="android-compose-box">
          <textarea
            id="chat-message"
            ref={inputRef}
            rows={1}
            value={draft}
            maxLength={8000}
            onChange={(event) => {
              updateDraft(event.target.value);
              if (!pendingRequest) setError(blocksUnsafeRetry(event.target.value, blockedMessage)
                ? "上次结果无法安全重试。请先检查设备状态，修改消息后再发送。" : "");
              event.target.style.height = "48px";
              event.target.style.height = `${Math.min(104, event.target.scrollHeight)}px`;
            }}
            onCompositionStart={() => { composing.current = true; }}
            onCompositionEnd={() => { composing.current = false; }}
            placeholder={t("说说你现在的感受…")}
          />
          <button
            type="submit"
            disabled={!draft.trim() || blockedRetry || busy || historyLoading || !!pendingRequest}
            className="android-send"
            aria-label={t(blockedRetry ? "请修改消息后再发送" : pendingRequest ? "请先确认上次请求" : queued ? "消息排队中" : busy ? "正在回复" : error ? "重试发送" : "发送消息")}
          >
            {busy && !pendingRequest ? <Loader2 size={22} className="animate-spin" aria-hidden="true" /> : <ArrowUp size={22} aria-hidden="true" />}
          </button>
        </div>
      </form>
    );
  }

  return (
    <form className={`flex-none space-y-2 border-t border-line bg-ink2 px-3.5 pb-4 pt-3 ${isAndroid ? "android-composer" : ""}`} onSubmit={(e) => {
      e.preventDefault();
      void send();
    }}>
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-lg border border-line bg-panel p-0.5" role="group" aria-label={t("聊天模式")}>
          {(["auto", "text"] as const).map((value) => (
            <button
              key={value}
              type="button"
              disabled={busy}
              aria-pressed={mode === value}
              onClick={() => { onModeChange(value); setError(""); }}
              className={`rounded-md px-3 py-1 text-xs transition-colors disabled:opacity-50 ${
                mode === value ? "bg-accent font-semibold text-ink" : "text-muted hover:text-text"
              }`}
            >
              {t(value === "text" ? "仅文字" : "情景互动")}
            </button>
          ))}
        </div>
        <span className="chat-mode-hint text-[10px] text-faint">
          {mode === "text"
            ? t(s?.autopilot ? "本条不调用设备；自动运行仍已开启" : "无需配对，本条消息只进行文字对话")
            : t(s?.estop
              ? "急停中仍可聊天，设备操作保持暂停"
              : deviceReady
                ? "角色结合性格与情景，自主决定是否调整强度或波形，受 A/B 上限限制"
                : "未配对时正常聊天；连接后由角色决定设备动作")}
        </span>
      </div>
      {s?.autopilot && (
        <p className="chat-autopilot-hint text-[11px] text-muted">
          {t("自动运行与聊天可同时进行，消息按顺序处理")}
        </p>
      )}
      {mode === "auto" && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("搜索波形或分类")}
            aria-label={t("搜索波形或分类")}
            className="min-w-0 flex-1 rounded-md border border-line bg-panel px-2 py-1.5 text-xs outline-none focus:border-accent/60"
          />
          <select
            value={selected}
            onChange={(e) => setPattern(e.target.value)}
            disabled={busy}
            aria-label={t("本条消息的波形")}
            className="min-w-0 flex-1 rounded-md border border-line bg-panel px-2 py-1.5 text-xs outline-none focus:border-accent/60 disabled:opacity-50"
          >
            <option value="">{t("波形：AI 自动选择")}</option>
            {categories.map((category) => (
              <optgroup key={category} label={t(category)}>
                {matches.filter((p) => p.category === category).map((p) => (
                  <option key={p.name} value={p.name}>{t(p.name)}</option>
                ))}
              </optgroup>
            ))}
          </select>
          {query && matches.length === 0 && <span className="text-[10px] text-faint">{t("没有匹配的波形")}</span>}
        </div>
      )}
      {error && <p className="break-words text-xs text-bad" role="alert">{t("发送失败：{error}", { error })}</p>}
      <label className="sr-only" htmlFor="chat-message">{t("聊天消息")}</label>
      <div className="flex items-end gap-2">
        <textarea
          id="chat-message"
          ref={inputRef}
          rows={isAndroid ? 2 : 3}
          value={draft}
          maxLength={8000}
          readOnly={busy}
          onChange={(e) => { updateDraft(e.target.value); setError(blocksUnsafeRetry(e.target.value, blockedMessage) ? "上次结果无法安全重试。请先检查设备状态，修改消息后再发送。" : ""); }}
          onCompositionStart={() => { composing.current = true; }}
          onCompositionEnd={() => { composing.current = false; }}
          onKeyDown={(e) => {
            if (isAndroid || e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing || e.keyCode === 229 || composing.current) return;
            e.preventDefault();
            void send();
          }}
          placeholder={t("输入你想说的话…")}
          className="max-h-40 min-h-20 min-w-0 flex-1 resize-y rounded-xl border border-line2 bg-panel px-3 py-2 text-[13px] leading-relaxed outline-none placeholder:text-faint focus:border-accent/60 read-only:opacity-70"
        />
        <button
          type="submit"
          disabled={!draft.trim() || busy || blockedRetry}
          className="rounded-xl bg-accent px-4 py-2.5 text-xs font-semibold text-ink transition-opacity disabled:cursor-not-allowed disabled:opacity-40"
        >
          {t(queued ? "排队中…" : busy ? "回复中…" : error ? "重试发送" : "发送")}
        </button>
      </div>
      <div className="flex justify-between gap-2 text-[10px] text-faint">
        <span>{t(isAndroid ? "点击发送 · 回车换行" : "Enter 发送 · Shift + Enter 换行")}</span>
        <span role="status" aria-live="polite">
          {queued ? t("消息已排队，当前回合结束后回复") : busy ? t("正在等待回复…") : `${draft.length} / 8000`}
        </span>
      </div>
    </form>
  );
}
