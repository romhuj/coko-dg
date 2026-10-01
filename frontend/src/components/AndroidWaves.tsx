import { useMemo, useState } from "react";
import { ArrowLeft, ChevronRight, Search } from "lucide-react";
import { useApp } from "../store";

/** Browsing this catalog never starts playback; explicit actions live in manual control. */
export default function AndroidWaves({ onManual, onBack }: { onManual: () => void; onBack?: () => void }) {
  const presets = useApp((st) => st.state?.presets);
  const [query, setQuery] = useState("");
  const shown = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    return (presets ?? []).filter((preset) => !term || `${preset.name} ${preset.label} ${preset.category}`.toLocaleLowerCase().includes(term));
  }, [presets, query]);

  return <section className="android-waves" aria-label="波形">
    <div className="mb-[18px] mt-2 flex items-center gap-2">
      {onBack && <button type="button" className="-ml-3 flex min-h-12 w-12 flex-none items-center justify-center" aria-label="返回聊天" onClick={onBack}><ArrowLeft size={22} aria-hidden="true" /></button>}
      <h2 className="text-[25px] font-semibold tracking-tight">波形</h2>
      <span className="ml-auto text-[13px] text-muted">{presets?.length ?? 0} 项</span>
    </div>
    <p className="mb-6 text-sm leading-[1.8] text-muted">由 AI 结合角色与对话自动选择。</p>
    <label className="mb-[17px] flex min-h-[50px] items-center gap-2.5 rounded-[15px] bg-panel px-[15px]"><Search size={19} className="flex-none text-muted" aria-hidden="true" /><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} aria-label="搜索波形" placeholder="搜索波形" className="min-w-0 flex-1 border-0 bg-transparent text-[15px] text-text outline-none placeholder:text-muted" /></label>
    {shown.map((preset) => <div key={preset.name} className="flex min-h-[65px] items-center justify-between gap-3 border-b border-line px-px py-2.5"><strong className="min-w-0 break-words text-[15px] font-normal">{preset.name}</strong><span className="max-w-[40%] flex-none text-right text-xs leading-relaxed text-muted">{preset.category || "波形"}</span></div>)}
    {!shown.length && <p className="py-7 text-center text-sm text-muted" role="status">{presets === undefined ? "正在加载波形" : presets.length ? "没有匹配的波形" : "暂无波形"}</p>}
    <button type="button" onClick={onManual} className="mx-auto mt-[18px] flex min-h-12 items-center gap-1.5 px-2 text-sm text-muted underline underline-offset-4">进入手动控制<ChevronRight size={16} aria-hidden="true" /></button>
  </section>;
}
