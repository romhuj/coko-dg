import { useEffect, useRef, useState } from "react";
import { ChevronDown, Link2, Minus, Plus, Smartphone } from "lucide-react";
import { api } from "../api";
import { doResume, targets } from "../commands";
import { useApp, useChat } from "../store";

type Channel = "A" | "B";
const channels: Channel[] = ["A", "B"];

/** Android's manual controls keep the same server safety and A/B targeting paths. */
export default function AndroidDevice({ onPair, manualOpen = false }: { onPair: () => void; manualOpen?: boolean }) {
  const s = useApp((st) => st.state);
  const linkOn = useApp((st) => st.linkOn);
  const toggleLink = useApp((st) => st.toggleLink);
  const lastPreset = useApp((st) => st.lastPreset);
  const setLastPreset = useApp((st) => st.setLastPreset);
  const setFocus = useApp((st) => st.setFocus);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const [notice, setNotice] = useState("");
  const [failed, setFailed] = useState(false);
  const manual = useRef<HTMLDetailsElement>(null);
  const paired = s?.connected === true || s?.relay?.status === "paired" || s?.relay?.status === "ready";
  const devices = s?.relay?.clients?.flatMap((client) => client.devices ?? []) ?? [];
  const deviceName = devices[0]?.name || (paired ? "DG-LAB App" : "尚未连接设备");
  const presets = s?.presets ?? [];
  const categories = [...new Set(presets.map((preset) => preset.category || "其他"))];
  const estop = s?.estop ?? true;

  useEffect(() => {
    if (manualOpen && manual.current) {
      manual.current.open = true;
      manual.current.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }, [manualOpen]);

  const execute = async (ch: Channel, op: string, params: Record<string, unknown> = {}) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setNotice("");
    setFailed(false);
    setFocus(ch);
    const selected = targets(ch);
    const feedback: string[] = [];
    let hasFailure = false;
    try {
      for (const target of selected) {
        try {
          const result = await api.manual({ op, channel: target, ...params });
          for (const item of result.executed ?? []) {
            feedback.push(`${item.label}${item.sent ? "" : "（未发送到设备）"}`);
            if (!item.sent) hasFailure = true;
          }
          for (const item of result.dropped ?? []) {
            feedback.push(`${target}：${item.reason}`);
            hasFailure = true;
          }
        } catch (error) {
          feedback.push(`${target}：${error instanceof Error ? error.message : String(error)}`);
          hasFailure = true;
        }
      }
      const text = feedback.join("\n");
      setNotice(text);
      setFailed(hasFailure);
      if (text) useChat.getState().push({ role: "sys", text });
    } finally {
      pending.current = false;
      setBusy(false);
    }
  };

  const resume = async () => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setNotice("");
    setFailed(false);
    try {
      await doResume();
      setNotice("急停已解除。自动运行仍需在聊天页手动开启。");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
      setFailed(true);
    } finally {
      pending.current = false;
      setBusy(false);
    }
  };

  return (
    <section className="android-device" aria-label="设备">
      <h2 className="mb-[18px] mt-2 text-[25px] font-semibold tracking-tight">设备</h2>
      <div className="flex items-center gap-3 border-b border-line pb-6 pt-3">
        <div className="flex h-[46px] w-[46px] flex-none items-center justify-center rounded-[14px] border border-line"><Smartphone size={22} aria-hidden="true" /></div>
        <div className="min-w-0 flex-1">
          <strong className="block truncate text-base font-medium">{deviceName}</strong>
          <p className={`mt-1 flex items-center gap-1.5 text-xs ${paired ? "text-emerald-300" : "text-muted"}`}><span aria-hidden="true" className="h-1 w-1 rounded-full bg-current" />{paired ? "已连接 · Socket V4" : s?.relay?.status === "connecting" ? "中继连接中" : "等待 Socket V4 配对"}</p>
        </div>
        <button type="button" className="min-h-12 flex-none px-2 text-sm text-muted" onClick={onPair}>配对</button>
      </div>

      <div className="my-[22px] grid grid-cols-2 gap-3">
        {channels.map((ch) => <article key={ch} className="min-w-0 rounded-2xl bg-panel px-[17px] py-4">
          <h3 className="mb-3 text-[13px] font-medium text-muted">{ch} 通道</h3>
          <div className="text-[28px] font-semibold leading-tight tracking-tight tabular-nums">{s?.current?.[ch] ?? 0}<small className="ml-1 text-[13px] font-normal tracking-normal text-muted">/ {s?.effective_caps?.[ch] ?? 0}</small></div>
          <p className="mt-2 text-xs leading-[1.7] text-muted">记录值 / 上限<br /><span className="block truncate" title={s?.patterns?.[ch] || undefined}>{s?.pulse_active?.[ch] ? s.patterns?.[ch] || "播放中" : "空闲"}</span></p>
        </article>)}
      </div>

      {estop && <div className="mb-5 flex items-center gap-3 rounded-[14px] border border-bad/30 bg-bad/10 p-3">
        <div className="min-w-0 flex-1"><p className="text-sm text-bad">急停已锁定</p><p className="mt-1 text-xs leading-relaxed text-muted">解除后可进行设备操作。</p></div>
        <button type="button" disabled={busy || !s} onClick={() => void resume()} className="min-h-12 rounded-full border border-line px-4 text-[13px] disabled:opacity-40">解除急停</button>
      </div>}

      <details className="group border-t border-line">
        <summary className="flex min-h-16 cursor-pointer list-none items-center justify-between gap-3 text-[15px] [&::-webkit-details-marker]:hidden">通道上限<ChevronDown size={18} className="text-muted transition-transform group-open:rotate-180" aria-hidden="true" /></summary>
        <div className="pb-5">
          {channels.map((ch) => <CapInput key={ch} ch={ch} />)}
          <p className="mt-3 text-xs leading-relaxed text-muted">输入完成后自动保存；实际输出同时受 DG-LAB App 上限约束。</p>
        </div>
      </details>

      <details ref={manual} className="group mt-2 border-t border-line">
        <summary className="flex min-h-16 cursor-pointer list-none items-center justify-between gap-3 text-[15px] [&::-webkit-details-marker]:hidden">手动控制<ChevronDown size={18} className="text-muted transition-transform group-open:rotate-180" aria-hidden="true" /></summary>
        <div className="pb-5">
          <div className="flex min-h-[59px] items-center justify-between gap-3 border-b border-line py-2">
            <span className="text-sm">手动 A/B 联动</span>
            <button type="button" aria-pressed={linkOn} aria-label="手动 A/B 联动" disabled={busy} onClick={toggleLink} className={`flex min-h-12 items-center gap-2 rounded-full border px-4 text-[13px] disabled:opacity-40 ${linkOn ? "border-accent bg-accent text-ink" : "border-line text-muted"}`}><Link2 size={17} aria-hidden="true" />{linkOn ? "已开启" : "关闭"}</button>
          </div>
          <p className="mb-5 mt-3 text-xs leading-relaxed text-muted">{linkOn ? "手动操作将同时作用于 A/B 通道。" : "手动操作仅作用于对应通道。"}</p>
          {channels.map((ch) => {
            const picked = lastPreset[ch] || s?.patterns?.[ch] || presets[0]?.name || "";
            const selectedPreset = presets.some((preset) => preset.name === picked) ? picked : presets[0]?.name || "";
            const disabled = busy || estop || !paired || s?.enabled_channels?.[ch] === false;
            return <div key={ch} className="mb-[22px]">
              <h3 className="mb-3 text-sm font-medium">{ch} 通道{s?.enabled_channels?.[ch] === false && <span className="ml-2 text-xs text-muted">已关闭</span>}</h3>
              <div className="grid grid-cols-2 gap-2">
                <button type="button" disabled={disabled} aria-label={`减弱 ${ch} 通道`} title={`减弱 ${ch} 通道`} className="flex min-h-12 items-center justify-center rounded-xl border border-line disabled:opacity-35" onClick={() => void execute(ch, "add_strength", { delta: -10 })}><Minus size={22} aria-hidden="true" /></button>
                <button type="button" disabled={disabled} aria-label={`增强 ${ch} 通道`} title={`增强 ${ch} 通道`} className="flex min-h-12 items-center justify-center rounded-xl border border-line disabled:opacity-35" onClick={() => void execute(ch, "add_strength", { delta: 10 })}><Plus size={22} aria-hidden="true" /></button>
                <button type="button" disabled={busy || estop || !paired} className="col-span-2 min-h-12 rounded-xl border border-line text-[13px] disabled:opacity-35" aria-label={linkOn ? "停止 A/B 通道并清零" : `停止 ${ch} 通道并清零`} onClick={() => void execute(ch, "clear")}>停止并清零</button>
              </div>
              <label className="mb-2 mt-4 block text-xs text-muted" htmlFor={`android-wave-${ch}`}>选择波形</label>
              <select id={`android-wave-${ch}`} value={selectedPreset} disabled={!presets.length || busy} className="min-h-12 w-full rounded-xl border border-line bg-panel px-3 text-sm text-text disabled:opacity-40" onChange={(event) => { setFocus(ch); setLastPreset(ch, event.target.value); }}>
                {!presets.length && <option value="">暂无波形</option>}
                {categories.map((category) => <optgroup key={category} label={category}>{presets.filter((preset) => (preset.category || "其他") === category).map((preset) => <option key={preset.name} value={preset.name}>{preset.name}</option>)}</optgroup>)}
              </select>
              <button type="button" disabled={disabled || !selectedPreset} className="mt-2 min-h-12 w-full rounded-xl border border-line text-[13px] disabled:opacity-35" onClick={() => {
                for (const target of targets(ch)) setLastPreset(target, selectedPreset);
                void execute(ch, "pulse_hold", { pattern: selectedPreset });
              }}>立即播放到 {linkOn ? "A/B" : ch}</button>
            </div>;
          })}
          {!paired && <p className="text-xs leading-relaxed text-muted">连接设备后可使用手动控制。</p>}
        </div>
      </details>
      {notice && <p role="status" className={`mb-5 whitespace-pre-line rounded-xl bg-panel p-3 text-xs leading-relaxed ${failed ? "text-bad" : "text-muted"}`}>{notice}</p>}
    </section>
  );
}

function CapInput({ ch }: { ch: Channel }) {
  const s = useApp((st) => st.state);
  const cap = s?.user_caps?.[ch] ?? s?.effective_caps?.[ch] ?? 100;
  const hardCap = s?.caps?.[ch] ?? 100;
  const [draft, setDraft] = useState(String(cap));
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const dirty = useRef(false);
  const pending = useRef(false);

  useEffect(() => {
    if (!dirty.current && !pending.current) setDraft(String(cap));
  }, [cap]);

  const save = async () => {
    if (pending.current || !dirty.current) return;
    const value = Number(draft);
    if (!/^\d+$/.test(draft) || !Number.isSafeInteger(value) || value < 1 || value > hardCap) {
      setError(`请输入 1–${hardCap} 的整数`);
      return;
    }
    if (value === cap) { dirty.current = false; setError(""); return; }
    pending.current = true;
    setSaving(true);
    setError("");
    try {
      const result = await api.setChannelCap(ch, value);
      dirty.current = false;
      setDraft(String(result.user_caps?.[ch] ?? value));
    } catch (err) {
      setError(`未保存：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      pending.current = false;
      setSaving(false);
    }
  };

  return <div className="border-b border-line py-2">
    <div className="flex min-h-[51px] items-center justify-between gap-3">
      <label className="text-sm" htmlFor={`android-cap-${ch}`}>{ch} 通道上限</label>
      <input id={`android-cap-${ch}`} type="number" inputMode="numeric" min={1} max={hardCap} step={1} autoComplete="off" value={draft} disabled={saving || !s} aria-label={`${ch} 通道上限`} aria-describedby={`android-cap-help-${ch}`} aria-invalid={!!error} className="min-h-12 w-[94px] rounded-xl border border-line bg-panel px-3 text-right text-base tabular-nums text-text disabled:opacity-50" onChange={(event) => { dirty.current = true; setDraft(event.target.value); setError(""); }} onBlur={() => void save()} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); event.currentTarget.blur(); } }} />
    </div>
    <p id={`android-cap-help-${ch}`} role={error ? "alert" : undefined} className={`pb-1 text-right text-xs ${error ? "text-bad" : "text-muted"}`}>{error || (saving ? "保存中…" : `1–${hardCap}`)}</p>
  </div>;
}
