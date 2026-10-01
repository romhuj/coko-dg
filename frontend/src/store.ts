import { create } from "zustand";
import type { DevicePreferences, DroppedItem, DungeonRender, ExecutedItem, FullState } from "./types";

// ---------- 应用状态（与后端实时状态同步） ----------
interface AppStore {
  state: FullState | null;
  focusCh: "A" | "B";
  linkOn: boolean;
  lastPreset: Record<"A" | "B", string | null>;
  preferenceError: string;
  setState: (s: FullState) => void;
  setFocus: (ch: "A" | "B") => void;
  toggleLink: () => void;
  setLastPreset: (ch: "A" | "B", name: string) => void;
}
export const useApp = create<AppStore>((set) => ({
  state: null,
  focusCh: "A",
  linkOn: false,
  lastPreset: { A: null, B: null },
  preferenceError: "",
  setState: (s) => set((previous) => {
    const preferences = s.platform === "android" ? s.device_preferences : undefined;
    if (!preferences) return { state: s };
    for (const key of Object.keys(pendingPreferences) as (keyof DevicePreferences)[]) {
      if (JSON.stringify(pendingPreferences[key]) === JSON.stringify(preferences[key])) delete pendingPreferences[key];
    }
    return { state: s, focusCh: pendingPreferences.focus_channel ?? preferences.focus_channel ?? previous.focusCh,
      linkOn: pendingPreferences.manual_link ?? preferences.manual_link ?? previous.linkOn,
      lastPreset: pendingPreferences.last_presets ?? preferences.last_presets ?? previous.lastPreset };
  }),
  setFocus: (ch) => { if (useApp.getState().focusCh === ch) return; set({ focusCh: ch }); persistPreferences({ focus_channel: ch }); },
  toggleLink: () => set((st) => { const linkOn = !st.linkOn; persistPreferences({ manual_link: linkOn }); return { linkOn }; }),
  setLastPreset: (ch, name) => set((st) => { if (st.lastPreset[ch] === name) return {}; const lastPreset = { ...st.lastPreset, [ch]: name }; persistPreferences({ last_presets: lastPreset }); return { lastPreset }; }),
}));

const pendingPreferences: Partial<DevicePreferences> = {};
let preferenceWrites = Promise.resolve();
function persistPreferences(patch: Partial<DevicePreferences>): void {
  if (useApp.getState().state?.platform !== "android") return;
  Object.assign(pendingPreferences, patch);
  preferenceWrites = preferenceWrites.then(async () => {
    try {
      const { api } = await import("./api");
      const result = await api.setDevicePreferences(patch);
      const current = useApp.getState().state;
      if (current) useApp.getState().setState({ ...current, device_preferences: result.device_preferences });
    } catch {
      for (const key of Object.keys(patch) as (keyof DevicePreferences)[]) {
        if (JSON.stringify(pendingPreferences[key]) === JSON.stringify(patch[key])) delete pendingPreferences[key];
      }
      useApp.setState({ preferenceError: "设备选项未能保存，重启后可能恢复为之前的设置" });
    }
  });
}

// ---------- 聊天记录 ----------
export interface ChatMsg {
  id?: string;
  conversation_id?: string;
  created_at?: number | string;
  role: "user" | "ai" | "sys";
  text: string;
  /** Original AI reply only; empty when the visible text is an action-only placeholder. */
  speechText?: string;
  actions?: string;
  executed?: ExecutedItem[];
  dropped?: DroppedItem[];
}
interface ChatStore {
  messages: ChatMsg[];
  conversationId: string | null;
  historyRevision: number;
  historyLoading: boolean;
  historyError: string;
  historyHasMore: boolean;
  beginConversation: (id: string) => void;
  hydrate: (id: string, messages: ChatMsg[], hasMore: boolean) => void;
  prependHistory: (id: string, messages: ChatMsg[], hasMore: boolean) => void;
  ingest: (id: string, messages: ChatMsg[]) => void;
  busy: boolean;
  awaitingConfirmation: boolean;
  setAwaitingConfirmation: (value: boolean) => void;
  push: (m: ChatMsg) => void;
  setBusy: (b: boolean) => void;
  clear: () => void;
}
export const useChat = create<ChatStore>((set) => ({
  messages: [],
  conversationId: null,
  historyRevision: 0,
  historyLoading: false,
  historyError: "",
  historyHasMore: false,
  beginConversation: (id) => set((state) => state.conversationId === id ? {} : ({ conversationId: id, messages: [], historyLoading: true, historyError: "", historyHasMore: false, historyRevision: state.historyRevision + 1 })),
  hydrate: (id, messages, hasMore) => set((state) => state.conversationId !== id ? {} : ({ messages: mergeChatMessages(messages, state.messages), historyLoading: false, historyError: "", historyHasMore: hasMore, historyRevision: state.historyRevision + 1 })),
  prependHistory: (id, messages, hasMore) => set((state) => state.conversationId !== id ? {} : ({ messages: mergeChatMessages(messages, state.messages), historyLoading: false, historyError: "", historyHasMore: hasMore, historyRevision: state.historyRevision + 1 })),
  ingest: (id, messages) => set((state) => state.conversationId !== id ? {} : ({ messages: mergeChatMessages(state.messages, messages.filter((message) => message.conversation_id === id)) })),
  busy: false,
  awaitingConfirmation: false,
  setAwaitingConfirmation: (value) => set({ awaitingConfirmation: value }),
  push: (m) => set((st) => ({ messages: [...st.messages, m] })),
  setBusy: (b) => set({ busy: b }),
  clear: () => set((state) => ({ messages: [], historyRevision: state.historyRevision + 1 })),
}));

/** Stable message IDs make HTTP receipts, live WS delivery and archive hydration idempotent. */
export function mergeChatMessages(previous: ChatMsg[], incoming: ChatMsg[]): ChatMsg[] {
  const result = [...previous];
  const ids = new Map(result.flatMap((message, index) => message.id ? [[message.id, index] as const] : []));
  for (const message of incoming) {
    const index = message.id ? ids.get(message.id) : undefined;
    if (index === undefined) {
      if (message.id) ids.set(message.id, result.length);
      result.push(message);
    } else if (JSON.stringify(result[index]) !== JSON.stringify(message)) result[index] = message;
  }
  // Archives arrive chronologically; live items may race the first history fetch.
  return result.sort((a, b) => a.created_at != null && b.created_at != null
    ? (typeof a.created_at === "number" ? a.created_at : Date.parse(a.created_at)) - (typeof b.created_at === "number" ? b.created_at : Date.parse(b.created_at)) : 0);
}

// ---------- 布局（三栏固定比例，随窗口宽度计算，不可拖拽） ----------
function calcSidebarW(): number {
  try {
    const w = window.innerWidth;
    if (Number.isFinite(w) && w > 0) return Math.min(360, Math.max(160, Math.round(w * 0.158)));
  } catch {
    /* ignore */
  }
  return 268;
}

function calcControlW(): number {
  try {
    const w = window.innerWidth;
    if (Number.isFinite(w) && w > 0) return Math.min(900, Math.max(300, Math.round(w * 0.322)));
  } catch {
    /* ignore */
  }
  return 548;
}

/** 布局档位：宽 ≥1280 三栏；中 800-1279 聊天主栏 + 侧栏抽屉；窄 <800 单列 + 抽屉保安全操作 */
export type LayoutMode = "wide" | "mid" | "narrow";
function calcMode(): LayoutMode {
  try {
    const w = window.innerWidth;
    if (Number.isFinite(w) && w > 0) {
      if (w >= 1280) return "wide";
      if (w >= 800) return "mid";
      return "narrow";
    }
  } catch {
    /* ignore */
  }
  return "wide";
}

interface LayoutStore {
  sidebarW: number;
  controlW: number;
  mode: LayoutMode;
  updateLayout: () => void;
}
export const useLayout = create<LayoutStore>((set) => ({
  sidebarW: calcSidebarW(),
  controlW: calcControlW(),
  mode: calcMode(),
  updateLayout: () =>
    set({ sidebarW: calcSidebarW(), controlW: calcControlW(), mode: calcMode() }),
}));

// ---------- 地牢（紫金地牢） ----------
interface DungeonStore {
  render: DungeonRender | null;
  busy: boolean;
  error: string | null;
  notice: string | null;
  /** D30 路网：地图与岔口卡共享的「已选中待确认」节点；每帧 render 到达即清空 */
  selectedNodeId: string | null;
  /** D30 路网：mid/窄屏底部抽屉开合 */
  routeSheetOpen: boolean;
  setRender: (r: DungeonRender | null) => void;
  setBusy: (b: boolean) => void;
  setError: (e: string | null) => void;
  setNotice: (n: string | null) => void;
  setSelectedNodeId: (id: string | null) => void;
  setRouteSheetOpen: (o: boolean) => void;
}
export const useDungeon = create<DungeonStore>((set) => ({
  render: null,
  busy: false,
  error: null,
  notice: null,
  selectedNodeId: null,
  routeSheetOpen: false,
  setRender: (r) => set({ render: r, error: null, selectedNodeId: null, ...(r ? {} : { routeSheetOpen: false }) }),
  setBusy: (b) => set({ busy: b }),
  setError: (e) => set({ error: e }),
  setNotice: (n) => set({ notice: n }),
  setSelectedNodeId: (id) => set({ selectedNodeId: id }),
  setRouteSheetOpen: (o) => set({ routeSheetOpen: o }),
}));
