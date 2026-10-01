import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, ChevronUp, Plus } from "lucide-react";
import { api } from "../api";
import { useApp, useChat } from "../store";
import { useT } from "../i18n";
import RoleSearch from "./RoleSearch";
import AndroidRoleRow from "./AndroidRoleRow";
import AndroidRoleDeleteDialog from "./AndroidRoleDeleteDialog";
import AndroidRoleCreateDialog from "./AndroidRoleCreateDialog";
import AndroidModelJudgment from "./AndroidModelJudgment";
import AndroidRoleEditDialog from "./AndroidRoleEditDialog";
import { localRoleAvatar } from "../roleAvatar";
import { conversationSwitchBlocked } from "../chatArchive";
import {
  ENTRIES,
  INTENSITY_BADGE_CLS,
  INTENSITY_DOT_CLS,
  INTENSITY_LEVELS,
  ENTRY_RING_ACTIVE_CLS,
  ENTRY_RING_CLS,
  entryAvatar,
  entryOf,
  type Entry,
} from "../roleTheme";

// 体验版「新手推荐」角标：点过一次后不再显示（本地记忆）
const TRIAL_BADGE_KEY = "trial_badge_seen";

// 侧边栏顶部的「当前入口卡」：显示当前角色一行；点开浮动小下拉——上段列入口（体验版/角色），下段强度三档
export default function RoleCard({ onSearchOpenChange }: { onSearchOpenChange?: (open: boolean) => void } = {}) {
  const t = useT();
  const isAndroid = useApp((st) => st.state?.platform === "android");
  const role = useApp((st) => st.state?.role ?? "触手");
  const profile = useApp((st) => st.state?.profile ?? "纯爱");
  const intensity = useApp((st) => st.state?.intensity_level ?? "中");
  const roles = useApp((st) => st.state?.roles); // 后端就绪清单（prompt 文件是否存在）
  const localChatBusy = useChat((st) => st.busy || st.awaitingConfirmation || st.historyLoading);
  const serverChatBusy = useApp((st) => !!st.state?.turn_busy || (st.state?.pending_chat ?? 0) > 0);
  const chatBusy = localChatBusy || (isAndroid && serverChatBusy);
  const caps = useApp((st) => st.state?.effective_caps);
  const deviceLink = useApp((st) => st.state?.intensity_device_link ?? true);
  const [searchOpen, setSearchOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [judgmentOpen, setJudgmentOpen] = useState(false);
  const [editRole, setEditRole] = useState<string | null>(null);
  const [revealedEntry, setRevealedEntry] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ role: string; label: string; active: boolean } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteDone, setDeleteDone] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const deletedRole = useRef<string | null>(null);
  useEffect(() => {
    onSearchOpenChange?.(searchOpen || createOpen || !!deleteTarget || judgmentOpen || !!editRole);
    return () => onSearchOpenChange?.(false);
  }, [searchOpen, createOpen, deleteTarget, judgmentOpen, editRole, onSearchOpenChange]);
  const [intensityBusy, setIntensityBusy] = useState(false);
  const [roleBusy, setRoleBusy] = useState(false);
  const [roleStatus, setRoleStatus] = useState("正在切换角色，当前回合结束后生效…");
  const roleRequest = useRef(false);
  const intensityRequest = useRef(false);
  const dynamicEntries: Entry[] = (roles ?? []).flatMap((r) =>
    r.profiles
      .filter((p) => !ENTRIES.some((e) => e.role === r.name && e.profile === p.name))
      .map((p) => ({ key: `${r.name}:${p.name}`, role: r.name, profile: p.name, label: r.label || r.name })),
  );
  const entries = [...ENTRIES, ...dynamicEntries].filter((entry) => !isAndroid || roles?.some((r) => r.name === entry.role && r.profiles.some((p) => p.name === entry.profile)));
  const androidEntries = [...entries].sort((first, second) => Number(roles?.find((item) => item.name === second.role)?.pinned ?? false) - Number(roles?.find((item) => item.name === first.role)?.pinned ?? false));

  /** 入口就绪 = 后端该角色该档 available；state 未加载（roles 为 null）时不误锁；
   *  加载完但角色/档匹配不到 → 视为未导入（灰显，不静默放行） */
  const entryReady = (e: Entry): boolean => {
    if (!roles) return true;
    const r = roles.find((x) => x.name === e.role);
    const p = r?.profiles.find((x) => x.name === e.profile);
    return p ? p.available : false;
  };
  /** 展开入口列表前拉一次最新 state（available 是后端热加载快照，打开即见最新未导入态） */
  const refreshRoles = async () => {
    try {
      const s = await api.state();
      useApp.setState({ state: s });
    } catch {
      /* 拉取失败沿用现有快照，不打断交互 */
    }
  };
  const [open, setOpen] = useState(false);
  const [listStep, setListStep] = useState(false); // 展开入口列表
  const [err, setErr] = useState("");
  const [trialSeen, setTrialSeen] = useState(() => {
    try {
      return window.localStorage.getItem(TRIAL_BADGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const rowRef = useRef<HTMLButtonElement>(null);

  const current = dynamicEntries.find((e) => e.role === role && e.profile === profile) ?? entryOf(role, profile);

  // 点外部关闭下拉
  useEffect(() => {
    if (!open) return;
    const onDown = (ev: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(ev.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // 入口列表展开时：点列表外任意处收起列表（浮层本身保持打开，强度三档位置不动）
  useEffect(() => {
    if (!open || !listStep) return;
    const onDown = (ev: MouseEvent) => {
      if (listRef.current && !listRef.current.contains(ev.target as Node)) {
        if (rowRef.current && rowRef.current.contains(ev.target as Node)) return;
        setListStep(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open, listStep]);

  const switchEntry = async (e: Entry) => {
    if (chatBusy || roleRequest.current) return;
    setErr("");
    const already = e.role === role && e.profile === profile;
    // 点过体验版（或任意入口都算见过引导）→ 新手角标消失
    if (!trialSeen) {
      try {
        window.localStorage.setItem(TRIAL_BADGE_KEY, "1");
      } catch {
        /* 非核心的新手角标记忆失败不影响切换入口 */
      }
      setTrialSeen(true);
    }
    if (already) {
      setListStep(false);
      return;
    }
    roleRequest.current = true;
    setRoleStatus("正在切换角色，当前回合结束后生效…");
    setRoleBusy(true);
    try {
      await api.setProfile(e.role, e.profile);
      await refreshRoles();
      setListStep(false);
    } catch (ex) {
      setErr(t("切换失败：{msg}", { msg: (ex as Error).message }));
    } finally {
      roleRequest.current = false;
      setRoleBusy(false);
    }
  };

  const pinEntry = async (entry: Entry) => {
    const info = roles?.find((item) => item.name === entry.role);
    if (chatBusy || roleRequest.current || !info?.manageable) return;
    roleRequest.current = true;
    setRoleBusy(true);
    setRoleStatus("正在保存置顶…");
    setErr("");
    let saved = false;
    try {
      await api.pinCharacter(entry.role, !info.pinned);
      saved = true;
      useApp.setState({ state: await api.state() });
      setRevealedEntry(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setErr(saved ? `置顶已保存，列表刷新失败：${message}` : `置顶失败：${message}`);
    } finally { roleRequest.current = false; setRoleBusy(false); }
  };

  const askDelete = (entry: Entry) => {
    if (chatBusy || roleRequest.current || !roles?.find((item) => item.name === entry.role)?.manageable) return;
    setDeleteError(""); setDeleteDone(false); deletedRole.current = null;
    setDeleteTarget({ role: entry.role, label: entry.label, active: entry.role === role });
  };

  const deleteEntry = async () => {
    if (!deleteTarget || roleRequest.current || chatBusy) return;
    roleRequest.current = true;
    setDeleteBusy(true); setDeleteError("");
    try {
      if (deletedRole.current !== deleteTarget.role) {
        const wasActive = useApp.getState().state?.role === deleteTarget.role;
        await api.deleteCharacter(deleteTarget.role);
        deletedRole.current = deleteTarget.role;
        setDeleteDone(true);
        if (wasActive && !isAndroid) useChat.getState().clear();
      }
      useApp.setState({ state: await api.state() });
      setDeleteTarget(null); setRevealedEntry(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setDeleteError(deletedRole.current ? `角色已删除，列表刷新失败：${message}` : `删除失败：${message}`);
      // A storage failure can happen after the server has safely switched away.
      // Reflect that state without assuming the deleted role is still active.
      try {
        const fresh = await api.state();
        if (!isAndroid && deleteTarget.active && fresh.role !== deleteTarget.role) useChat.getState().clear();
        useApp.setState({ state: fresh });
      } catch { /* Keep the actionable error and allow an explicit retry. */ }
    } finally { roleRequest.current = false; setDeleteBusy(false); }
  };

  const switchIntensity = async (lv: string) => {
    if (lv === intensity || intensityRequest.current) return;
    setErr("");
    intensityRequest.current = true;
    setIntensityBusy(true);
    try {
      await api.setIntensity(lv);
      await refreshRoles();
    } catch (ex) {
      setErr(t("强度档切换失败：{msg}", { msg: (ex as Error).message }));
    } finally {
      intensityRequest.current = false;
      setIntensityBusy(false);
    }
  };

  if (isAndroid) {
    return (
      <div className="android-roles" ref={rootRef}>
        <div inert={searchOpen || createOpen || !!deleteTarget || judgmentOpen || !!editRole}>
        <div className="android-role-list" aria-label={t("角色入口")}>
          {androidEntries.map((entry) => {
            const active = entry.role === role && entry.profile === profile;
            const ready = entryReady(entry);
            const info = roles?.find((item) => item.name === entry.role);
            return (
              <AndroidRoleRow
                key={entry.key}
                label={t(info?.label || entry.label)}
                avatarUrl={localRoleAvatar(info?.avatar_url)}
                onEdit={() => { if (!roleRequest.current && !conversationSwitchBlocked()) { setRevealedEntry(null); setEditRole(entry.role); } }}
                description={!ready ? t("角色内容不可用") : info?.sources?.length ? t("联网创建的角色") : info?.profiles.find((item) => item.name === entry.profile)?.note || info?.title || t("结合角色性格与情景回应")}
                active={active} disabled={chatBusy || roleBusy} selectDisabled={!ready && !active}
                manageable={info?.manageable === true} pinned={info?.pinned === true}
                open={revealedEntry === entry.key}
                onOpenChange={(next) => setRevealedEntry(next ? entry.key : null)}
                onSelect={() => { setRevealedEntry(null); void switchEntry(entry); }}
                onPin={() => void pinEntry(entry)} onDelete={() => askDelete(entry)}
              />
            );
          })}
        </div>
        <button
          type="button"
          disabled={chatBusy || roleBusy}
          onClick={() => setCreateOpen(true)}
          className="android-role-search"
        ><Plus size={19} aria-hidden="true" />{t("创建角色")}</button>
        <AndroidModelJudgment disabled={chatBusy || roleBusy} onDialogChange={setJudgmentOpen} />
        <section className="android-strength" aria-labelledby="android-strength-title">
          <div className="android-strength-heading">
            <h3 id="android-strength-title">{t("输出强度")}</h3>
            <span>{t(intensity)}</span>
          </div>
          <div className="android-levels" role="group" aria-label={t("输出强度")}>
            {INTENSITY_LEVELS.map((level) => (
              <button
                type="button"
                key={level}
                className={`android-level${level === intensity ? " active" : ""}`}
                aria-pressed={level === intensity}
                disabled={intensityBusy}
                onClick={() => void switchIntensity(level)}
              >{t(level)}</button>
            ))}
          </div>
          <p className="android-strength-note">
            {t(deviceLink ? "同时作用于 A/B，切档本身不输出。" : "仅调整互动档位，设备强度联动未启用。")}<br />
            {t("任何档位都不超过两通道各自上限。")}
          </p>
        </section>
        {roleBusy && <p className="android-role-status" role="status">{t(roleStatus)}</p>}
        {err && <p className="android-compose-error" role="alert">{err}</p>}
        </div>
        {searchOpen && <RoleSearch onClose={() => setSearchOpen(false)} onCreated={() => { setSearchOpen(false); void refreshRoles(); }} />}
        {createOpen && <AndroidRoleCreateDialog onClose={() => setCreateOpen(false)} onSearch={() => { setCreateOpen(false); setSearchOpen(true); }} onCreated={() => { setCreateOpen(false); void refreshRoles(); }} />}
        {editRole && <AndroidRoleEditDialog role={editRole} onClose={() => setEditRole(null)} onSaved={() => { setEditRole(null); void refreshRoles(); }} />}
        {deleteTarget && <AndroidRoleDeleteDialog label={deleteTarget.label} active={deleteTarget.active} busy={deleteBusy} deleted={deleteDone} error={deleteError} onCancel={() => { if (!deleteBusy) { setDeleteTarget(null); if (deletedRole.current) void refreshRoles(); } }} onConfirm={() => void deleteEntry()} />}
      </div>
    );
  }

  return (
    <div ref={rootRef} className="relative mb-3.5 rounded-[14px] border border-line bg-panel p-3.5">
      <button
        className="flex w-full items-center gap-2.5 text-left"
        onClick={() => setOpen((v) => (v ? false : (setListStep(false), true)))}
        title={t("切换角色入口与电击强度")}
      >
        <span
          className={`relative flex h-9 w-9 flex-none items-center justify-center overflow-hidden rounded-[10px] border text-[16px] font-bold ${
            ENTRY_RING_ACTIVE_CLS[current?.key ?? ""] ?? "border-line2 bg-accent/15"
          }`}
        >
          {entryAvatar(current?.key ?? "") ? (
            <img
              src={entryAvatar(current?.key ?? "")!}
              alt={t(current?.label ?? role)}
              className="h-full w-full object-cover"
            />
          ) : (
            t(current?.label ?? role).slice(0, 1)
          )}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5 text-[14px] font-semibold">
            {t(current?.label ?? role)}
            {current?.recommended && (
              <span className="flex-none rounded-md border border-accent/60 bg-accent/15 px-1.5 py-px text-[10px] font-bold text-accent">
                {t("推荐")}
              </span>
            )}
            {current?.trial && !trialSeen && (
              <span className="flex-none rounded-md bg-accent px-1.5 py-px text-[10px] font-bold text-ink">
                {t("新手推荐")}
              </span>
            )}
          </span>
          <span className="mt-0.5 flex items-center gap-1 text-[11px] text-muted">
            <span
              className={`h-1.5 w-1.5 flex-none rounded-full ${INTENSITY_DOT_CLS[intensity] ?? ""}`}
            />
            {t("强度 · {v}", { v: t(intensity) })}
          </span>
        </span>
        {open ? (
          <ChevronUp size={14} className="flex-none text-faint" />
        ) : (
          <ChevronDown size={14} className="flex-none text-faint" />
        )}
      </button>

      {open && (
        <div className="absolute left-0 right-0 top-full z-30 mt-2 rounded-[12px] border border-line bg-panel shadow-xl shadow-black/50">
          <div className="flex flex-col gap-2 p-3">
            <div className="relative flex flex-col gap-0.5">
              <span className="px-1 text-[10px] font-medium tracking-wide text-muted">{t("角色入口")}</span>
              <button
                ref={rowRef}
                onClick={() => {
                  const next = !listStep;
                  if (next) void refreshRoles(); // 展开时刷新最新 available
                  setListStep(next);
                }}
                className="flex items-center gap-2 rounded-[6px] px-2 py-1.5 text-left text-[12px] transition-colors hover:bg-panel2"
                title={t("展开入口列表")}
              >
                <span className="flex-1 text-text">
                  {t(current?.label ?? role)}
                  <span className="ml-1 text-[10px] text-muted">{t("（点击换入口）")}</span>
                </span>
                {listStep ? (
                  <ChevronUp size={12} className="flex-none text-faint" />
                ) : (
                  <ChevronDown size={12} className="flex-none text-faint" />
                )}
              </button>
              {listStep && (
                <div
                  ref={listRef}
                  className="absolute left-0 right-0 top-full z-40 mt-1 flex max-h-[45vh] flex-col gap-0.5 overflow-y-auto rounded-[8px] border border-line bg-panel p-1 shadow-xl shadow-black/60"
                >
                  {entries.map((e) => {
                    const active = e.role === role && e.profile === profile;
                    const ready = entryReady(e);
                    const disabledEntry = !ready && !active; // 当前激活项保留可点（防呆）
                    return (
                      <button
                        key={e.key}
                        disabled={disabledEntry || chatBusy || roleBusy}
                        onClick={() => void switchEntry(e)}
                        title={
                          disabledEntry
                            ? t("内容稿未安装：到侧边栏「内容 / 语言包」安装 DLC 大包后即可使用")
                            : undefined
                        }
                        className={`flex items-center gap-2 rounded-[6px] border px-2 py-1.5 text-left text-[12px] transition-all ${
                          active
                            ? `${ENTRY_RING_ACTIVE_CLS[e.key] ?? "border-line2 bg-accent/15"} font-medium text-text scale-[1.03]`
                            : `border-transparent font-medium text-text ${ENTRY_RING_CLS[e.key] ?? ""} ${
                                ready ? "hover:bg-panel2 hover:scale-[1.03]" : "cursor-not-allowed opacity-40 grayscale"
                              }`
                        }`}
                      >
                        <span className="flex h-5 w-5 flex-none items-center justify-center overflow-hidden rounded-[5px]">
                          {entryAvatar(e.key) ? (
                            <img src={entryAvatar(e.key)!} alt={t(e.label)} className="h-full w-full object-cover" />
                          ) : (
                            <span className="text-[10px] font-bold">{t(e.label).slice(0, 1)}</span>
                          )}
                        </span>
                        <span className="min-w-0 flex-1">{t(e.label)}</span>
                        {!ready && (
                          <span className="flex-none rounded border border-line bg-panel2 px-1 py-px text-[9px] text-muted">
                            {t("不支持")}
                          </span>
                        )}
                        {e.recommended && (
                          <span className="flex-none rounded border border-accent/40 px-1 py-px text-[9px] font-bold text-accent">
                            {t("推荐")}
                          </span>
                        )}
                        {e.trial && !trialSeen && (
                          <span className="flex-none rounded bg-accent/15 px-1 py-px text-[9px] font-bold text-accent">
                            {t("新手推荐")}
                          </span>
                        )}
                        {active && <Check size={12} className="flex-none text-accent" />}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            <button type="button" disabled={chatBusy || roleBusy} onClick={() => { setSearchOpen(true); setOpen(false); }}
              className="rounded-[8px] border border-accent/40 bg-accent/10 px-2 py-2 text-[12px] font-medium text-accent hover:bg-accent/20">
              ＋ 角色扮演 · 网络搜索角色
            </button>

            <div className="flex flex-col gap-0.5">
              <div className="flex items-center justify-between px-1">
                <span className="text-[10px] font-medium tracking-wide text-muted">{t("强度")}</span>
                <span
                  className={`rounded-md border px-1.5 py-px text-[10px] ${INTENSITY_BADGE_CLS[intensity] ?? INTENSITY_BADGE_CLS["中"]}`}
                >
                  {t("当前 ×{s}", { s: deviceLink ? INTENSITY_SCALE_TEXT[intensity] ?? "1" : "1.0" })}
                </span>
              </div>
              <div className="grid grid-cols-3 gap-1">
                {INTENSITY_LEVELS.map((lv) => {
                  const selected = lv === intensity;
                  return (
                    <button
                      key={lv}
                      disabled={intensityBusy}
                      onClick={() => void switchIntensity(lv)}
                      className={`flex items-center justify-center gap-1.5 rounded-[6px] border px-1 py-1.5 text-[12px] transition-colors ${
                        selected
                          ? "border-accent bg-accent font-semibold text-ink"
                          : "border-line bg-panel2 font-medium text-text hover:border-line2"
                      }`}
                    >
                      <span
                        className={`h-1.5 w-1.5 flex-none rounded-full ${
                          selected ? "bg-ink" : (INTENSITY_DOT_CLS[lv] ?? "bg-faint")
                        }`}
                      />
                      {t(lv)}
                    </button>
                  );
                })}
              </div>
              <p className="mt-1 px-1 text-[10px] leading-relaxed text-muted">
                六档联动后续 AI 设备动作；切档本身不启动输出。<br />
                A ≤ {caps?.A ?? "—"} · B ≤ {caps?.B ?? "—"}，最高与炼狱也受此限制。
              </p>
            </div>

            {roleBusy && <p className="text-[11px] text-muted" role="status">{t("正在切换角色，当前回合结束后生效…")}</p>}
            {err && <p className="text-[10px] leading-relaxed text-red-400">{err}</p>}
          </div>
        </div>
      )}
      {searchOpen && <RoleSearch onClose={() => setSearchOpen(false)} onCreated={() => { setSearchOpen(false); setOpen(false); void refreshRoles(); }} />}
    </div>
  );
}

const INTENSITY_SCALE_TEXT: Record<string, string> = { 低: "0.7", 中: "1.0", 高: "1.3", 极高: "1.6", 最高: "2.0", 炼狱: "2.5" };
