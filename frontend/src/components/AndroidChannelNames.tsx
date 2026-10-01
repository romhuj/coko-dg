import { useRef, useState } from "react";
import { api } from "../api";
import { useApp } from "../store";
import AndroidDialog from "./AndroidDialog";

export default function AndroidChannelNames({ onClose }: { onClose: () => void }) {
  const [names, setNames] = useState(() => ({ A: useApp.getState().state?.device_channels?.A?.name || "A 通道", B: useApp.getState().state?.device_channels?.B?.name || "B 通道" }));
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const pending = useRef(false);
  const save = async () => {
    if (pending.current) return;
    const value = { A: names.A.trim(), B: names.B.trim() };
    if (Object.values(value).some((name) => [...name].length > 20 || /[\u0000-\u001f\u007f]/.test(name))) { setError("通道名称最多 20 字，不能包含换行或控制字符。"); return; }
    pending.current = true; setBusy(true); setError("");
    try {
      const result = await api.setChannelNames(value), state = useApp.getState().state;
      if (state) useApp.getState().setState({ ...state, device_channels: result.device_channels });
      onClose();
    } catch (failure) { setError(failure instanceof Error ? failure.message : "保存失败，请重试。"); }
    finally { pending.current = false; setBusy(false); }
  };
  return <AndroidDialog title="通道名称" id="android-channel-names-title" busy={busy} onClose={onClose}>
    <p className="android-small">为 A、B 通道设置名称。留空恢复默认名称。</p>
    <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      {(["A", "B"] as const).map((channel) => <div key={channel}>
        <label className="android-field-label" htmlFor={`android-channel-name-${channel}`}>{channel} 通道名称</label>
        <input id={`android-channel-name-${channel}`} className="android-field" value={names[channel]} disabled={busy} autoComplete="off" placeholder={`${channel} 通道`} onChange={(event) => { setError(""); setNames((current) => ({ ...current, [channel]: event.target.value })); }} />
      </div>)}
      {error && <p className="android-compose-error" role="alert">{error}</p>}
      <div className="android-dialog-actions"><button type="button" className="android-button" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="android-button primary" disabled={busy}>{busy ? "保存中…" : "保存"}</button></div>
    </form>
  </AndroidDialog>;
}
