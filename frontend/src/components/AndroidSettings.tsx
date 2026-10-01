import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useApp } from "../store";
import { ChevronRight } from "lucide-react";

export default function AndroidSettings({ onDone, onAbout }: { onDone: () => void; onAbout: () => void }) {
  const s = useApp((st) => st.state);
  const [baseUrl, setBaseUrl] = useState("https://api.deepseek.com");
  const [model, setModel] = useState("deepseek-flash");
  const [key, setKey] = useState("");
  const [hasKey, setHasKey] = useState(false);
  const [jsonMode, setJsonMode] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const [status, setStatus] = useState("");
  const [failed, setFailed] = useState(false);
  const [interval, setInterval] = useState(s?.autopilot_interval_s ?? 12);
  const [intervalStatus, setIntervalStatus] = useState("");

  useEffect(() => {
    let active = true;
    api.getLlm().then((info) => {
      if (!active) return;
      setBaseUrl(info.base_url || "https://api.deepseek.com");
      setModel(info.model || "deepseek-flash");
      setHasKey(info.has_key);
      setJsonMode(info.json_mode);
      setLoaded(true);
    }).catch(() => { if (active) { setStatus("读取配置失败，请返回后重试"); setFailed(true); } });
    return () => { active = false; };
  }, []);
  useEffect(() => { if (typeof s?.autopilot_interval_s === "number") setInterval(s.autopilot_interval_s); }, [s?.autopilot_interval_s]);
  const act = async (test: boolean) => {
    if (pending.current || !loaded) return;
    pending.current = true; setBusy(true); setStatus(""); setFailed(false);
    const body = { base_url: baseUrl.trim(), model: model.trim(), api_key: key.trim(), keep_api_key: hasKey && !key.trim(), json_mode: jsonMode };
    try {
      if (test) {
        const result = await api.testLlm(body);
        setFailed(!result.ok);
        setStatus(result.ok ? "连接成功" : result.error || "连接失败，请检查配置");
      } else {
        await api.setLlm(body);
        setHasKey(hasKey || !!key.trim()); setKey("");
        setStatus("已保存并生效");
      }
    } catch (error) { setFailed(true); setStatus(error instanceof Error ? error.message : String(error)); }
    finally { pending.current = false; setBusy(false); }
  };
  const saveInterval = async (value: number) => {
    try { await api.setAutopilotInterval(value); setIntervalStatus("已保存"); }
    catch { setIntervalStatus("保存失败，请重试"); }
  };

  return <section className="android-settings" data-coyote-settings-page>
    <h2>模型连接</h2>
    <form onSubmit={(event) => { event.preventDefault(); void act(false); }}>
      <label className="android-field-label" htmlFor="android-base-url">服务地址</label>
      <input id="android-base-url" className="android-field" type="url" autoComplete="off" autoCapitalize="none" spellCheck={false} required value={baseUrl} disabled={!loaded || busy} onChange={(event) => setBaseUrl(event.target.value)} />
      <label className="android-field-label" htmlFor="android-model">模型</label>
      <input id="android-model" className="android-field" autoComplete="off" autoCapitalize="none" spellCheck={false} required value={model} disabled={!loaded || busy} onChange={(event) => setModel(event.target.value)} />
      <div className="android-key-label"><label htmlFor="android-api-key">API Key</label><a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-label="前往 DeepSeek 获取 API Key（外部浏览器）">前往获取</a></div>
      <input id="android-api-key" className="android-field" type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} value={key} disabled={!loaded || busy} placeholder={hasKey ? "已保存 · 留空保留原密钥" : "粘贴 API Key"} onChange={(event) => setKey(event.target.value)} />
      <div className="android-form-actions"><button className="android-button" type="button" disabled={!loaded || busy} onClick={() => void act(true)}>测试连接</button><button className="android-button primary" type="submit" disabled={!loaded || busy}>{busy ? "处理中…" : "保存"}</button></div>
      {status && <p className={failed ? "android-compose-error" : "android-small"} role="status">{status}</p>}
      <details className="android-fold"><summary>高级选项</summary><div className="android-advanced">
        <label className="android-setting-row" htmlFor="android-interval"><span>自动运行间隔</span><span>{interval} 秒 / 轮</span></label>
        <input id="android-interval" type="range" min={5} max={30} step={1} value={interval} onChange={(event) => setInterval(Number(event.target.value))} onPointerUp={() => void saveInterval(interval)} onKeyUp={(event) => { if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) void saveInterval(interval); }} />
        {intervalStatus && <p className="android-small" role="status">{intervalStatus}</p>}
        <label className="android-setting-row"><span>JSON 模式</span><input type="checkbox" checked={jsonMode} disabled={!loaded || busy} onChange={(event) => setJsonMode(event.target.checked)} /></label>
        <p className="android-small">模型配置与聊天记录保存在本机，重启后仍可使用。</p>
        <p className="android-small">{s?.config_info?.version} · {s?.presets?.length ?? 0} 个波形</p>
      </div></details>
    </form>
    <button type="button" className="android-about-entry" data-coko-about onClick={onAbout}><span>关于</span><ChevronRight size={19} /></button>
    <button type="button" className="android-button android-done" onClick={onDone}>完成</button>
  </section>;
}
