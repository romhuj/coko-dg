import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useApp, useChat, type ChatMsg } from "../store";
import { chatFeedback } from "../chatFeedback";
import { useT } from "../i18n";
import { INTENSITY_BADGE_CLS, entryOf } from "../roleTheme";
import ChatComposer, { type ChatMode } from "./ChatComposer";
import AndroidPairing from "./AndroidPairing";
import { refreshConversation } from "../chatArchive";

export default function ChatPanel({ active = true }: { active?: boolean }) {
  const t = useT();
  const messages = useChat((st) => st.messages);
  const pushMsg = useChat((st) => st.push);
  const clearChat = useChat((st) => st.clear);
  const busy = useChat((st) => st.busy);
  const historyLoading = useChat((st) => st.historyLoading);
  const historyError = useChat((st) => st.historyError);
  const historyHasMore = useChat((st) => st.historyHasMore);
  const s = useApp((st) => st.state);
  const isAndroid = s?.platform === "android";
  const autopilot = useApp((st) => st.state?.autopilot ?? false);
  const relay = useApp((st) => st.state?.relay);
  const paired = s?.connected === true || relay?.status === "paired" || relay?.status === "ready";
  const testMode = useApp((st) => st.state?.test_mode ?? false);
  const lang = useApp((st) => st.state?.lang ?? "zh");
  const enAvailable = useApp((st) => st.state?.en_available ?? false);
  const estop = useApp((st) => st.state?.estop ?? false);
  const controllerId = relay?.controller_id;
  const enabled = useApp((st) => st.state?.enabled_channels);
  const role = useApp((st) => st.state?.role ?? "触手");
  const profile = useApp((st) => st.state?.profile ?? "纯爱");
  const intensity = useApp((st) => st.state?.intensity_level ?? "中");
  const entry = entryOf(role, profile);
  const roleLabel = s?.roles?.find((r) => r.name === role)?.label ?? entry?.label ?? role;
  const [chatMode, setChatMode] = useState<ChatMode>("auto");
  const [showPairing, setShowPairing] = useState(false);
  const [qrFail, setQrFail] = useState(false);   // 二维码加载失败（后端 503 / relay 未就绪）
  const [qrTick, setQrTick] = useState(0);       // 重试计数器（同时刷新 img key 与请求 URL）
  const [qrRetries, setQrRetries] = useState(0); // 自动重试最多 6 次，避免 relay 异常时无限请求
  const boxRef = useRef<HTMLDivElement>(null);
  const olderScroll = useRef<{ height: number; top: number } | null>(null);

  // T058 P1.3：最后一个 "[sim]" 分界（进入测试模式）之前的消息块视为“切换前历史”
  const simSince = messages.reduce(
    (acc, m, i) => ((m.text ?? "").startsWith("[sim]") ? i : acc),
    -1,
  );

  // 切换角色/内容档时插一条系统分隔消息，防上下文串戏
  const roleKeyRef = useRef(`${role}·${profile}`);
  useEffect(() => {
    const key = `${role}·${profile}`;
    if (roleKeyRef.current !== key) {
      roleKeyRef.current = key;
      pushMsg({
        role: "sys",
        text: t("—— 已切换至 {x} ——", { x: t(roleLabel) }),
      });
    }
  }, [role, profile, roleLabel, pushMsg]);

  useEffect(() => {
    if (olderScroll.current && boxRef.current) {
      boxRef.current.scrollTop = olderScroll.current.top + boxRef.current.scrollHeight - olderScroll.current.height;
      olderScroll.current = null;
      return;
    }
    boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight });
  }, [messages, busy]);

  // 中继重连时清除旧二维码的失败状态，允许新控制器重新尝试。
  useEffect(() => {
    setQrFail(false);
    setQrRetries(0);
  }, [controllerId]);

  // 二维码重试：加载失败（relay 未就绪 503）时定时重试；relay controller_id 就绪后立即重载
  useEffect(() => {
    if (isAndroid || paired || qrRetries >= 6 || (!qrFail && controllerId)) return;
    const timer = window.setTimeout(() => {
      setQrFail(false);
      setQrRetries((n) => n + 1);
      setQrTick((t) => t + 1);
    }, 4000); // 限频：失败后每 4s 重试，最多 6 次
    return () => window.clearTimeout(timer);
  }, [isAndroid, paired, qrFail, qrRetries, controllerId]);

  const toggle = async () => {
    try {
      await api.setAutopilot(!autopilot);
    } catch {
      /* 状态由 ws 推送刷新 */
    }
  };

  if (isAndroid) {
    return (
      <aside className="android-chat flex h-full min-h-0 flex-col">
        <div ref={boxRef} className="android-chat-scroll" aria-label={t("聊天记录")}>
          {historyHasMore && <button className="android-history-more" type="button" disabled={historyLoading} onClick={() => {
            if (boxRef.current) olderScroll.current = { height: boxRef.current.scrollHeight, top: boxRef.current.scrollTop };
            void refreshConversation(true).then(() => { if (useChat.getState().historyError) olderScroll.current = null; });
          }}>加载更早的消息</button>}
          {historyLoading && <p className="android-small" role="status">正在加载聊天记录…</p>}
          {historyError && <div className="android-history-error"><p role="alert">{historyError}</p><button type="button" onClick={() => void refreshConversation()}>重新加载</button></div>}
          {!messages.length && <p className="android-chat-empty">{t("与 {role} 聊天", { role: t(roleLabel) })}</p>}
          {messages.map((message, index) => (
            <div key={message.id ?? index} className={`android-message ${message.role}`}>
              <div className="whitespace-pre-wrap break-words">{message.text}</div>
              {message.role === "ai" && <CommandFeedback message={message} />}
            </div>
          ))}
          <TurnProgress />
        </div>
        <ChatComposer mode="auto" onModeChange={setChatMode} paired={paired} active={active} />
      </aside>
    );
  }

  return (
    <aside className={`flex h-full min-h-0 flex-col border-l border-line bg-ink2 ${isAndroid ? "android-chat" : "pb-14"}`}>
      <div className="chat-header flex flex-none flex-wrap items-center gap-y-2 border-b border-line px-4 py-2">
        <div className="flex items-center gap-2 rounded-lg border border-line bg-panel2 px-2.5 py-1">
          <span className="text-[11px] text-faint">{t("当前入口")}</span>
          <span className="text-[12px] font-semibold text-text">{t(roleLabel)}</span>
          <span
            className={`rounded-md border px-1.5 py-px text-[10px] ${INTENSITY_BADGE_CLS[intensity] ?? INTENSITY_BADGE_CLS["中"]}`}
            title={t("设备互动强度")}
          >
            {t("强度 {v}", { v: t(intensity) })}
          </span>
        </div>
        {/* AI 内容语言切换：中/EN（放「当前入口」右边；无英文稿的角色禁用 EN） */}
        <div
          className="ml-2 flex items-center overflow-hidden rounded-md border border-line text-[11px]"
          title={
            enAvailable
              ? t("AI 内容语言（提示词/台词/语料）：中文 / English")
              : t("当前角色暂无英文版（英文稿未随内容提供）")
          }
        >
          {(["zh", "en"] as const).map((l) => (
            <button
              key={l}
              onClick={() => {
                if (l === lang) return;
                try {
                  localStorage.setItem("lang", l);
                } catch {
                  /* 记忆失败不影响本次切换 */
                }
                void api.setLang(l).catch(() => {});
              }}
              disabled={l === "en" && !enAvailable}
              className={`px-2 py-1 transition-colors ${
                l === lang
                  ? "bg-accent font-semibold text-ink"
                  : "bg-panel2 text-muted hover:text-text"
              } ${l === "en" && !enAvailable ? "cursor-not-allowed opacity-40" : ""}`}
            >
              {l === "zh" ? "ZH" : "EN"}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-[11px] text-muted">{t("自动运行")}</span>
          <button
            onClick={() => void toggle()}
            data-tour="autopilot"
            title={t(autopilot ? "停止自动运行" : isAndroid ? "开始自动运行（AI 自主回合）" : "开始自动运行（AI 自主回合，摄像头/麦克风跟随启停）")}
            className={`relative h-5 w-9 flex-none rounded-full transition-colors ${
              autopilot ? "bg-accent" : "bg-ink3 border border-line"
            }`}
          >
            <span
              className={`absolute top-1/2 h-4 w-4 -translate-y-1/2 rounded-full transition-all ${
                autopilot ? "left-[18px] bg-ink" : "left-[2px] bg-muted"
              }`}
            />
          </button>
          {testMode && (
            <button
              onClick={() => void api.testMode(false)}
              title={t("退出测试模式，恢复扫码配对")}
              className="rounded-md border border-line bg-warn/10 px-2 py-1 text-[11px] text-warn transition-colors hover:border-line2 hover:text-text"
            >
              {t("测试 ✕")}
            </button>
          )}
          <button
            disabled={busy}
            onClick={() => {
              if (!window.confirm(t("清空对话历史？将清空聊天记录与 AI 的记忆上下文，设备强度不受影响。"))) return;
              api
                .clearHistory()
                .then(() => {
                  clearChat();
                  pushMsg({ role: "sys", text: t("—— 对话历史已清空 ——") });
                })
                .catch(() => {});
            }}
            className="rounded-md border border-line bg-panel2 px-2 py-1 text-[11px] text-muted transition-colors hover:border-line2 hover:text-text disabled:opacity-40"
          >
            {t("清空")}
          </button>
        </div>
      </div>
      {estop && (
        <div className="chat-estop-hint flex-none border-b border-bad/60 bg-bad/15 px-3 py-1.5 text-center text-[11px] font-semibold text-bad">
          {t(isAndroid ? "设备处于急停保护，文字聊天可用；恢复设备请点「解除急停」" : "⛔ 已急停：设备已清零、AI 已暂停——到底部按「解除」恢复")}
        </div>
      )}
      {!paired && (
        <div className="chat-connection-hint flex flex-none flex-wrap items-center justify-between gap-2 border-b border-line px-3.5 py-2 text-xs text-muted">
          <span>{t("未配对设备，可以直接文字聊天")}</span>
          <button
            type="button"
            aria-expanded={showPairing}
            onClick={() => setShowPairing((value) => !value)}
            className="rounded-md border border-line px-2 py-1 text-accent"
          >
            {t(showPairing ? "收起配对" : "配对设备")}
          </button>
        </div>
      )}
      <div ref={boxRef} className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-3.5" aria-label={t("聊天记录")}>
        {!paired && showPairing && (
          <div className="flex flex-none flex-col items-center justify-center gap-2 rounded-xl border border-line bg-panel/50 px-4 py-4 text-center" data-tour="pair-qr">
            {!controllerId || qrFail ? (
              <div className="flex h-48 w-48 flex-col items-center justify-center gap-1 rounded-lg border border-line bg-panel2 p-2">
                <span className="text-[13px] text-muted">
                  {controllerId ? t("二维码加载失败") : t("等待中继就绪，正在生成二维码…")}
                </span>
                <span className="text-[10px] text-faint">
                  {qrRetries >= 6
                    ? t("自动重试已暂停，请确认中继服务后点击重试")
                    : controllerId
                      ? t("会自动重试，或点下方手动重试")
                      : t("二维码会自动重试，稍候即可")}
                </span>
                <button
                  onClick={() => {
                    setQrFail(false);
                    setQrRetries(0);
                    setQrTick((t) => t + 1);
                  }}
                  className="mt-1 rounded-md border border-line px-3 py-1 text-[11px] text-accent/80 hover:border-line2 hover:text-accent"
                >
                  {t("重试")}
                </button>
              </div>
            ) : (
              <img
                key={`${controllerId}:${qrTick}`}
                src={`/api/qrcode.png?controller_id=${encodeURIComponent(controllerId)}&retry=${qrTick}`}
                alt={t("配对二维码")}
                className="h-48 w-48 rounded-lg border border-line bg-white p-2"
                onLoad={() => {
                  setQrFail(false);
                  setQrRetries(0);
                }}
                onError={() => setQrFail(true)}
              />
            )}
            <div className="mt-2 text-[13px] font-semibold text-muted">{t("手机打开 DG-LAB 4.0 App 的「Socket V4」扫码配对")}</div>
            {relay?.last_error && (
              <div className="max-w-full break-all text-xs text-bad" role="status">
                {t("中继连接错误：{error}", { error: relay.last_error })}
              </div>
            )}
            <div className="mt-1 text-[11px] text-warn">{t("⚠️ 配对后请在 App 里打开「输出」开关（解除屏蔽），否则设备不会有感觉")}</div>
            {isAndroid && <AndroidPairing />}
            {!isAndroid && <><div className="mt-3 flex w-full items-center gap-2 text-[10px] text-faint">
              <span className="h-px flex-1 bg-line" />
              {t("没有郊狼？")}
              <span className="h-px flex-1 bg-line" />
            </div>
            <button
              onClick={() => {
                // T058 P1.3：进测试模式时 push 一条分界，让此线以上的 ✖ 未发送 明确是切换前历史
                void api.testMode(true)
                  .then(() =>
                    pushMsg({
                      role: "sys",
                      text: "[sim] " + t("已进入测试模式：上方 ✖ 未发送 均为切换前（真实设备）的历史"),
                    }),
                  )
                  .catch(() => {});
              }}
              title={t("不连接设备，用模拟设备试跑聊天 / 地牢 / AI 全流程（不会真正电击）")}
              className="mt-1 rounded-lg border border-line2 bg-panel2 px-5 py-1.5 text-[12px] font-medium text-accent transition-colors hover:border-accent/60 hover:text-accent"
            >
              {t("点击进入测试（模拟设备）")}
            </button></>}
          </div>
        )}
        {chatMode === "auto" && paired && enabled !== undefined && !enabled.A && !enabled.B && (
          <div className="self-center rounded-xl border border-bad/50 bg-bad/10 px-3 py-2 text-center text-[11px] text-bad">
            {t(isAndroid ? "所有通道已关闭。需要设备互动时，请在「设备」页开启通道。" : "⚠️ 所有通道都已关闭，AI 不会输出任何刺激——请在左侧通道卡打开至少一个通道")}
          </div>
        )}
        {!messages.length && (
          <div className="my-auto self-center px-4 py-6 text-center">
              <div className="text-sm font-semibold text-text">{t("与 {role} 聊天", { role: t(roleLabel) })}</div>
              <p className="mt-2 max-w-sm text-xs leading-relaxed text-muted">{t("直接输入消息，角色会结合对话情景回应。自动运行开启时也能聊天。")}</p>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className="flex flex-col">
            <div
              className={`max-w-[90%] whitespace-pre-wrap break-words rounded-xl px-3 py-2 text-[13px] leading-relaxed ${
                m.role === "user"
                  ? "self-end border border-line2 bg-accent/15 rounded-br"
                  : m.role === "ai"
                    ? "self-start mr-auto border border-line bg-ink3 rounded-bl"
                    : "self-center text-[11px] text-muted"
              }`}
            >
              {m.text}
            </div>
            {m.actions && (() => {
              const lines = m.actions.split("\n").filter(Boolean);
              const done = lines.filter((l) => l.startsWith("▶"));
              const skipped = lines.filter((l) => l.startsWith("✖") || l.startsWith("×"));
              if (done.length === 0 && skipped.length === 0) return null;
              const chip = (s: string, j: number, cls: string) => (
                <span key={`${j}-${s}`} className={`rounded-md border px-1.5 py-0.5 text-[10px] ${cls}`}>
                  {s.replace(/^[▶✖×]/, "")}
                </span>
              );
              // T058 P1.3：最后一个 "[sim]" 分界之前的 ✖ 块按“切换前历史”淡化
              const historic = simSince >= 0 && i < simSince;
              return (
                <div
                  className={`mt-1 flex max-w-[90%] flex-col gap-1 border-t border-dashed border-line pt-1 ${
                    m.role === "user" ? "self-end" : "self-start"
                  }`}
                >
                  {done.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1">
                      <span className="text-[10px] text-emerald-400/80">{t("✓ 已执行 {n}", { n: done.length })}</span>
                      {done.map((a, j) => chip(a, j, "border-emerald-500/30 bg-emerald-500/10 text-emerald-200/90"))}
                    </div>
                  )}
                  {skipped.length > 0 && (
                    <div
                      title={historic ? t("切换测试模式前的历史") : undefined}
                      className={`flex flex-wrap items-center gap-1 ${historic ? "opacity-50 saturate-50" : ""}`}
                    >
                      <span className="text-[10px] text-red-400/80">{t("✖ 未发送 {n}", { n: skipped.length })}</span>
                      {skipped.map((a, j) => chip(a, j, "border-red-500/30 bg-red-500/10 text-red-200/90"))}
                    </div>
                  )}
                </div>
              );
            })()}
          </div>
        ))}
        {busy && <div className="self-start rounded-xl border border-line bg-ink3 px-3 py-2 text-xs text-muted" role="status">{t((s?.pending_chat ?? 0) > 0 ? "消息已排队，当前回合结束后回复" : "正在回复…")}</div>}
      </div>
      <ChatComposer mode={chatMode} onModeChange={setChatMode} paired={paired} />
    </aside>
  );
}


function CommandFeedback({ message }: { message: ChatMsg }) {
  const feedback = chatFeedback(message);
  if (!feedback.sent.length && !feedback.other.length) return null;
  const unsent = feedback.other.filter((item) => item.state === "unsent").length;
  return <div className="android-command-feedback">
    {feedback.sent.length > 0 && <div className="android-command-chips" aria-label="已发送的设备操作">
      {feedback.sent.map((label, index) => <span key={index} className="android-command-chip">{label}</span>)}
    </div>}
    {feedback.other.length > 0 && <details className="android-command-other">
      <summary>{unsent > 0 ? `未发送 ${unsent} 项` : "其他回执详情"}</summary>
      {feedback.other.map((item, index) => <p key={index}>{item.label}</p>)}
    </details>}
  </div>;
}

function TurnProgress() {
  const s = useApp((state) => state.state);
  const busy = useChat((state) => state.busy);
  const awaitingConfirmation = useChat((state) => state.awaitingConfirmation);
  const turn = s?.turn_status;
  const active = !!turn && turn.phase !== "idle";
  const [now, setNow] = useState(Date.now());
  const received = useRef({ status: turn, at: Date.now() });
  if (received.current.status !== turn) received.current = { status: turn, at: Date.now() };
  useEffect(() => {
    if (!active && !s?.autopilot) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, s?.autopilot]);
  const elapsed = Math.max(0, Math.floor((turn?.elapsed_s ?? 0) + Math.max(0, now - received.current.at) / 1000));
  const next = turn?.next_auto_at_ms ? Math.max(0, Math.ceil((turn.next_auto_at_ms - now) / 1000)) : null;
  let label = "";
  if (active) {
    const phase = turn.phase === "thinking" ? "正在思考" : turn.phase === "executing" ? "正在执行操作" : "正在返回回复";
    label = `${turn.source === "autopilot" ? "自动运行 · " : ""}${phase} · ${elapsed} 秒`;
    if ((s?.pending_chat ?? 0) > 0 && turn.source !== "user") label += " · 消息已排队";
  } else if (awaitingConfirmation) label = "上次请求结果待确认";
  else if (busy) label = (s?.pending_chat ?? 0) > 0 ? "消息已排队，等待当前回合结束" : "正在确认本条消息的结果…";
  else if (s?.autopilot && !s.estop) {
    if (!s.connected && !s.test_mode) label = "自动运行 · 等待设备连接";
    else label = next !== null && next > 0 ? `自动运行 · 下一轮约 ${next} 秒后` : "自动运行 · 等待下一轮";
  }
  return <>
    {label && <p className="android-turn-status" role="status" aria-live="polite">{label}</p>}
    {!active && !busy && turn?.last_error && <p className="android-turn-error" role="status">上轮处理未完成：{turn.last_error}</p>}
  </>;
}
