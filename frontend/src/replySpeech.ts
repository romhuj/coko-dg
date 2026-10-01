export type SpeechStatus = "off" | "preparing" | "ready" | "speaking" | "error";
export type SpeechBridge = {
  postMessage: (message: string) => void;
  onmessage: ((event: { data: string }) => void) | null;
};
export type SpeechMessage = { role: "user" | "ai" | "sys"; text: string; speechText?: string };
export type SpeechSnapshot = {
  status: SpeechStatus;
  sessionId: string | null;
  utteranceId: string | null;
  queued: number;
  message: string;
};
type Options = {
  bridge: () => SpeechBridge | undefined;
  createId: () => string;
  allowed: () => boolean;
  changed: (state: SpeechSnapshot) => void;
};

/** Only the user's auditioned voices and the existing system fallback are selectable. */
export const SPEECH_VOICES = [
  { id: "system-default", label: "系统默认", provider: "android-system" },
  { id: "kokoro-zf_001", label: "女声 A", provider: "offline" },
  { id: "kokoro-zm_010", label: "男声 B", provider: "offline" },
  { id: "melo-zh", label: "女声 C", provider: "offline" },
] as const;
export type SpeechVoiceId = typeof SPEECH_VOICES[number]["id"];
export type RoleSpeechPreference = { voiceId: SpeechVoiceId };
export function resolveSpeechVoice(id?: string): typeof SPEECH_VOICES[number] {
  return SPEECH_VOICES.find((voice) => voice.id === id) ?? SPEECH_VOICES[0];
}

/** Read the newest AI reply only. Native replaces audio atomically and filters microphone echo. */
export class ReplySpeech {
  private state: SpeechSnapshot = { status: "off", sessionId: null, utteranceId: null, queued: 0, message: "" };
  private queue: { utteranceId: string; text: string }[] = [];
  private seen = new WeakSet<SpeechMessage>();
  private bridge?: SpeechBridge;
  private handler: SpeechBridge["onmessage"] = null;
  private previousHandler: SpeechBridge["onmessage"] = null;

  constructor(private options: Options) {}
  get snapshot(): SpeechSnapshot { return this.state; }

  enable(history: readonly SpeechMessage[], voiceId: SpeechVoiceId = "system-default"): void {
    if (!this.options.allowed()) return;
    this.disable();
    this.seen = new WeakSet(history);
    const bridge = this.options.bridge();
    if (!bridge) { this.publish({ status: "error", message: "当前版本未提供回复朗读，请安装支持朗读的 APK。" }); return; }
    const sessionId = this.options.createId();
    this.bridge = bridge;
    this.previousHandler = bridge.onmessage ?? null;
    this.handler = (event) => this.receive(event.data);
    bridge.onmessage = this.handler;
    this.publish({ status: "preparing", sessionId, message: "正在准备回复朗读…" });
    try { bridge.postMessage(JSON.stringify({ type: "enable", sessionId, voiceId: resolveSpeechVoice(voiceId).id })); }
    catch { this.fail("无法启动回复朗读，请重试。"); }
  }

  disable(message = ""): void {
    const sessionId = this.state.sessionId;
    const bridge = this.bridge;
    this.queue = [];
    this.seen = new WeakSet();
    this.publish({ status: "off", sessionId: null, utteranceId: null, message });
    if (bridge && bridge.onmessage === this.handler) bridge.onmessage = this.previousHandler;
    this.bridge = undefined;
    this.handler = null;
    this.previousHandler = null;
    if (bridge && sessionId) {
      // Stop audio immediately, then relinquish this playback session.
      try { bridge.postMessage(JSON.stringify({ type: "stop", sessionId })); } catch { /* Continue disabling. */ }
      try { bridge.postMessage(JSON.stringify({ type: "disable", sessionId })); } catch { /* Local session is already invalid. */ }
    }
  }

  accept(messages: readonly SpeechMessage[]): void {
    if (!this.state.sessionId) return;
    if (!this.options.allowed()) { this.disable(); return; }
    for (const message of messages) {
      if (this.seen.has(message)) continue;
      this.seen.add(message);
      if (message.role !== "ai") continue;
      const text = (message.speechText ?? message.text).trim();
      if (!text) continue;
      if (text.length > 16000) { this.fail("这条回复过长，已暂停朗读。文字内容仍保留在聊天中。"); return; }
      // Model replies can arrive faster than speech. A newer reply replaces all pending audio,
      // while the chat history and device-action receipts remain untouched.
      this.queue = [{ utteranceId: this.options.createId(), text }];
    }
    this.publish({});
    this.drain();
  }

  private publish(update: Partial<SpeechSnapshot>): void {
    this.state = { ...this.state, ...update, queued: this.queue.length };
    this.options.changed(this.state);
  }

  private fail(message: string): void {
    this.disable();
    this.publish({ status: "error", message });
  }

  private drain(): void {
    if (!this.state.sessionId || !["ready", "speaking"].includes(this.state.status) || !this.queue.length) return;
    if (!this.options.allowed()) { this.disable(); return; }
    const next = this.queue.shift()!;
    // Reserve the utterance before crossing the bridge, including synchronous mocked callbacks.
    this.publish({ status: "speaking", utteranceId: next.utteranceId });
    try { this.bridge!.postMessage(JSON.stringify({ type: "speak", sessionId: this.state.sessionId, ...next, replace: true })); }
    catch { this.fail("朗读中断，请重新开启。"); }
  }

  private receive(raw: string): void {
    let event: Record<string, unknown>;
    try { event = JSON.parse(raw); } catch { return; }
    if (!event || !this.state.sessionId || event.sessionId !== this.state.sessionId) return;
    if (!this.options.allowed()) { this.disable(); return; }
    if (event.type === "interrupted") {
      // A confirmed user interruption discards old audio, without disabling future reply playback.
      if (!this.state.utteranceId || event.utteranceId !== this.state.utteranceId) return;
      this.queue = [];
      this.publish({ status: "ready", utteranceId: null, message: "" });
      return;
    }
    if (event.type !== "state") return;
    const status = event.status;
    if (!["off", "preparing", "ready", "speaking", "error"].includes(String(status))) return;
    const utteranceId = typeof event.utteranceId === "string" ? event.utteranceId : undefined;
    const message = typeof event.message === "string" ? event.message.slice(0, 500) : "";
    if (status === "off") { this.disable(message); return; }
    // Late completion/error for an earlier utterance must not advance or interrupt the current one.
    if (utteranceId && utteranceId !== this.state.utteranceId) return;
    if (status === "error") { this.fail(message || "回复朗读不可用，请稍后重试。"); return; }
    if (status === "ready") {
      if (this.state.utteranceId && utteranceId !== this.state.utteranceId) return;
      this.publish({ status: "ready", utteranceId: null, message });
      this.drain();
    } else if (status === "speaking") {
      if (!this.state.utteranceId || utteranceId !== this.state.utteranceId) return;
      this.publish({ status: "speaking", message });
    } else this.publish({ status: "preparing", message });
  }
}
