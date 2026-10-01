import type {
  ChatResult,
  DungeonRender,
  DungeonState,
  FullState,
  ManualResult,
  NetworkInfo,
  CharacterSearchResult,
  ConversationPage,
  ConversationList,
  DevicePreferences,
} from "./types";
import { useApp } from "./store";

export class ChatTransportError extends Error {
  constructor(message: string, public requestId?: string) { super(message); this.name = "ChatTransportError"; }
}
export class ChatUnsafeRetryError extends Error {
  constructor(message: string, public requestId?: string) { super(message); this.name = "ChatUnsafeRetryError"; }
}

type ChatReceipt = { status: "pending" | "completed"; request_id: string; result?: ChatResult };

async function chatReceipt(path: string, requestId: string, init?: RequestInit): Promise<ChatReceipt> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const resp = await fetch(path, { ...init, signal: controller.signal, cache: "no-store" });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({})) as { error?: string };
      if (resp.status >= 500) throw new ChatTransportError("连接中断，尚未确认处理结果", requestId);
      if (resp.status === 409 || resp.status === 410) throw new ChatUnsafeRetryError(data.error || "无法确认上次结果，请先检查设备状态", requestId);
      if (resp.status === 404 && !init) throw new ChatTransportError("尚未找到上次请求，请确认处理结果", requestId);
      throw new Error(data.error || `请求失败（${resp.status}）`);
    }
    const receipt = await resp.json() as ChatReceipt;
    const result = receipt?.result;
    const completeResult = result && typeof result.line === "string" && Array.isArray(result.executed) && Array.isArray(result.dropped)
      && (result.error == null || typeof result.error === "string");
    if (!receipt || receipt.request_id !== requestId || !["pending", "completed"].includes(receipt.status) ||
      (receipt.status === "completed" && !completeResult)) {
      throw new ChatTransportError("收到的处理结果不完整，请确认上次请求", requestId);
    }
    return receipt;
  } catch (error) {
    if (error instanceof ChatTransportError) throw error;
    if (error instanceof TypeError || error instanceof SyntaxError || controller.signal.aborted) {
      throw new ChatTransportError("连接中断，尚未确认处理结果", requestId);
    }
    throw error;
  } finally { clearTimeout(timer); }
}

async function sendChat(message: string, mode: "auto" | "text" | "device", preferredPattern?: string, requestId?: string, conversationId?: string): Promise<ChatResult> {
  if (!requestId) return j<ChatResult>("/api/chat", json({ message, mode, preferred_pattern: preferredPattern, conversation_id: conversationId }));
  const deadline = Date.now() + 120000;
  let receipt = await chatReceipt("/api/chat", requestId, json({ message, mode, preferred_pattern: preferredPattern, request_id: requestId, conversation_id: conversationId }));
  while (receipt.status === "pending") {
    if (Date.now() >= deadline) throw new ChatTransportError("仍未确认处理结果，请确认上次请求", requestId);
    await new Promise((resolve) => setTimeout(resolve, 800));
    receipt = await chatReceipt(`/api/chat/result/${encodeURIComponent(requestId)}`, requestId);
  }
  return receipt.result!;
}

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init);
  if (!resp.ok) {
    let msg = `${resp.status} ${resp.statusText}`;
    try {
      const data = (await resp.json()) as { error?: string; detail?: string };
      if (data?.error) msg = data.error;
      else if (typeof data?.detail === "string") msg = data.detail;
    } catch {
      /* 无 JSON 错误体时用状态码提示 */
    }
    throw new Error(msg);
  }
  return (await resp.json()) as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  state: () => j<FullState>("/api/state"),
  newChatRequestId: () => {
    const session = useApp.getState().state?.chat_session_id;
    if (!session) throw new Error("正在连接，请稍后发送");
    return `${session}:${crypto.randomUUID()}`;
  },
  chat: (message: string, mode: "auto" | "text" | "device" = "auto", preferredPattern?: string, requestId?: string, conversationId?: string) =>
    sendChat(message, mode, preferredPattern, requestId, conversationId),
  conversations: (before?: string | number) => j<ConversationList>(`/api/conversations${before === undefined ? "" : `?before=${encodeURIComponent(before)}`}`),
  conversationMessages: (id: string, before?: string) => j<ConversationPage>(`/api/conversations/${encodeURIComponent(id)}/messages?limit=100${before ? `&before=${encodeURIComponent(before)}` : ""}`),
  newConversation: () => j<ConversationPage>("/api/conversations", json({})),
  selectConversation: (id: string) => j<ConversationPage>(`/api/conversations/${encodeURIComponent(id)}/select`, json({})),
  pinConversation: (id: string, pinned: boolean) => j<{ conversation_id: string; pinned: boolean; active_id: string }>(`/api/conversations/${encodeURIComponent(id)}/pin`, json({ pinned })),
  deleteConversation: (id: string) => j<{ deleted_id: string; active_id: string; active_changed: boolean; conversation: ConversationPage | null }>(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" }),
  pinCharacter: (role: string, pinned: boolean) => j<{ok: boolean; role: string; pinned: boolean}>("/api/character/pin", json({role, pinned})),
  deleteCharacter: (role: string) => j<{ok: boolean; deleted_role: string; role: string; profile: string}>("/api/character/delete", json({role})),
  createCustomCharacter: (name: string, personality: string, background: string, voiceId?: string) =>
    j<{ok: boolean; role: string; profile: string}>("/api/character/custom", json({name, personality, background, voiceId})),
  searchCharacter: (query: string) => j<CharacterSearchResult>("/api/character/search", json({ query })),
  createCharacter: (searchId: string, sourceIndex: number, name: string, note: string, voiceId?: string) =>
    j<{ ok: boolean; role: string; profile: string }>("/api/character/create", json({ search_id: searchId, source_index: sourceIndex, name, note, voiceId })),
  manual: (action: Record<string, unknown>) =>
    j<ManualResult>("/api/manual", json(action)),
  estop: () => j<{ estop: boolean; sent: boolean }>("/api/estop", json({})),
  resume: () => j<ManualResult>("/api/resume", json({})),
  clearHistory: () => j<{ ok: boolean }>("/api/history/clear", json({})),
  network: () => j<NetworkInfo>("/api/network"),
  pairUrl: () => j<{ url: string }>("/api/pair_url"),
  deviceChannels: (
    channels: Record<string, { name?: string; location?: string; baseline?: number }>,
  ) => j<{ ok: boolean }>("/api/device/channels", json(channels)),
  setChannelEnabled: (channel: "A" | "B", enabled: boolean) =>
    j<{ ok: boolean }>("/api/device/channels/enabled", json({ channel, enabled })),
  setChannelCap: (channel: "A" | "B", value: number) =>
    j<{ ok: boolean; user_caps?: Record<string, number> }>(
      "/api/device/channels/cap",
      json({ channel, value }),
    ),
  reportLayout: (body: { sidebar_w: number; control_w: number; inner_width: number; zoom: number }) =>
    j<{ ok: boolean; layout?: Record<string, number> }>("/api/layout", json(body)),
  setSensor: (key: "camera" | "audio", enabled: boolean) =>
    j<{ ok: boolean; sensors?: { camera: boolean; audio: boolean } }>(
      "/api/sensors",
      json({ [key]: enabled }),
    ),
  setProfile: (role: string, profile: string) =>
    j<{ ok: boolean; role?: string; profile?: string }>(
      "/api/character/profile",
      json({ role, profile }),
    ),
  setIntensity: (level: string, syncToDevice?: boolean) =>
    j<{ ok: boolean; intensity_level?: string; strength_scale?: Record<string, number> }>(
      "/api/intensity",
      json({ level, sync_to_device: syncToDevice }),
    ),
  setNick: (nick: string) =>
    j<{ ok: boolean }>("/api/character/nick", json({ nick })),
  prepareModelJudgment: () => j<{ confirmation_token: string; wait_seconds: number }>("/api/character/model-judgment/prepare", json({})),
  setModelJudgment: (enabled: boolean, confirmationToken?: string) => j<{ ok: boolean; model_judgment: boolean }>("/api/character/model-judgment", json({ enabled, confirmation_token: confirmationToken })),
  setDevicePreferences: (preferences: Partial<DevicePreferences>) => j<{ ok: boolean; device_preferences: DevicePreferences }>("/api/device/preferences", json(preferences)),
  setLang: (lang: "zh" | "en") =>
    j<{ ok: boolean; lang?: string; en_available?: boolean }>(
      "/api/character/lang",
      json({ lang }),
    ),
  installContent: (file: File) =>
    j<{ ok: boolean; files: number; added: number; updated: number; sections: string[] }>(
      "/api/content/install",
      { method: "POST", headers: { "Content-Type": "application/zip" }, body: file },
    ),
  setAutopilot: (enabled: boolean) =>
    j<{ ok: boolean }>("/api/autopilot", json({ enabled })),
  setAutopilotInterval: (interval_s: number) =>
    j<{ ok: boolean; interval_s: number }>("/api/autopilot/interval", json({ interval_s })),
  testMode: (enabled: boolean) =>
    j<{ ok: boolean; test_mode: boolean }>("/api/test_mode", json({ enabled })),
  getLlm: () =>
    j<{
      base_url: string;
      model: string;
      api_key_masked: string;
      has_key: boolean;
      saved: boolean;
      json_mode: boolean;
    }>("/api/settings/llm"),
  setLlm: (body: { api_key: string; base_url: string; model: string; json_mode: boolean; keep_api_key?: boolean }) =>
    j<{ ok: boolean; model?: string }>("/api/settings/llm", json(body)),
  testLlm: (body: { api_key: string; base_url: string; model: string; keep_api_key?: boolean }) =>
    j<{ ok: boolean; error?: string; detail?: string }>(
      "/api/settings/llm/test",
      json(body),
    ),
  updateCheck: () =>
    j<{ enabled: boolean; latest: string; url: string; available: boolean }>("/api/update"),
  setUpdateCheck: (enabled: boolean) =>
    j<{ ok: boolean; enabled: boolean; latest: string; url: string; available: boolean }>(
      "/api/update",
      json({ enabled }),
    ),
  // ---------- 地牢 ----------
  dungeonState: () => j<DungeonState>("/api/dungeon/state"),
  /** 上一帧 render（D11 E1）：刷新后恢复进行中视图；只读，不触发设备动作 */
  dungeonRender: () => j<DungeonRender>("/api/dungeon/render"),
  dungeonStart: (body: { active_themes?: string[]; seed?: number }) =>
    j<DungeonRender>("/api/dungeon/start", json(body)),
  dungeonAdvance: (body: { choice_id?: string; text?: string }) =>
    j<DungeonRender>("/api/dungeon/advance", json(body)),
  /** 路网选路（D25/D30）：仅 map 模式、awaiting_move 且目标 reachable；否则 400 [not_reachable]/[not_awaiting]/[estop] */
  dungeonMove: (node_id: string) => j<DungeonRender>("/api/dungeon/move", json({ node_id })),
  dungeonSave: (slot: string) =>
    j<{ ok: boolean; path: string }>("/api/dungeon/save", json({ slot })),
  dungeonLoad: (slot: string) => j<DungeonRender>("/api/dungeon/load", json({ slot })),
  dungeonRestart: () => j<{ ok: boolean }>("/api/dungeon/restart", json({})),
};
