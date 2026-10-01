export type VoiceStatus = "off" | "preparing" | "downloading" | "listening" | "error";
export type VoiceBridge = {
  postMessage: (message: string) => void;
  onmessage: ((event: { data: string }) => void) | null;
};
export type VoiceSentence = { sessionId: string; utteranceId: string; text: string };
export type VoiceSendResult = { status: "sent" | "blocked" | "failed" | "uncertain"; message?: string; unsafe?: boolean };
export type VoiceSnapshot = {
  status: VoiceStatus;
  sessionId: string | null;
  queued: number;
  message: string;
  progress?: number;
  reason?: "permission" | "playback";
  duplex: boolean;
  level: number;
  preview: string;
};
type Options = {
  bridge: () => VoiceBridge | undefined;
  createSessionId: () => string;
  allowed: () => boolean;
  blocked: () => boolean;
  captureBlocked?: () => boolean;
  submit: (sentence: VoiceSentence) => Promise<VoiceSendResult>;
  changed: (state: VoiceSnapshot) => void;
  review: (texts: string[], message: string, unsafe?: boolean) => void;
  emergencyStop: () => Promise<void>;
  maxQueued?: number;
};

export function isVoiceEmergencyStop(text: string): boolean {
  const normalized = text.trim().replace(/[。！？!?.,，；;、:：\s]+$/u, "").toLowerCase();
  return ["急停", "停止设备", "stop", "estop"].includes(normalized);
}

/** Native final sentences enter the same chat sender as typed messages. No device API lives here. */
export class ContinuousVoice {
  private state: VoiceSnapshot = { status: "off", sessionId: null, queued: 0, message: "", duplex: false, level: 0, preview: "" };
  private queue: VoiceSentence[] = [];
  private seen = new Set<string>();
  private inFlight = false;
  private bridge?: VoiceBridge;
  private previousHandler: VoiceBridge["onmessage"] = null;
  private handler: VoiceBridge["onmessage"] = null;

  constructor(private options: Options) {}

  get snapshot(): VoiceSnapshot { return this.state; }

  start(): void {
    if (!this.options.allowed()) return;
    this.stop();
    const bridge = this.options.bridge();
    if (!bridge) {
      this.publish({ status: "error", message: "当前版本未提供离线语音，请安装支持语音的 APK。" });
      return;
    }
    const sessionId = this.options.createSessionId();
    this.bridge = bridge;
    this.previousHandler = bridge.onmessage ?? null;
    this.handler = (event) => this.receive(event.data);
    bridge.onmessage = this.handler;
    this.publish({ status: "preparing", sessionId, message: "准备离线语音；首次使用需下载约 241 MB 资源，可随时取消。", progress: undefined, reason: undefined });
    try { bridge.postMessage(JSON.stringify({ type: "start", sessionId })); }
    catch { this.halt("无法启动麦克风，请重试。", []); }
  }

  stop(message = ""): void {
    const sessionId = this.state.sessionId;
    const bridge = this.bridge;
    // Invalidate before notifying native: synchronous or late callbacks must not enqueue again.
    this.queue = [];
    this.seen.clear();
    this.publish({ status: "off", sessionId: null, message, progress: undefined, reason: undefined, duplex: false, level: 0, preview: "" });
    if (bridge && bridge.onmessage === this.handler) bridge.onmessage = this.previousHandler;
    this.bridge = undefined;
    this.handler = null;
    this.previousHandler = null;
    if (sessionId && bridge) {
      try { bridge.postMessage(JSON.stringify({ type: "stop", sessionId })); } catch { /* Session is already invalid locally. */ }
    }
  }

  /** Call when the shared chat lock becomes available; never retries a failed sentence. */
  notifyReady(): void { void this.drain(); }

  pauseForError(message: string): void { this.halt(message, this.queue.map((item) => item.text)); }

  clearFeedback(): void {
    if (this.state.level || this.state.preview) this.publish({ level: 0, preview: "" });
  }

  private publish(update: Partial<VoiceSnapshot>): void {
    this.state = { ...this.state, ...update, queued: this.queue.length };
    this.options.changed(this.state);
  }

  private halt(message: string, texts: string[], unsafe = false): void {
    this.stop();
    this.publish({ status: "error", message });
    if (texts.length) this.options.review(texts, message, unsafe);
  }

  private capturePaused(): boolean {
    // Native confirms actual AEC/headset availability. Unknown capability stays conservative;
    // explicit playback suspension wins even during a capability/route transition.
    return this.state.reason === "playback" || (!this.state.duplex && !!this.options.captureBlocked?.());
  }

  private receive(raw: string): void {
    let event: Record<string, unknown>;
    try { event = JSON.parse(raw); } catch { return; }
    if (!event || !this.state.sessionId || event.sessionId !== this.state.sessionId) return;
    if (!this.options.allowed()) { this.stop(); return; }
    if (event.type === "level" || event.type === "preview") {
      if (this.state.status !== "listening" || this.capturePaused()) return;
      if (event.type === "level" && typeof event.level === "number" && Number.isFinite(event.level)) {
        this.publish({ level: Math.max(0, Math.min(1, event.level)) });
      } else if (event.type === "preview" && typeof event.text === "string") {
        this.publish({ preview: event.text.slice(-8000) });
      }
      return;
    }
    if (event.type === "state") {
      const status = event.status;
      if (!["off", "preparing", "downloading", "listening", "error"].includes(String(status))) return;
      const message = typeof event.message === "string" ? event.message.slice(0, 500) : "";
      if (status === "off") { this.stop(message); return; }
      if (status === "error") { this.halt(message || "语音识别失败，请重试。", this.queue.map((item) => item.text)); return; }
      this.publish({
        status: status as VoiceStatus,
        message,
        progress: typeof event.progress === "number" && Number.isFinite(event.progress) ? Math.max(0, Math.min(100, event.progress)) : undefined,
        reason: event.reason === "permission" || event.reason === "playback" ? event.reason : undefined,
        duplex: status === "listening" && event.duplex === true,
        ...(status !== "listening" || event.reason === "playback" ? { level: 0, preview: "" } : {}),
      });
      this.notifyReady();
      return;
    }
    if (event.type !== "sentence" || this.state.status !== "listening"
      || typeof event.utteranceId !== "string" || !event.utteranceId || event.utteranceId.length > 180
      || typeof event.text !== "string") return;
    if (this.capturePaused()) {
      // Never recycle a late recognition callback from audio played by this application.
      this.seen.add(event.utteranceId);
      return;
    }
    const text = event.text.trim();
    if (!text || this.seen.has(event.utteranceId)) return;
    // Only the exact stop whitelist bypasses the model queue; negation/quotes/general commands do not.
    if (isVoiceEmergencyStop(text)) {
      this.stop();
      void this.options.emergencyStop().catch(() => this.halt("急停请求未确认，请立即检查官方 App 或设备。", this.queue.map((item) => item.text)));
      return;
    }
    if (text.length > 8000) {
      this.halt("这段语音太长，已暂停监听。请分段编辑后发送。", [...this.queue.map((item) => item.text), text]);
      return;
    }
    this.seen.add(event.utteranceId);
    if (this.queue.length >= (this.options.maxQueued ?? 5)) {
      this.halt("待发语音已达 5 句，已暂停监听。请检查转写后发送。", [...this.queue.map((item) => item.text), text]);
      return;
    }
    this.queue.push({ sessionId: this.state.sessionId, utteranceId: event.utteranceId, text });
    this.publish({});
    this.notifyReady();
  }

  private async drain(): Promise<void> {
    if (this.inFlight || this.state.status !== "listening" || !this.state.sessionId || !this.queue.length || this.options.blocked()) return;
    if (!this.options.allowed()) { this.stop(); return; }
    const sentence = this.queue.shift()!;
    let blocked = false;
    this.inFlight = true;
    this.publish({});
    try {
      const result = await this.options.submit(sentence);
      // Navigation/stop discards unsent speech. An already submitted request still settles in chat.
      if (this.state.sessionId !== sentence.sessionId) {
        // A submitted sentence can still fail after the user stops listening. Retain it for review.
        if (result.status === "failed") this.halt(result.message || "语音发送失败。", [sentence.text, ...this.queue.map((item) => item.text)], result.unsafe);
        return;
      }
      if (result.status === "blocked") { blocked = true; this.queue.unshift(sentence); }
      else if (result.status === "failed" || result.status === "uncertain") {
        const remaining = this.queue.map((item) => item.text);
        this.halt(result.message || "发送未完成，语音已暂停。", result.status === "failed" ? [sentence.text, ...remaining] : remaining, result.unsafe);
      }
    } catch (error) {
      if (this.state.sessionId === sentence.sessionId) this.halt(error instanceof Error ? error.message : "语音发送失败。", [sentence.text, ...this.queue.map((item) => item.text)]);
    } finally {
      this.inFlight = false;
      this.publish({});
      if (!blocked && !this.options.blocked()) this.notifyReady();
    }
  }
}
