import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ChevronRight, Menu, Octagon, Settings, Smartphone, Square, SquarePen, UserRound, UserRoundPen, Waves } from "lucide-react";
import { api } from "../api";
import { useApp, useChat } from "../store";
import ChatPanel from "./ChatPanel";
import RoleCard from "./RoleCard";
import AndroidDevice from "./AndroidDevice";
import AndroidWaves from "./AndroidWaves";
import AndroidSettings from "./AndroidSettings";
import { PairView } from "./views";
import AndroidNickname from "./AndroidNickname";
import AndroidPairingDialog from "./AndroidPairingDialog";
import AndroidAbout from "./AndroidAbout";
import AndroidConversationRow from "./AndroidConversationRow";
import AndroidConversationDialog from "./AndroidConversationDialog";
import { conversationSwitchBlocked, refreshConversation, restoreConversation } from "../chatArchive";
import type { ConversationSummary } from "../types";

type Section = "聊天" | "角色" | "波形" | "设备" | "配对" | "设置" | "关于";
declare global { interface Window { coyoteHandleBack?: () => boolean } }

export default function AndroidLayout() {
  const s = useApp((st) => st.state);
  const preferenceError = useApp((st) => st.preferenceError);
  const [section, setSection] = useState<Section>("聊天");
  const [menu, setMenu] = useState(false);
  const [roleDialog, setRoleDialog] = useState(false);
  const [nickname, setNickname] = useState(false);
  const [pairing, setPairing] = useState(false);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [historyError, setHistoryError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyBefore, setHistoryBefore] = useState<string | number | undefined>();
  const [changingChat, setChangingChat] = useState(false);
  const [managedChat, setManagedChat] = useState<ConversationSummary | null>(null);
  const [deletingChat, setDeletingChat] = useState(false);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [managementError, setManagementError] = useState("");
  const historyPending = useRef(false);
  const changePending = useRef(false);
  const listGeneration = useRef(0);
  const loadedConversation = useRef<string | undefined>(undefined);
  const startupSeen = useRef(new Set<string>());
  const modal = roleDialog || nickname || pairing || !!managedChat;
  const [manualOpen, setManualOpen] = useState(false);
  const [modelReady, setModelReady] = useState<boolean | null>(null);
  const [notice, setNotice] = useState("");
  const [height, setHeight] = useState(window.visualViewport?.height ?? window.innerHeight);
  const menuTitle = useRef<HTMLHeadingElement>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const drawer = useRef<HTMLElement>(null);
  const swipe = useRef<{ x: number; y: number } | null>(null);
  const suppressClickUntil = useRef(0);
  useEffect(() => { if (preferenceError) { setNotice(preferenceError); useApp.setState({ preferenceError: "" }); } }, [preferenceError]);
  const go = (next: Section) => {
    if (document.querySelector('[role="dialog"]')) return;
    window.dispatchEvent(new Event("coyote:voice-stop"));
    setMenu(false); setSection(next); setManualOpen(false);
  };
  const openMenu = () => { window.dispatchEvent(new Event("coyote:voice-stop")); setMenu(true); };
  const openManual = () => { go("设备"); setManualOpen(true); };
  const closeMenu = () => { setMenu(false); menuButton.current?.focus(); };

  const loadConversations = async (before?: string | number) => {
    const token = ++listGeneration.current;
    setHistoryLoading(true); setHistoryError("");
    try {
      const result = await api.conversations(before);
      if (token !== listGeneration.current) return;
      setConversations((previous) => before === undefined ? result.conversations : [...previous, ...result.conversations.filter((item) => !previous.some((old) => old.id === item.id))]);
      const next = result.next_cursor ?? result.next_before;
      setHistoryBefore(result.has_more && next != null ? next : undefined);
    } catch (failure) {
      if (token !== listGeneration.current) return;
      if (before !== undefined && failure instanceof Error && failure.message.includes("聊天列表已更新")) { void loadConversations(); return; }
      setHistoryError(failure instanceof Error ? failure.message : "聊天列表加载失败");
    }
    finally { if (token === listGeneration.current) setHistoryLoading(false); }
  };
  const changeChat = async (id?: string) => {
    window.dispatchEvent(new Event("coyote:voice-stop"));
    if (changePending.current || historyPending.current) return;
    if (conversationSwitchBlocked()) { setNotice("请先等待当前消息完成，或确认上次请求，再切换聊天"); return; }
    if (id && id === useApp.getState().state?.conversation_id) { go("聊天"); return; }
    changePending.current = true; setChangingChat(true); setHistoryError("");
    try {
      const page = id ? await api.selectConversation(id) : await api.newConversation();
      loadedConversation.current = page.conversation_id;
      restoreConversation(page);
      const state = useApp.getState().state;
      if (state) useApp.getState().setState({ ...state, conversation_id: page.conversation_id, conversation_title: page.title, estop: true, autopilot: false });
      setMenu(false); setSection("聊天"); setManualOpen(false);
      void loadConversations();
    } catch (failure) { setHistoryError(failure instanceof Error ? failure.message : "切换聊天失败，请重试"); }
    finally { changePending.current = false; setChangingChat(false); }
  };
  const closeManagement = () => { if (!historyPending.current) { setManagedChat(null); setDeletingChat(false); setManagementError(""); } };
  const manageChat = (chat: ConversationSummary) => {
    if (changePending.current || historyPending.current) return;
    setManagedChat(chat); setDeletingChat(false); setManagementError("");
  };
  const pinChat = async () => {
    if (!managedChat || historyPending.current) return;
    historyPending.current = true; setHistoryBusy(true); setManagementError("");
    try {
      await api.pinConversation(managedChat.id, !managedChat.pinned);
      setManagedChat(null); setNotice(managedChat.pinned ? "已取消置顶" : "聊天已置顶");
      await loadConversations();
    } catch (failure) { setManagementError(failure instanceof Error ? failure.message : "置顶失败，请重试"); }
    finally { historyPending.current = false; setHistoryBusy(false); }
  };
  const deleteChat = async () => {
    if (!managedChat || historyPending.current) return;
    if (managedChat.id === useApp.getState().state?.conversation_id && conversationSwitchBlocked()) {
      setManagementError("请先等待当前消息完成，或确认上次请求，再删除当前聊天"); return;
    }
    historyPending.current = true; setHistoryBusy(true); setManagementError("");
    try {
      const result = await api.deleteConversation(managedChat.id);
      setManagedChat(null); setDeletingChat(false);
      if (result.active_changed && result.conversation) {
        const page = result.conversation;
        loadedConversation.current = page.conversation_id;
        restoreConversation(page);
        const state = useApp.getState().state;
        if (state) useApp.getState().setState({ ...state, conversation_id: page.conversation_id, conversation_title: page.title, estop: true, autopilot: false });
        setMenu(false); setSection("聊天"); setManualOpen(false);
        try { useApp.getState().setState(await api.state()); }
        catch { setNotice("聊天已删除，状态刷新失败，请稍后重试"); }
      }
      await loadConversations();
    } catch (failure) { setManagementError(failure instanceof Error ? failure.message : "删除失败，请重试"); }
    finally { historyPending.current = false; setHistoryBusy(false); }
  };

  useEffect(() => {
    const session = s?.chat_session_id;
    if (!session || startupSeen.current.has(session)) return;
    startupSeen.current.add(session);
    let seen = false;
    try { seen = sessionStorage.getItem("coyote.pairing-shown-session") === session; sessionStorage.setItem("coyote.pairing-shown-session", session); } catch { /* The in-memory session guard still prevents repeated prompts. */ }
    if (!seen && s?.relay?.status !== "paired") { window.dispatchEvent(new Event("coyote:voice-stop")); setPairing(true); }
  }, [s?.chat_session_id, s?.relay?.status]);
  useEffect(() => {
    if (!s?.conversation_id || loadedConversation.current === s.conversation_id) return;
    loadedConversation.current = s.conversation_id;
    void refreshConversation();
  }, [s?.conversation_id]);
  useEffect(() => {
    const refresh = () => { void refreshConversation(); };
    window.addEventListener("coyote:history-refresh", refresh);
    return () => window.removeEventListener("coyote:history-refresh", refresh);
  }, []);
  useEffect(() => {
    if (!menu) return;
    void loadConversations();
    const refresh = () => { void loadConversations(); };
    window.addEventListener("coyote:conversations-changed", refresh);
    return () => window.removeEventListener("coyote:conversations-changed", refresh);
  }, [menu]);

  useEffect(() => {
    document.body.classList.add("coyote-android");
    const update = () => setHeight(Math.min(window.innerHeight, window.visualViewport?.height ?? window.innerHeight));
    update();
    window.addEventListener("resize", update);
    window.visualViewport?.addEventListener("resize", update);
    return () => {
      document.body.classList.remove("coyote-android");
      window.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("resize", update);
    };
  }, []);
  useEffect(() => {
    if (section !== "聊天") return;
    let active = true;
    api.getLlm().then((v) => { if (active) setModelReady(!!(v.has_key && v.base_url.trim() && v.model.trim())); })
      .catch(() => { if (active) setModelReady(null); });
    return () => { active = false; };
  }, [section]);
  useEffect(() => { if (menu) menuTitle.current?.focus(); }, [menu]);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    const back = () => {
      window.dispatchEvent(new Event("coyote:voice-stop"));
      if (document.querySelector('[role="dialog"]')) {
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
        return true;
      }
      if (menu) { closeMenu(); return true; }
      if (section !== "聊天") { go(section === "关于" ? "设置" : section === "配对" ? "设备" : "聊天"); return true; }
      return false;
    };
    window.coyoteHandleBack = back;
    const keys = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key === "Escape" && !document.querySelector('[role="dialog"]') && back()) event.preventDefault();
      if (event.key !== "Tab" || !menu) return;
      const items = [...(drawer.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href]') ?? []),
        ...document.querySelectorAll<HTMLElement>('[data-coyote-estop]')];
      if (!items.length) return;
      event.preventDefault();
      const current = items.indexOf(document.activeElement as HTMLElement);
      const next = current < 0 ? (event.shiftKey ? items.length - 1 : 0) : (current + (event.shiftKey ? -1 : 1) + items.length) % items.length;
      items[next].focus();
    };
    document.addEventListener("keydown", keys);
    return () => { delete window.coyoteHandleBack; document.removeEventListener("keydown", keys); };
  }, [menu, section]);

  const stop = async () => {
    try {
      const result = await api.estop();
      const connected = useApp.getState().state?.connected;
      setNotice(connected && !result.sent ? "已锁定急停，但设备未确认清零，请检查官方 App" : "已急停：输出清零，自动运行已停止");
    } catch { setNotice("急停请求未确认，请立即检查官方 App 或设备"); }
  };
  const devices = s?.relay?.clients?.flatMap((client) => client.devices ?? []) ?? [];
  const roleLabel = s?.roles?.find((role) => role.name === s.role)?.label ?? s?.role ?? "情景助手";
  const settings = section === "设置";
  const about = section === "关于";
  const standalone = settings || about;

  return <div className="android-shell" style={{ height }}
    onPointerDown={(event) => {
      if (event.pointerType === "mouse" || standalone || modal || (event.target as HTMLElement).closest('input, textarea, select, [data-coyote-estop]')) return;
      swipe.current = { x: event.clientX, y: event.clientY };
    }}
    onPointerCancel={() => { swipe.current = null; }}
    onPointerUp={(event) => {
      const start = swipe.current; swipe.current = null;
      if (!start || standalone || modal) return;
      const dx = event.clientX - start.x, dy = event.clientY - start.y;
      if (Math.abs(dx) < 65 || Math.abs(dx) < Math.abs(dy) * 1.6) return;
      if (menu && dx < 0) closeMenu();
      else if (!menu && dx > 0) openMenu();
      else return;
      suppressClickUntil.current = Date.now() + 300;
    }}
    onClickCapture={(event) => {
      if (Date.now() < suppressClickUntil.current && !(event.target as HTMLElement).closest('[data-coyote-estop]')) {
        event.preventDefault(); event.stopPropagation();
      }
    }}>
    <header className="android-header" inert={menu || modal}>
      {standalone ? <>
        <button type="button" className="android-icon" aria-label={about ? "返回设置" : "返回聊天"} onClick={() => go(about ? "设置" : "聊天")}><ArrowLeft size={22} /></button>
        <h1>{section}</h1>
      </> : <>
        <button type="button" data-coyote-menu ref={menuButton} className="android-icon" aria-label="打开菜单" aria-expanded={menu} aria-controls="android-menu" onClick={openMenu}><Menu size={25} /></button>
        <nav className="android-tabs" aria-label="主要视图">
          {(["聊天", "设备"] as const).map((item) => <button key={item} type="button" aria-current={section === item ? "page" : undefined} onClick={() => go(item)}>{item}</button>)}
        </nav>
      </>}
      <span className="android-header-spacer" />
    </header>
    <button type="button" data-coyote-estop="true" className="android-estop" aria-label="急停：停止输出并清零" title="急停" onClick={() => void stop()}>
      <Octagon size={25} strokeWidth={1.8} /><Square size={9} fill="currentColor" className="android-estop-center" />
    </button>
    <div className="android-body" inert={menu || modal}>
      <div data-coyote-chat className={`android-chat-page ${section === "聊天" ? "" : "hidden"}`}>
        <div className="android-context">
          <button className="android-role-context" type="button" onClick={() => go("角色")}>{roleLabel}<ChevronRight size={15} /></button>
          {s?.estop && <button className="android-protection" type="button" onClick={() => go("设备")}>急停保护中</button>}
        </div>
        {modelReady === false && <div className="android-setup"><span>填写 API Key 后开始对话</span><button type="button" onClick={() => go("设置")}>配置模型<ChevronRight size={14} /></button></div>}
        <div className="android-chat-host"><ChatPanel active={section === "聊天" && !menu && !modal && !changingChat} /></div>
      </div>
      {section !== "聊天" && <main className="android-page" aria-label={`${section}面板`}>
        {!["设备", "设置", "关于", "波形"].includes(section) && <div className="android-page-title"><button className="android-icon" type="button" aria-label={section === "配对" ? "返回设备" : "返回聊天"} onClick={() => go(section === "配对" ? "设备" : "聊天")}><ArrowLeft size={22} /></button><h1>{section === "配对" ? "连接设备" : section}</h1></div>}
        {section === "角色" && <RoleCard onSearchOpenChange={setRoleDialog} />}
        {section === "设备" && <AndroidDevice onPair={() => { window.dispatchEvent(new Event("coyote:voice-stop")); setPairing(true); }} manualOpen={manualOpen} />}
        {section === "波形" && <AndroidWaves onManual={openManual} onBack={() => go("聊天")} />}
        {section === "配对" && <><PairView /><p className="android-small">配对不会自动解除急停。</p></>}
        {standalone && <div hidden={about}><AndroidSettings onDone={() => go("聊天")} onAbout={() => go("关于")} /></div>}
        {about && <AndroidAbout />}
      </main>}
    </div>
    {menu && <div className="android-drawer-layer">
      <div className="android-scrim" onClick={closeMenu} aria-hidden="true" />
      <aside id="android-menu" data-coyote-sidebar ref={drawer} className="android-drawer" aria-label="菜单" inert={modal}>
        <div className="android-drawer-heading"><h2 ref={menuTitle} tabIndex={-1}>coko DG</h2><button type="button" className="android-icon" aria-label="新建聊天" title="新建聊天" disabled={changingChat || historyBusy} onClick={() => void changeChat()}><SquarePen size={22} /></button></div>
        <div className="android-drawer-scroll">
          <nav aria-label="功能菜单">
            <button onClick={() => { window.dispatchEvent(new Event("coyote:voice-stop")); setMenu(false); setNickname(true); }}><UserRoundPen size={22} /><span>我的名称<small className="android-menu-nickname">{s?.config_info.player_nick || "设置称呼"}</small></span></button>
            <button onClick={() => go("角色")}><UserRound size={22} />角色</button>
            <button onClick={() => go("波形")}><Waves size={22} />波形</button>
            <button onClick={() => go("设备")}><Smartphone size={22} />设备</button>
          </nav>
          <div className="android-connected"><p>已连接 {devices.length}</p>{devices.map((device, i) => <button key={`${device.slotId}:${i}`} onClick={() => go("设备")}><span className="android-dot" />{device.name || `设备 ${i + 1}`}</button>)}</div>
          <section className="android-history" aria-label="聊天记录"><h3>聊天记录</h3>
            <span id="android-history-hint" className="sr-only">长按、右键或按 Shift 加 F10 管理聊天</span>
            {conversations.map((chat) => <AndroidConversationRow key={chat.id} chat={chat} active={chat.id === s?.conversation_id} disabled={changingChat || historyBusy} onSelect={() => void changeChat(chat.id)} onManage={() => manageChat(chat)} />)}
            {!conversations.length && !historyLoading && !historyError && <p>暂无聊天记录</p>}
            {historyLoading && <p role="status">正在加载…</p>}
            {changingChat && <p role="status">正在切换聊天…</p>}
            {historyError && <><p role="alert">{historyError}</p><button type="button" onClick={() => void loadConversations()}>重新加载</button></>}
            {historyBefore !== undefined && <button type="button" disabled={historyLoading} onClick={() => void loadConversations(historyBefore)}>更多聊天</button>}
          </section>
        </div>
        <footer><button data-coyote-settings onClick={() => go("设置")}><Settings size={21} />设置</button></footer>
      </aside>
    </div>}
    {nickname && <AndroidNickname onClose={() => setNickname(false)} />}
    {pairing && <AndroidPairingDialog onClose={() => setPairing(false)} />}
    {managedChat && <AndroidConversationDialog chat={managedChat} deleting={deletingChat} busy={historyBusy} error={managementError} active={managedChat.id === s?.conversation_id} onClose={closeManagement} onPin={() => void pinChat()} onAskDelete={() => { setDeletingChat(true); setManagementError(""); }} onDelete={() => void deleteChat()} />}
    {notice && <div className="android-toast" role="status">{notice}</div>}
  </div>;
}
